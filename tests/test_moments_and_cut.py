"""Unit tests for audio-first moments, boundary snapping, hooks, segment download, and NVENC flags."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import cut
import moments


class MomentsAndCutTests(unittest.TestCase):
    def test_snap_boundaries_respects_sentence_and_tail(self):
        words = [
            {"w": "So", "s": 95.6, "e": 95.95},
            {"w": "hello", "s": 96.0, "e": 96.4},
            {"w": "world.", "s": 96.5, "e": 97.0},
            {"w": "Wait", "s": 98.2, "e": 98.5},
            {"w": "what", "s": 98.6, "e": 98.9},
            {"w": "is", "s": 99.0, "e": 99.2},
            {"w": "that", "s": 99.3, "e": 99.7},
            {"w": "thing?!", "s": 99.8, "e": 100.3},
            {"w": "Oh", "s": 115.0, "e": 115.3},
            {"w": "my", "s": 115.4, "e": 115.7},
            {"w": "god!", "s": 115.8, "e": 116.5},
            {"w": "Next", "s": 125.0, "e": 125.4},
        ]
        s0, e0 = moments.snap_boundaries(words, peak=114.0, dur=200.0, min_len=15.0, max_len=40.0)
        # Should snap start right before "Wait" (98.2 - 0.25 = 97.95) and end 1.0s after "god!" (116.5 + 1.0 = 117.5)
        self.assertAlmostEqual(s0, 97.95, places=2)
        self.assertAlmostEqual(e0, 117.5, places=2)
        self.assertGreaterEqual(e0 - s0, 15.0)
        self.assertLessEqual(e0 - s0, 40.0)

    def test_build_hooks_three_styles_and_bounds(self):
        text = "Um, wait until you see this hidden GTA 6 car. Nobody found this yet!"
        info = {"channel": "IShowSpeed", "title": "🔴 BEATING GTA 6 RIGHT NOW 100% (FINAL MISSION)"}
        primary, hooks = moments.build_hooks(text, info, ["story/payoff cue", "chat spike"])
        self.assertGreaterEqual(len(hooks), 2)
        self.assertLessEqual(len(primary), 52)
        for h in hooks:
            self.assertLessEqual(len(h), 52)

    def test_ensure_clip_source_local_vs_segment_download(self):
        with tempfile.TemporaryDirectory() as td:
            job_dir = Path(td)
            local_vid = job_dir / "source.mp4"
            local_vid.write_bytes(b"mp4")

            # Existing local source -> offset 0.0, is_segment False
            path1, off1, is_seg1 = cut.ensure_clip_source(job_dir, {"source": str(local_vid)}, 1, 100.0, 125.0)
            self.assertEqual(path1, local_vid)
            self.assertEqual(off1, 0.0)
            self.assertFalse(is_seg1)

            # Audio-first job (source=None) -> calls yt-dlp --download-sections with 5s padding
            cand = {
                "source": None,
                "duration": 500.0,
                "info": {"webpage_url": "https://www.youtube.com/watch?v=abc"},
            }
            with patch("cut.run") as m_run:
                path2, off2, is_seg2 = cut.ensure_clip_source(job_dir, cand, 2, 100.0, 125.0, padding=5.0)
            self.assertTrue(is_seg2)
            self.assertEqual(off2, 95.0)
            self.assertEqual(path2.name, "seg_2_9500_13000.mp4")
            cmd = m_run.call_args[0][0]
            self.assertIn("--download-sections", cmd)
            self.assertIn("*95.0-130.0", cmd)
            self.assertIn("--force-keyframes-at-cuts", cmd)

    def test_video_encoder_args_nvenc_and_fallback(self):
        with patch("cut.has_nvenc", return_value=True):
            self.assertEqual(cut.video_encoder_args("auto")[1], "h264_nvenc")
            self.assertEqual(cut.video_encoder_args("cpu")[1], "libx264")
        with patch("cut.has_nvenc", return_value=False):
            self.assertEqual(cut.video_encoder_args("auto")[1], "libx264")
            self.assertEqual(cut.video_encoder_args("nvenc")[1], "h264_nvenc")

    def test_snap_boundaries_clamps_tail_before_next_word(self):
        words = [
            {"w": "Start.", "s": 96.0, "e": 96.5},
            {"w": "Look", "s": 98.0, "e": 98.3},
            {"w": "at", "s": 98.4, "e": 98.6},
            {"w": "that", "s": 115.0, "e": 115.5},
            {"w": "jump", "s": 115.6, "e": 116.2},
            {"w": "Anyway", "s": 116.8, "e": 117.2},
            {"w": "moving", "s": 117.3, "e": 139.0},
            {"w": "on.", "s": 139.1, "e": 140.0},
        ]
        s0, e0 = moments.snap_boundaries(words, peak=113.0, dur=200.0, min_len=15.0, max_len=40.0)
        self.assertAlmostEqual(s0, 97.75, places=2)
        # Weak boundary at "jump" (116.2) with 0.6s gap before "Anyway" (116.8) must not bleed into "Anyway"
        self.assertLess(e0, 116.8)
        self.assertAlmostEqual(e0, 116.7, places=2)
        self.assertGreaterEqual(e0 - s0, 15.0)

        # If a later weak boundary occurs before a strong boundary, keep the first weak boundary unless a strong one appears
        words_two_weak = [
            {"w": "Start.", "s": 96.0, "e": 96.5},
            {"w": "Look", "s": 98.0, "e": 98.3},
            {"w": "jump", "s": 115.6, "e": 116.2},
            {"w": "Anyway", "s": 116.8, "e": 117.2},
            {"w": "pause", "s": 117.3, "e": 118.0},
            {"w": "here", "s": 118.6, "e": 139.0},
            {"w": "end.", "s": 139.1, "e": 140.0},
        ]
        _, e1 = moments.snap_boundaries(words_two_weak, peak=113.0, dur=200.0, min_len=15.0, max_len=40.0)
        self.assertAlmostEqual(e1, 116.7, places=2)

    def test_build_ass_masks_slurs_and_swears_in_hook_and_title(self):
        words = [{"w": "hello", "s": 10.0, "e": 10.5}, {"w": "world", "s": 10.6, "e": 11.0}]
        ass_masked = cut.build_ass(
            words, 10.0, 20.0, hook="What the fuck nigger", cap_y=1460,
            title=("box", "This bitch ass stunt", 730, 0), profanity=False,
        )
        self.assertNotIn("fuck", ass_masked.lower())
        self.assertNotIn("nigger", ass_masked.lower())
        self.assertNotIn("bitch", ass_masked.lower())
        self.assertIn("b****", ass_masked)

        ass_profane = cut.build_ass(
            words, 10.0, 20.0, hook="What the fuck nigger", cap_y=1460,
            title=None, profanity=True,
        )
        self.assertIn("fuck", ass_profane.lower())
        self.assertNotIn("nigger", ass_profane.lower())
        self.assertIn("n*****", ass_profane)

    def test_build_hooks_word_boundary_trim_and_clean(self):
        long_text = "This is a completely ridiculous moment happening right now in Vice City! What the fuck was that leak!"
        info = {"channel": "LongChannelNameForTesting", "title": "CRAZY LEAKED BUILD GAMEPLAY VIDEO"}
        primary, hooks = moments.build_hooks(long_text, info, ["loudness spike"])
        self.assertNotIn("fuck", primary.lower())
        self.assertNotIn("leak", primary.lower())
        self.assertLessEqual(len(primary), 52)
        for h in hooks:
            self.assertLessEqual(len(h), 52)
            self.assertNotIn("leak", h.lower())

        # 7-word quote exceeding 48 chars should trim cleanly at a word boundary
        quote = "Wait until everybody sees this extraordinary explosion!"
        p2, _ = moments.build_hooks(quote, {"channel": "Test"}, [])
        self.assertEqual(p2, "WAIT UNTIL EVERYBODY SEES THIS EXTRAORDINARY")
        self.assertLessEqual(len(p2), 48)

    def test_transcript_signals_matches_multiword_hype_and_no_triple_count(self):
        words = [
            {"w": "Oh", "s": 5.0, "e": 5.2},
            {"w": "my", "s": 5.2, "e": 5.4},
            {"w": "god", "s": 5.4, "e": 5.7},
            {"w": "no", "s": 10.0, "e": 10.2},
            {"w": "way", "s": 10.2, "e": 10.5},
            {"w": "check", "s": 14.0, "e": 14.3},
            {"w": "this", "s": 14.5, "e": 14.8},
            {"w": "secret", "s": 15.0, "e": 15.4},
        ]
        hype, semantic, tip_sig, cadence = moments.transcript_signals(words, 20)
        self.assertEqual(hype[5], 1)
        self.assertEqual(hype[10], 1)
        self.assertEqual(semantic[15], 1)
        self.assertEqual(semantic[14], 0)
        self.assertEqual(tip_sig.sum(), 0)
        self.assertGreater(cadence[5], 0)

    def test_clip_filename_consistent_across_channels_and_titles(self):
        import common
        self.assertEqual(
            common.clip_filename("caseoh_", 2, "Gator Pool"),
            "caseoh_clip_2_gator_pool.mp4",
        )
        self.assertEqual(
            common.clip_filename("caseoh_", 5, "caseoh_ Chat LOST IT in GTA 6"),
            "caseoh_clip_5_chat_lost_it_in_gta_6.mp4",
        )
        self.assertEqual(
            common.clip_filename("Kai Cenat", 3, "Recon Detail!", ext="json"),
            "kai_cenat_clip_3_recon_detail.json",
        )
        self.assertEqual(
            common.clip_filename("", 1, "Bigfoot Ambush"),
            "clip_1_bigfoot_ambush.mp4",
        )
        self.assertEqual(
            common.clip_filename("summit1g", 4, ""),
            "summit1g_clip_4.mp4",
        )


if __name__ == "__main__":
    unittest.main()
