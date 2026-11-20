"""Comprehensive unit & integration tests for 100% free native social scheduling."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import smart_queue
import social_config
import social_scheduler
import telegram_bot
from publishers.base import PublishError, PublishResult
from publishers.instagram import InstagramPublisher
from publishers.tiktok import TikTokPublisher, generate_tiktok_desktop_pkce, plan_tiktok_chunks
from publishers.youtube import YouTubePublisher, format_youtube_short_metadata


def _mock_resp(status_code=200, json_data=None, headers=None, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.headers = headers or {}
    r.text = text or (json.dumps(json_data) if json_data is not None else "")
    r.json.return_value = json_data if json_data is not None else {}
    return r


class SocialSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.orig_queue_file = smart_queue.QUEUE_FILE
        self.orig_config_file = social_config.CONFIG_FILE

        smart_queue.QUEUE_FILE = self.tmp / "queue.json"
        social_config.CONFIG_FILE = self.tmp / "social_config.json"
        os.environ["SOCIAL_SCHEDULER_LOCK"] = str(self.tmp / ".scheduler.lock")

        self.sample_clip = self.tmp / "clip_test.mp4"
        self.sample_clip.write_bytes(b"\x00\x00\x00\x20ftypmp42" + b"0" * 4096)

    def tearDown(self):
        smart_queue.QUEUE_FILE = self.orig_queue_file
        social_config.CONFIG_FILE = self.orig_config_file
        os.environ.pop("SOCIAL_SCHEDULER_LOCK", None)
        self.tmpdir.cleanup()

    def test_legacy_queue_backward_compatibility_and_toggles(self):
        legacy = {
            "items": [
                {
                    "id": "clip_1_1795195996",
                    "job_id": "job-1",
                    "clip_file": str(self.sample_clip),
                    "hook": "5-Star Police Chase",
                    "caption": "Watch this #GTA6",
                    "credit": "ChampsNetwork",
                    "source_url": "https://youtube.com/watch?v=abc",
                    "scheduled_time": "2026-11-20T12:00",
                    "status": "approved",
                    "created_at": "2026-11-20T10:00:00",
                    "telegram_message_id": None,
                }
            ]
        }
        smart_queue.QUEUE_FILE.write_text(json.dumps(legacy), encoding="utf-8")

        loaded = smart_queue.load_queue()
        item = loaded["items"][0]
        self.assertIn("platforms", item)
        for p in ("instagram", "youtube", "tiktok"):
            self.assertTrue(item["platforms"][p]["enabled"])
            self.assertEqual(item["platforms"][p]["status"], "pending")

        # Toggle YouTube off and verify persistence
        toggled = smart_queue.toggle_platform("clip_1_1795195996", "youtube")
        self.assertFalse(toggled["platforms"]["youtube"]["enabled"])
        self.assertTrue(smart_queue.get_item("clip_1_1795195996")["platforms"]["instagram"]["enabled"])

        # Due check should detect legacy naive timestamp in the past
        due = smart_queue.get_due_items(now="2026-11-20T12:05")
        self.assertEqual(len(due), 1)

    def test_partial_failure_isolation_and_no_duplicate_reupload(self):
        past = (datetime.now().astimezone() - timedelta(minutes=10)).isoformat(timespec="minutes")
        smart_queue.QUEUE_FILE.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "id": "clip_partial",
                            "job_id": "job-p",
                            "clip_file": str(self.sample_clip),
                            "hook": "Test Hook",
                            "caption": "Caption #GTA6",
                            "credit": "creator",
                            "source_url": "https://yt",
                            "scheduled_time": past,
                            "status": "approved",
                            "platforms": smart_queue.default_platforms_map(["instagram", "tiktok"]),
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        cfg = social_config.save_social_config(
            {
                "instagram": {"enabled": True, "ig_user_id": "123", "access_token": "ig_tok"},
                "tiktok": {"enabled": True, "client_key": "k", "client_secret": "s", "access_token": "tt_tok"},
            }
        )

        ig_calls = []
        tt_calls = []

        class FakeIG:
            def __init__(self, *a, **kw): pass
            def publish(self, **kw):
                ig_calls.append(kw)
                return PublishResult("instagram", True, "published", post_id="ig_1", url="https://ig/1", message="OK")

        class FakeTTFailThenOK:
            attempt = 0
            def __init__(self, *a, **kw): pass
            def publish(self, **kw):
                tt_calls.append(kw)
                FakeTTFailThenOK.attempt += 1
                if FakeTTFailThenOK.attempt == 1:
                    raise PublishError("TikTok 503 temporary", retryable=True, status_code=503)
                return PublishResult("tiktok", True, "inbox_sent", post_id="tt_1", url="https://tiktok", message="Inbox")

        with patch.dict(
            social_scheduler.__dict__,
            {},
        ), patch("publishers.PUBLISHER_CLASSES", {"instagram": FakeIG, "tiktok": FakeTTFailThenOK, "youtube": MagicMock()}):
            # Also patch get_publisher import used by social_scheduler
            def fake_get_pub(plat, config=None, config_path=None):
                return {"instagram": FakeIG, "tiktok": FakeTTFailThenOK}[plat](config, config_path)

            with patch("social_scheduler.get_publisher", side_effect=fake_get_pub):
                processed = social_scheduler.process_due_items(config=cfg, notifier=False)

                self.assertEqual(len(processed), 1)
                item = smart_queue.get_item("clip_partial")
                self.assertEqual(item["status"], "partial_failed")
                self.assertEqual(item["platforms"]["instagram"]["status"], "published")
                self.assertEqual(item["platforms"]["tiktok"]["status"], "failed")
                self.assertIsNotNone(item["platforms"]["tiktok"]["next_retry_at"])

                # Simulate retry backoff elapsed
                q = smart_queue.load_queue()
                q["items"][0]["platforms"]["tiktok"]["next_retry_at"] = (
                    datetime.now().astimezone() - timedelta(seconds=5)
                ).isoformat(timespec="seconds")
                smart_queue.save_queue(q)

                social_scheduler.process_due_items(config=cfg, notifier=False)

                item_after = smart_queue.get_item("clip_partial")
                self.assertEqual(item_after["status"], "published")
                self.assertEqual(len(ig_calls), 1, "Instagram must NOT be re-uploaded on TikTok retry!")
                self.assertEqual(len(tt_calls), 2)

    def test_instagram_publisher_rupload_and_token_refresh(self):
        old_date = (datetime.now(timezone.utc) - timedelta(days=35)).isoformat()
        cfg = social_config.save_social_config(
            {
                "instagram": {
                    "enabled": True,
                    "api_version": "v25.0",
                    "app_id": "app1",
                    "app_secret": "sec1",
                    "ig_user_id": "ig_999",
                    "access_token": "old_ig_tok",
                    "token_obtained_at": old_date,
                    "poll_attempts": 3,
                    "poll_interval_sec": 0,
                }
            }
        )
        pub = InstagramPublisher(cfg, social_config.CONFIG_FILE)

        get_responses = [
            _mock_resp(200, {"access_token": "new_ig_tok", "expires_in": 5184000}),
            _mock_resp(200, {"status_code": "IN_PROGRESS"}),
            _mock_resp(200, {"status_code": "FINISHED"}),
            _mock_resp(200, {"permalink": "https://www.instagram.com/reel/XYZ/"}),
        ]
        post_responses = [
            _mock_resp(200, {"id": "cont_123", "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/cont_123"}),
            _mock_resp(200, {"success": True}),
            _mock_resp(200, {"id": "media_789"}),
        ]

        with patch("publishers.instagram.requests.get", side_effect=get_responses) as m_get, \
             patch("publishers.instagram.requests.post", side_effect=post_responses) as m_post:
            res = pub.publish(self.sample_clip, "Hook", "Caption #GTA6", "Creator")

        self.assertTrue(res.ok)
        self.assertEqual(res.status, "published")
        self.assertEqual(res.post_id, "media_789")
        self.assertEqual(res.url, "https://www.instagram.com/reel/XYZ/")

        # Verify rupload used OAuth header and persisted refreshed token
        rupload_call = m_post.call_args_list[1]
        self.assertEqual(rupload_call.args[0], "https://rupload.facebook.com/ig-api-upload/v25.0/cont_123")
        self.assertEqual(rupload_call.kwargs["headers"]["Authorization"], "OAuth new_ig_tok")
        self.assertEqual(social_config.load_social_config()["instagram"]["access_token"], "new_ig_tok")
        self.assertGreaterEqual(m_get.call_count, 4)

    def test_youtube_publisher_resumable_shorts_and_publish_at(self):
        long_hook = "A" * 120
        meta = format_youtube_short_metadata(long_hook, "Insane stunt #GTA6 #ViceCity", "LazarBeam")
        self.assertLessEqual(len(meta["title"]), 100)
        self.assertTrue(meta["title"].endswith("#Shorts"))
        self.assertIn("#Shorts", meta["description"])
        self.assertIn("GTA6", meta["tags"])

        expired = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        cfg = social_config.save_social_config(
            {
                "youtube": {
                    "enabled": True,
                    "client_id": "yt_cid",
                    "client_secret": "yt_sec",
                    "access_token": "stale_yt",
                    "refresh_token": "yt_ref",
                    "token_expires_at": expired,
                    "use_native_publish_at": True,
                    "chunk_size": 2048,
                }
            }
        )
        pub = YouTubePublisher(cfg, social_config.CONFIG_FILE)
        future_slot = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()

        post_calls = [
            _mock_resp(200, {"access_token": "fresh_yt", "expires_in": 3600}),
            _mock_resp(200, {}, headers={"Location": "https://upload.yt/session/abc"}),
        ]
        put_calls = [
            _mock_resp(308, {}, headers={"Range": "bytes=0-2047"}),
            _mock_resp(201, {"id": "yt_vid_99"}),
        ]

        with patch("publishers.youtube.requests.post", side_effect=post_calls) as m_post, \
             patch("publishers.youtube.requests.put", side_effect=put_calls) as m_put:
            res = pub.publish(self.sample_clip, "Whale in GTA 6", "Swim with whales #GTA6", "LazarBeam", scheduled_time=future_slot)

        self.assertTrue(res.ok)
        self.assertEqual(res.status, "scheduled_native")
        self.assertEqual(res.post_id, "yt_vid_99")
        self.assertEqual(res.url, "https://youtube.com/shorts/yt_vid_99")

        init_body = m_post.call_args_list[1].kwargs["json"]
        self.assertEqual(init_body["status"]["privacyStatus"], "private")
        self.assertTrue(init_body["status"]["publishAt"].endswith("Z"))
        self.assertEqual(m_put.call_count, 2)
        self.assertEqual(social_config.load_social_config()["youtube"]["access_token"], "fresh_yt")

    def test_tiktok_pkce_hex_and_chunk_math(self):
        verifier, challenge = generate_tiktok_desktop_pkce()
        self.assertGreaterEqual(len(verifier), 43)
        self.assertEqual(len(challenge), 64)
        self.assertRegex(challenge, r"^[0-9a-f]{64}$")

        mib = 1024 * 1024
        # < 5 MiB -> single chunk
        cs, cnt, rng = plan_tiktok_chunks(3 * mib)
        self.assertEqual((cs, cnt, rng), (3 * mib, 1, [(0, 3 * mib - 1)]))

        # 25 MiB -> single chunk
        cs, cnt, rng = plan_tiktok_chunks(25 * mib)
        self.assertEqual((cs, cnt, rng), (25 * mib, 1, [(0, 25 * mib - 1)]))

        # 75 MiB -> floor(75/10) = 7 chunks, final chunk absorbs remaining 15 MiB
        cs, cnt, rng = plan_tiktok_chunks(75 * mib)
        self.assertEqual(cs, 10 * mib)
        self.assertEqual(cnt, 7)
        self.assertEqual(len(rng), 7)
        self.assertEqual(rng[0], (0, 10 * mib - 1))
        self.assertEqual(rng[-1], (60 * mib, 75 * mib - 1))

    def test_tiktok_publisher_inbox_and_direct_and_token_rotation(self):
        expired = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        cfg = social_config.save_social_config(
            {
                "tiktok": {
                    "enabled": True,
                    "client_key": "tk_key",
                    "client_secret": "tk_sec",
                    "access_token": "old_tk",
                    "refresh_token": "old_tk_ref",
                    "token_expires_at": expired,
                    "mode": "inbox",
                    "poll_attempts": 2,
                    "poll_interval_sec": 0,
                }
            }
        )
        pub = TikTokPublisher(cfg, social_config.CONFIG_FILE)

        post_responses = [
            _mock_resp(200, {"access_token": "new_tk", "refresh_token": "rotated_tk_ref", "open_id": "oid_1", "expires_in": 86400}),
            _mock_resp(200, {"data": {"publish_id": "pub_inbox_1", "upload_url": "https://open-upload.tiktokapis.com/u1"}, "error": {"code": "ok"}}),
            _mock_resp(200, {"data": {"status": "SEND_TO_USER_INBOX"}, "error": {"code": "ok"}}),
        ]

        with patch("publishers.tiktok.requests.post", side_effect=post_responses), \
             patch("publishers.tiktok.requests.put", return_value=_mock_resp(201)):
            res = pub.publish(self.sample_clip, "Hook", "Cap #GTA6", "Creator")

        self.assertTrue(res.ok)
        self.assertEqual(res.status, "inbox_sent")
        self.assertEqual(res.post_id, "pub_inbox_1")
        persisted = social_config.load_social_config()["tiktok"]
        self.assertEqual(persisted["access_token"], "new_tk")
        self.assertEqual(persisted["refresh_token"], "rotated_tk_ref")

        # Now test Direct mode
        cfg_direct = social_config.update_platform_config("tiktok", {"mode": "direct", "privacy_level": "PUBLIC_TO_EVERYONE"})
        pub_direct = TikTokPublisher(cfg_direct, social_config.CONFIG_FILE)
        direct_posts = [
            _mock_resp(200, {"data": {"privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"]}, "error": {"code": "ok"}}),
            _mock_resp(200, {"data": {"publish_id": "pub_dir_1", "upload_url": "https://open-upload.tiktokapis.com/u2"}, "error": {"code": "ok"}}),
            _mock_resp(200, {"data": {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": ["777888"]}, "error": {"code": "ok"}}),
        ]
        with patch("publishers.tiktok.requests.post", side_effect=direct_posts), \
             patch("publishers.tiktok.requests.put", return_value=_mock_resp(200)):
            res_dir = pub_direct.publish(self.sample_clip, "Hook", "Direct post #GTA6", "Creator")

        self.assertTrue(res_dir.ok)
        self.assertEqual(res_dir.status, "published")
        self.assertEqual(res_dir.post_id, "777888")

    def test_fallback_manual_alert_when_no_platforms_configured(self):
        past = (datetime.now().astimezone() - timedelta(minutes=5)).isoformat(timespec="minutes")
        smart_queue.QUEUE_FILE.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "id": "clip_fallback",
                            "job_id": "job-f",
                            "clip_file": str(self.sample_clip),
                            "hook": "Fallback Hook",
                            "caption": "Caption",
                            "credit": "Creator",
                            "source_url": "https://yt",
                            "scheduled_time": past,
                            "status": "approved",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        cfg = social_config.save_social_config({})  # all disabled
        notifier = MagicMock()
        social_scheduler.process_due_items(config=cfg, notifier=notifier)

        notifier.send_manual_post_alert.assert_called_once()
        self.assertEqual(smart_queue.get_item("clip_fallback")["status"], "posted_alerted")

    def test_telegram_bot_callbacks(self):
        future = (datetime.now().astimezone() + timedelta(hours=2)).isoformat(timespec="minutes")
        smart_queue.QUEUE_FILE.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "id": "clip_tg_1",
                            "job_id": "job-tg",
                            "clip_file": str(self.sample_clip),
                            "hook": "TG Hook",
                            "caption": "TG Caption",
                            "credit": "Creator",
                            "source_url": "https://yt",
                            "scheduled_time": future,
                            "status": "pending",
                            "platforms": smart_queue.default_platforms_map(["instagram", "youtube", "tiktok"]),
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        desk = telegram_bot.TelegramDesk(token="fake_tok", chat_id="999")
        desk._sync_scheduler = True

        updates = [
            {"update_id": 1, "callback_query": {"id": "c1", "data": "tog_yt_clip_tg_1", "message": {"message_id": 10, "chat": {"id": 999}}}},
            {"update_id": 2, "callback_query": {"id": "c2", "data": "app_clip_tg_1", "message": {"message_id": 10, "chat": {"id": 999}}}},
            {"update_id": 3, "callback_query": {"id": "c3", "data": "now_clip_tg_1", "message": {"message_id": 10, "chat": {"id": 999}}}},
            {"update_id": 4, "callback_query": {"id": "c4", "data": "ret_clip_tg_1", "message": {"message_id": 10, "chat": {"id": 999}}}},
            {"update_id": 5, "callback_query": {"id": "c5", "data": "dec_clip_tg_1", "message": {"message_id": 10, "chat": {"id": 999}}}},
        ]

        with patch.object(desk, "poll_updates", return_value=updates), \
             patch("telegram_bot.requests.post", return_value=_mock_resp(200, {"ok": True})), \
             patch("social_scheduler.pre_schedule_native") as m_pre, \
             patch("social_scheduler.publish_item") as m_pub:
            next_off = desk.process_updates(None)

        self.assertEqual(next_off, 6)
        m_pre.assert_called_once_with("clip_tg_1", notifier=desk)
        self.assertEqual(m_pub.call_count, 2)
        final = smart_queue.get_item("clip_tg_1")
        self.assertFalse(final["platforms"]["youtube"]["enabled"])
        self.assertEqual(final["status"], "declined")


if __name__ == "__main__":
    unittest.main()
