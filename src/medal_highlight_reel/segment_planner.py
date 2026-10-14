"""Select eligible bookmark rows and turn them into bounded, merged segments."""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
from typing import Any

from .report_reader import InputReportError, load_reports


class PlanError(ValueError):
    pass


def finite_number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _seconds(value: Any, label: str) -> float:
    result = finite_number(value)
    if result is None or result < 0:
        raise PlanError(f"{label} must be a finite number greater than or equal to zero")
    return result


def load_window_config(path: Path | None, before_override: float | None = None,
                       after_override: float | None = None) -> dict[str, Any]:
    config: dict[str, Any] = {"default": {"before_seconds": 10.0, "after_seconds": 5.0}, "bookmarks": {}}
    if path:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise PlanError(f"cannot read window config {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise PlanError(f"invalid JSON in window config {path}: {exc}") from exc
        if not isinstance(loaded, dict) or not isinstance(loaded.get("default"), dict):
            raise PlanError("window config must have a 'default' object")
        bookmark_config = loaded.get("bookmarks", {})
        if not isinstance(bookmark_config, dict) or any(not isinstance(v, dict) for v in bookmark_config.values()):
            raise PlanError("window config 'bookmarks' must map names to objects")
        config = loaded
    default = config["default"]
    default_before = before_override if before_override is not None else default.get("before_seconds", 10)
    default_after = after_override if after_override is not None else default.get("after_seconds", 5)
    config["default"] = {"before_seconds": _seconds(default_before, "default.before_seconds"),
                          "after_seconds": _seconds(default_after, "default.after_seconds")}
    normalized: dict[str, dict[str, float]] = {}
    for name, values in config.get("bookmarks", {}).items():
        key = str(name).casefold()
        normalized[key] = {
            "before_seconds": _seconds(values.get("before_seconds", config["default"]["before_seconds"]), f"{name}.before_seconds"),
            "after_seconds": _seconds(values.get("after_seconds", config["default"]["after_seconds"]), f"{name}.after_seconds"),
        }
    config["bookmarks"] = normalized
    return config


def _created_sort_key(value: str) -> tuple[int, str]:
    if not value:
        return (1, "")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (0, parsed.astimezone(dt.timezone.utc).isoformat())
    except (ValueError, OverflowError):
        return (1, value)


def create_plan(clips_path: Path, bookmarks_path: Path, config: dict[str, Any],
                games: set[str] | None = None, bookmark_names: set[str] | None = None,
                include_out_of_range: bool = False) -> tuple[dict[str, Any], list[dict[str, str]]]:
    try:
        clip_index, bookmark_rows, duplicate_clips = load_reports(clips_path, bookmarks_path)
    except InputReportError:
        raise
    rejects: list[dict[str, str]] = []
    for row in duplicate_clips:
        rejects.append({"local_content_id": row.get("local_content_id", ""), "bookmark_name": "",
                        "video_path": row.get("video_path", ""), "reason": "duplicate_clip_id"})
    selected_games = {g.casefold() for g in games or set()}
    selected_names = {n.casefold() for n in bookmark_names or set()}
    eligible: list[dict[str, Any]] = []
    for bookmark in bookmark_rows:
        clip_id = bookmark.get("local_content_id", "")
        clip = clip_index.get(clip_id)
        name = bookmark.get("bookmark_name", "")
        video = (clip or {}).get("video_path", "")
        def reject(reason: str) -> None:
            rejects.append({"local_content_id": clip_id, "bookmark_name": name, "video_path": video, "reason": reason})
        if clip is None:
            reject("missing_or_ambiguous_clip_record"); continue
        if selected_games and clip.get("game", "").casefold() not in selected_games:
            reject("game_filter"); continue
        if selected_names and name.casefold() not in selected_names:
            reject("bookmark_name_filter"); continue
        if not video:
            reject("missing_video_path"); continue
        try:
            path = Path(video).expanduser()
        except (OSError, ValueError):
            reject("video_path_unreadable"); continue
        try:
            if not path.is_file():
                reject("video_file_missing"); continue
        except (OSError, ValueError):
            reject("video_path_unreadable"); continue
        status = bookmark.get("position_status", "").strip().upper()
        if status == "MISSING_TIMING_DATA":
            reject("missing_timing_data"); continue
        if status == "OUT_OF_RANGE" and not include_out_of_range:
            reject("out_of_range"); continue
        if status not in {"IN_RANGE", "OUT_OF_RANGE"}:
            reject("unsupported_position_status"); continue
        position = finite_number(bookmark.get("candidate_recovered_position_seconds"))
        duration = finite_number(clip.get("clip_duration_seconds"))
        if position is None:
            reject("invalid_candidate_position"); continue
        if position < 0 and not include_out_of_range:
            reject("position_before_clip_start"); continue
        if duration is None or duration <= 0:
            reject("missing_or_invalid_clip_duration"); continue
        if position > duration and not include_out_of_range:
            reject("position_exceeds_clip_duration"); continue
        window = config["bookmarks"].get(name.casefold(), config["default"])
        start = max(0.0, position - window["before_seconds"])
        end = min(duration, position + window["after_seconds"])
        if end <= start:
            reject("empty_segment_after_clamping"); continue
        eligible.append({"clip": clip, "clip_id": clip_id, "bookmark": bookmark, "name": name,
                         "position": position, "duration": duration, "start": start, "end": end})

    eligible.sort(key=lambda e: (_created_sort_key(e["clip"].get("created_at_utc", "")),
                                 e["clip_id"], e["start"], e["end"]))
    segments: list[dict[str, Any]] = []
    by_clip: dict[str, list[dict[str, Any]]] = {}
    for event in eligible:
        by_clip.setdefault(event["clip_id"], []).append(event)
    ordered_groups = sorted(by_clip.values(), key=lambda group: (_created_sort_key(group[0]["clip"].get("created_at_utc", "")), group[0]["clip_id"]))
    for group in ordered_groups:
        group.sort(key=lambda e: (e["start"], e["end"]))
        current: dict[str, Any] | None = None
        for event in group:
            if current is None or event["start"] > current["source_end_seconds"]:
                if current:
                    segments.append(current)
                clip = event["clip"]
                current = {"segment_id": "", "local_content_id": event["clip_id"],
                    "created_at_utc": clip.get("created_at_utc", ""), "game": clip.get("game", "Unknown"),
                    "video_path": clip["video_path"], "source_duration_seconds": event["duration"],
                    "source_start_seconds": event["start"], "source_end_seconds": event["end"],
                    "bookmarks": [event_record(event)]}
            else:
                current["source_end_seconds"] = max(current["source_end_seconds"], event["end"])
                current["bookmarks"].append(event_record(event))
        if current:
            segments.append(current)
    for i, segment in enumerate(segments, start=1):
        segment["segment_id"] = f"seg-{i:05d}"
        segment["duration_seconds"] = round(segment["source_end_seconds"] - segment["source_start_seconds"], 6)
        segment["source_start_seconds"] = round(segment["source_start_seconds"], 6)
        segment["source_end_seconds"] = round(segment["source_end_seconds"], 6)
    plan = {"schema_version": 1, "segments": segments,
            "options": {"games": sorted(selected_games), "bookmark_names": sorted(selected_names),
                        "include_out_of_range": include_out_of_range,
                        "window_defaults": config["default"], "bookmark_windows": config["bookmarks"]},
            "summary": {"segments": len(segments), "selected_bookmark_rows": len(eligible),
                        "merged_or_selected_clip_count": len({e["clip_id"] for e in eligible}),
                        "rejected_bookmark_rows": len(rejects)}}
    return plan, rejects


def event_record(event: dict[str, Any]) -> dict[str, Any]:
    bookmark = event["bookmark"]
    return {"name": event["name"], "candidate_position_seconds": round(event["position"], 6),
            "position_status": bookmark.get("position_status", ""),
            "manual_verification_status": bookmark.get("manual_verification_status", "UNVERIFIED"),
            "event_timestamp": bookmark.get("bookmark_timestamp", "")}
