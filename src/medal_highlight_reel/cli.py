"""Command-line entry point for Medal Highlight Reel."""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from .ffmpeg_runner import RenderError, render_plan
from .report_reader import InputReportError
from .segment_planner import PlanError, create_plan, load_window_config


def _write_text_atomic(path: Path, content: str, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"file already exists: {path} (use --overwrite)")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _write_csv_atomic(path: Path, rows: list[dict[str, Any]], fields: list[str], overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"file already exists: {path} (use --overwrite)")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _segments_csv(plan: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for segment in plan["segments"]:
        rows.append({"segment_id": segment["segment_id"], "local_content_id": segment["local_content_id"],
                     "created_at_utc": segment["created_at_utc"], "game": segment["game"],
                     "video_path": segment["video_path"], "source_start_seconds": segment["source_start_seconds"],
                     "source_end_seconds": segment["source_end_seconds"], "duration_seconds": segment["duration_seconds"],
                     "bookmarks": json.dumps(segment["bookmarks"], ensure_ascii=False, separators=(",", ":"))})
    return rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="medal-highlights", description="Build reviewable highlight plans and render videos from Medal CSV reports.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan", help="Join the reports and create a bounded, auditable segment plan")
    plan.add_argument("--clips", required=True, type=Path, help="Path to clips.csv")
    plan.add_argument("--bookmarks", required=True, type=Path, help="Path to bookmarks.csv")
    plan.add_argument("--config", type=Path, help="JSON file with default and bookmark-specific surround times")
    plan.add_argument("--before-seconds", type=float, help="Override the default lead-in from the config")
    plan.add_argument("--after-seconds", type=float, help="Override the default lead-out from the config")
    plan.add_argument("--game", action="append", help="Keep only this game; repeat to select multiple")
    plan.add_argument("--bookmark-name", action="append", help="Keep only this bookmark name; repeat to select multiple")
    plan.add_argument("--include-out-of-range", action="store_true", help="Allow candidate positions marked OUT_OF_RANGE")
    plan.add_argument("--output-dir", required=True, type=Path, help="Directory for plan.json, segments.csv, and rejected.csv")
    plan.add_argument("--preview", action="store_true", help="Show planned and rejected counts without writing files")
    plan.add_argument("--overwrite", action="store_true", help="Replace existing plan files")

    render = subparsers.add_parser("render", help="Render a plan to a normalized MP4 using FFmpeg")
    render.add_argument("--plan", required=True, type=Path, help="plan.json created by the plan command")
    render.add_argument("--output", required=True, type=Path, help="Final .mp4 path")
    render.add_argument("--ffmpeg", default="ffmpeg", help="FFmpeg executable or path (default: ffmpeg on PATH)")
    render.add_argument("--ffprobe", default="ffprobe", help="ffprobe executable or path (default: ffprobe on PATH)")
    render.add_argument("--width", type=int, default=1920)
    render.add_argument("--height", type=int, default=1080)
    render.add_argument("--fps", type=int, default=30)
    render.add_argument("--overwrite", action="store_true", help="Replace existing MP4 and skipped report")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "plan":
            config = load_window_config(args.config, args.before_seconds, args.after_seconds)
            plan, rejects = create_plan(args.clips, args.bookmarks, config,
                set(args.game or []), set(args.bookmark_name or []), args.include_out_of_range)
            summary = plan["summary"]
            if args.preview:
                print(f"Preview: {summary['segments']} segment(s) from {summary['selected_bookmark_rows']} bookmark row(s); "
                      f"{summary['rejected_bookmark_rows']} row(s) skipped. No files written.")
                if rejects:
                    counts: dict[str, int] = {}
                    for row in rejects:
                        counts[row["reason"]] = counts.get(row["reason"], 0) + 1
                    print("Skipped by reason: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
                return 0
            output = args.output_dir
            output.mkdir(parents=True, exist_ok=True)
            outputs = [output / "plan.json", output / "segments.csv", output / "rejected.csv"]
            if not args.overwrite:
                existing = [path for path in outputs if path.exists()]
                if existing:
                    raise FileExistsError("output already exists: " + ", ".join(map(str, existing)) + " (use --overwrite)")
            _write_text_atomic(outputs[0], json.dumps(plan, indent=2, ensure_ascii=False) + "\n", args.overwrite)
            segment_fields = ["segment_id", "local_content_id", "created_at_utc", "game", "video_path",
                "source_start_seconds", "source_end_seconds", "duration_seconds", "bookmarks"]
            _write_csv_atomic(outputs[1], _segments_csv(plan), segment_fields, args.overwrite)
            reject_fields = ["local_content_id", "bookmark_name", "video_path", "reason"]
            _write_csv_atomic(outputs[2], rejects, reject_fields, args.overwrite)
            print(f"Wrote {summary['segments']} segment(s) to {output} (plan.json, segments.csv, rejected.csv)")
        else:
            count, skipped, skipped_path = render_plan(args.plan, args.output, args.ffmpeg, args.ffprobe,
                args.overwrite, args.width, args.height, args.fps)
            print(f"Rendered {count} segment(s) to {args.output}; skipped {len(skipped)}. Details: {skipped_path}")
    except (InputReportError, PlanError, RenderError, OSError, ValueError) as exc:
        print(f"medal-highlights: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
