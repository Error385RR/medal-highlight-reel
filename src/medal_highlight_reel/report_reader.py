"""Read and validate the clip/bookmark CSV exports from medal-audit."""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any


class InputReportError(ValueError):
    pass


def read_csv(path: Path, required: set[str]) -> list[dict[str, str]]:
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            headers = set(reader.fieldnames or [])
            missing = required - headers
            if missing:
                raise InputReportError(f"{path} is missing required column(s): {', '.join(sorted(missing))}")
            return [{key: (value or "").strip() for key, value in row.items() if key is not None}
                    for row in reader]
    except (OSError, UnicodeError, csv.Error) as exc:
        raise InputReportError(f"cannot read report {path}: {exc}") from exc


def load_reports(clips_path: Path, bookmarks_path: Path) -> tuple[dict[str, dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    clips = read_csv(clips_path, {"local_content_id", "video_path", "game", "clip_duration_seconds"})
    bookmarks = read_csv(bookmarks_path, {"local_content_id", "bookmark_name", "candidate_recovered_position_seconds", "position_status"})
    for row_number, row in enumerate(bookmarks, start=1):
        row["__source_row_number"] = str(row_number)
    index: dict[str, dict[str, str]] = {}
    duplicate_ids: set[str] = set()
    for row in clips:
        clip_id = row["local_content_id"].strip()
        if not clip_id:
            continue
        if clip_id in index:
            duplicate_ids.add(clip_id)
        else:
            index[clip_id] = row
    duplicate_clips = [row for row in clips if row.get("local_content_id", "") in duplicate_ids]
    for clip_id in duplicate_ids:
        index.pop(clip_id, None)
    return index, bookmarks, duplicate_clips


def parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().casefold()
    if text in {"1", "true", "yes", "y"}:
        return True
    if text in {"0", "false", "no", "n"}:
        return False
    return None
