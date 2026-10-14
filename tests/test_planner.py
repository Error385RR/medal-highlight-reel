import csv
import json
import tempfile
import unittest
from pathlib import Path

from medal_highlight_reel.segment_planner import PlanError, create_plan, load_window_config


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        self.video = self.root / "clip.mp4"
        self.video.write_bytes(b"placeholder; planner only verifies file presence")
        self.clips = self.root / "clips.csv"
        self.bookmarks = self.root / "bookmarks.csv"
        self._write_reports()

    def tearDown(self):
        self.temp.cleanup()

    def _write_reports(self, clips=None, bookmarks=None):
        clip_fields = ["local_content_id", "created_at_utc", "game", "video_path", "clip_duration_seconds"]
        bookmark_fields = ["local_content_id", "bookmark_name", "candidate_recovered_position_seconds", "position_status", "bookmark_timestamp", "manual_verification_status"]
        clips = clips if clips is not None else [{"local_content_id":"a", "created_at_utc":"2026-01-01T00:00:00Z", "game":"Valorant", "video_path":str(self.video), "clip_duration_seconds":"20"}]
        bookmarks = bookmarks if bookmarks is not None else [
            {"local_content_id":"a", "bookmark_name":"Kill", "candidate_recovered_position_seconds":"1", "position_status":"IN_RANGE", "bookmark_timestamp":"", "manual_verification_status":"UNVERIFIED"},
            {"local_content_id":"a", "bookmark_name":"Kill", "candidate_recovered_position_seconds":"18", "position_status":"IN_RANGE", "bookmark_timestamp":"", "manual_verification_status":"UNVERIFIED"},
        ]
        for path, fields, rows in [(self.clips, clip_fields, clips), (self.bookmarks, bookmark_fields, bookmarks)]:
            with path.open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)

    def _plan(self, **kwargs):
        return create_plan(self.clips, self.bookmarks, load_window_config(None), **kwargs)

    def test_clamps_windows_at_clip_edges_and_merges_overlap(self):
        plan, rejected = self._plan()
        self.assertEqual(rejected, [])
        self.assertEqual(len(plan["segments"]), 2)  # separated events remain separate
        self.assertEqual(plan["segments"][0]["source_start_seconds"], 0.0)
        self.assertEqual(plan["segments"][0]["source_end_seconds"], 6.0)
        self.assertEqual(plan["segments"][1]["source_end_seconds"], 20.0)

    def test_overlapping_windows_merge_and_keep_both_events(self):
        rows = [
            {"local_content_id":"a", "bookmark_name":"Kill", "candidate_recovered_position_seconds":"10", "position_status":"IN_RANGE"},
            {"local_content_id":"a", "bookmark_name":"ACE", "candidate_recovered_position_seconds":"12", "position_status":"IN_RANGE"},
        ]
        self._write_reports(bookmarks=rows)
        plan, _ = self._plan()
        self.assertEqual(len(plan["segments"]), 1)
        self.assertEqual(len(plan["segments"][0]["bookmarks"]), 2)
        self.assertEqual(plan["segments"][0]["source_start_seconds"], 0.0)
        self.assertEqual(plan["segments"][0]["source_end_seconds"], 17.0)

    def test_missing_source_and_missing_timing_are_distinct_rejections(self):
        rows = [
            {"local_content_id":"a", "bookmark_name":"Kill", "candidate_recovered_position_seconds":"5", "position_status":"IN_RANGE"},
            {"local_content_id":"a", "bookmark_name":"Kill", "candidate_recovered_position_seconds":"", "position_status":"MISSING_TIMING_DATA"},
        ]
        self.video.unlink()
        self._write_reports(bookmarks=rows)
        _, rejected = self._plan()
        self.assertEqual([r["reason"] for r in rejected], ["video_file_missing", "video_file_missing"])
        self.video.write_bytes(b"exists")
        _, rejected = self._plan()
        self.assertEqual([r["reason"] for r in rejected], ["missing_timing_data"])

    def test_out_of_range_is_opt_in_and_clamps(self):
        rows = [{"local_content_id":"a", "bookmark_name":"Kill", "candidate_recovered_position_seconds":"-2", "position_status":"OUT_OF_RANGE"}]
        self._write_reports(bookmarks=rows)
        _, rejected = self._plan()
        self.assertEqual(rejected[0]["reason"], "out_of_range")
        plan, rejected = self._plan(include_out_of_range=True)
        self.assertEqual(rejected, [])
        self.assertEqual(plan["segments"][0]["source_start_seconds"], 0.0)
        self.assertEqual(plan["segments"][0]["source_end_seconds"], 3.0)

    def test_duplicate_clip_ids_and_orphan_bookmarks_are_rejected(self):
        clip = {"local_content_id":"a", "created_at_utc":"", "game":"Valorant", "video_path":str(self.video), "clip_duration_seconds":"20"}
        orphan = {"local_content_id":"missing", "bookmark_name":"Kill", "candidate_recovered_position_seconds":"5", "position_status":"IN_RANGE"}
        self._write_reports(clips=[clip, clip], bookmarks=[orphan])
        plan, rejected = self._plan()
        self.assertEqual(plan["segments"], [])
        self.assertEqual({row["reason"] for row in rejected}, {"duplicate_clip_id", "missing_or_ambiguous_clip_record"})

    def test_filters_are_case_insensitive_and_overrides_apply(self):
        config_path = self.root / "windows.json"
        config_path.write_text(json.dumps({"default":{"before_seconds":2,"after_seconds":1},"bookmarks":{"kill":{"before_seconds":4}}}), encoding="utf-8")
        config = load_window_config(config_path)
        plan, rejected = create_plan(self.clips, self.bookmarks, config, games={"valorant"}, bookmark_names={"KILL"})
        self.assertEqual(plan["segments"][0]["source_start_seconds"], 0.0)
        self.assertEqual(plan["segments"][0]["source_end_seconds"], 2.0)
        self.assertFalse(rejected)

    def test_invalid_config_numbers_are_rejected(self):
        path = self.root / "bad.json"
        path.write_text('{"default":{"before_seconds":-1}}', encoding="utf-8")
        with self.assertRaises(PlanError):
            load_window_config(path)


if __name__ == "__main__":
    unittest.main()
