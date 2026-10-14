import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from medal_highlight_reel.manual_review import build_review_queue
from medal_highlight_reel.manual_timestamps import file_sha256, save_manual_records
from medal_highlight_reel.segment_planner import PlanError, create_plan, load_window_config


class ManualTimestampTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        self.video = self.root / "review.mp4"
        self.video.write_bytes(b"test fixture")
        self.clips = self.root / "clips.csv"
        self.bookmarks = self.root / "bookmarks.csv"
        self.corrections = self.root / "manual-timestamps.csv"
        self._write_reports()

    def tearDown(self):
        self.temp.cleanup()

    def _write_reports(self, status="MISSING_TIMING_DATA", video_path=None):
        with self.clips.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["local_content_id", "created_at_utc", "game", "video_path", "clip_duration_seconds"])
            writer.writeheader()
            writer.writerow({"local_content_id":"c1", "created_at_utc":"2026-01-01T00:00:00Z", "game":"Valorant", "video_path":str(video_path or self.video), "clip_duration_seconds":"10"})
        with self.bookmarks.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["local_content_id", "bookmark_name", "candidate_recovered_position_seconds", "position_status"])
            writer.writeheader()
            writer.writerow({"local_content_id":"c1", "bookmark_name":"Kill", "candidate_recovered_position_seconds":"", "position_status":status})

    def _save_mark(self, row_number=1, position=4.25):
        save_manual_records(self.corrections, {row_number: {
            "bookmarks_csv_sha256":file_sha256(self.bookmarks), "bookmark_row_number":str(row_number),
            "local_content_id":"c1", "bookmark_name":"Kill", "video_path":str(self.video),
            "position_seconds":str(position), "source_duration_seconds":"10", "marked_at_utc":"2026-01-01T00:00:00Z",
            "status":"MANUALLY_MARKED"}})

    def test_manual_position_is_used_and_identified_in_plan(self):
        self._save_mark()
        plan, rejected = create_plan(self.clips, self.bookmarks, load_window_config(None), manual_timestamps_path=self.corrections)
        self.assertFalse(rejected)
        bookmark = plan["segments"][0]["bookmarks"][0]
        self.assertEqual(bookmark["candidate_position_seconds"], 4.25)
        self.assertEqual(bookmark["position_status"], "MANUALLY_MARKED")
        self.assertEqual(bookmark["manual_verification_status"], "MANUALLY_VERIFIED")

    def test_manual_mark_is_rejected_if_source_report_changes(self):
        self._save_mark()
        self.bookmarks.write_bytes(self.bookmarks.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "different bookmarks.csv"):
            create_plan(self.clips, self.bookmarks, load_window_config(None), manual_timestamps_path=self.corrections)

    def test_manual_position_outside_duration_is_rejected(self):
        self._save_mark(position=11)
        with self.assertRaisesRegex(PlanError, "outside the clip duration"):
            create_plan(self.clips, self.bookmarks, load_window_config(None), manual_timestamps_path=self.corrections)

    def test_review_queue_only_contains_valid_missing_timing_rows(self):
        self._write_reports(status="IN_RANGE")
        # Add one event with missing timing after the in-range row.
        with self.bookmarks.open("a", newline="", encoding="utf-8") as f:
            f.write("c1,ACE,,MISSING_TIMING_DATA\n")
        with patch("medal_highlight_reel.manual_review.resolve_executable", return_value="ffprobe"), \
             patch("medal_highlight_reel.manual_review.probe_media", return_value=(10.0, True, True)):
            queue, skipped, _records, _digest = build_review_queue(self.clips, self.bookmarks, self.corrections)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["bookmark_name"], "ACE")
        self.assertEqual(skipped, [])

    def test_review_queue_reports_nonvideo_media(self):
        with patch("medal_highlight_reel.manual_review.resolve_executable", return_value="ffprobe"), \
             patch("medal_highlight_reel.manual_review.probe_media", return_value=(10.0, False, False)):
            queue, skipped, _records, _digest = build_review_queue(self.clips, self.bookmarks, self.corrections)
        self.assertEqual(queue, [])
        self.assertEqual(skipped[0]["reason"], "source_has_no_video_stream")


if __name__ == "__main__":
    unittest.main()
