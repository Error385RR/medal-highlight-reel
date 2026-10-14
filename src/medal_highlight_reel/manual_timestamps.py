"""Stable, auditable storage for manually recovered bookmark positions."""
from __future__ import annotations

import csv
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

from .report_reader import InputReportError


MANUAL_FIELDS = [
    "bookmarks_csv_sha256", "bookmark_row_number", "local_content_id", "bookmark_name",
    "video_path", "position_seconds", "source_duration_seconds", "marked_at_utc", "status",
]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise InputReportError(f"cannot read report {path}: {exc}") from exc
    return digest.hexdigest()


def load_manual_records(path: Path | None, bookmarks_path: Path, allow_missing: bool = False) -> dict[int, dict[str, str]]:
    if path is None:
        return {}
    if not path.exists():
        if allow_missing:
            return {}
        raise InputReportError(f"manual timestamp file does not exist: {path}")
    digest = file_sha256(bookmarks_path)
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            missing = set(MANUAL_FIELDS) - set(reader.fieldnames or [])
            if missing:
                raise InputReportError(f"manual timestamp file is missing column(s): {', '.join(sorted(missing))}")
            records: dict[int, dict[str, str]] = {}
            for row in reader:
                if row.get("bookmarks_csv_sha256", "").strip().casefold() != digest:
                    raise InputReportError("manual timestamps were made from a different bookmarks.csv; review the current report again")
                try:
                    row_number = int(row.get("bookmark_row_number", ""))
                except (TypeError, ValueError) as exc:
                    raise InputReportError("manual timestamp file contains an invalid bookmark row number") from exc
                if row_number < 1 or row_number in records:
                    raise InputReportError("manual timestamp file contains an invalid or duplicate bookmark row number")
                records[row_number] = {key: (value or "").strip() for key, value in row.items() if key is not None}
    except (OSError, UnicodeError, csv.Error) as exc:
        raise InputReportError(f"cannot read manual timestamp file {path}: {exc}") from exc
    return records


def save_manual_records(path: Path, records: dict[int, dict[str, Any]]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=MANUAL_FIELDS)
            writer.writeheader()
            for row_number in sorted(records):
                writer.writerow(records[row_number])
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
