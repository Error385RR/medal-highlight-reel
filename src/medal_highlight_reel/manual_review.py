"""Review missing bookmark positions in mpv and save resumable corrections."""
from __future__ import annotations

import csv
import datetime as dt
import os
import subprocess
import tempfile
try:
    import tkinter as tk
except ImportError:  # Tk is optional for plan/render-only installations.
    tk = None  # type: ignore[assignment]
from pathlib import Path
from typing import Any

from .ffmpeg_runner import RenderError, probe_media, resolve_executable
from .manual_timestamps import file_sha256, load_manual_records, save_manual_records
from .report_reader import InputReportError, load_reports
from .segment_planner import finite_number, _created_sort_key

if tk is not None:
    from tkinter import messagebox
else:
    messagebox = None


SKIP_FIELDS = ["bookmark_row_number", "local_content_id", "bookmark_name", "video_path", "reason"]


class ReviewError(ValueError):
    pass


def build_review_queue(clips_path: Path, bookmarks_path: Path, corrections_path: Path,
                       ffprobe_bin: str = "ffprobe") -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[int, dict[str, str]], str]:
    clip_index, bookmarks, duplicate_clips = load_reports(clips_path, bookmarks_path)
    digest = file_sha256(bookmarks_path)
    corrections = load_manual_records(corrections_path, bookmarks_path, allow_missing=True)
    duplicate_rows = {row.get("local_content_id", "") for row in duplicate_clips}
    skipped: list[dict[str, str]] = []
    queue: list[dict[str, Any]] = []
    media_cache: dict[str, tuple[float, bool, bool] | str] = {}
    probe: str | None = None

    for bookmark in bookmarks:
        if bookmark.get("position_status", "").strip().upper() != "MISSING_TIMING_DATA":
            continue
        row_number = int(bookmark["__source_row_number"])
        clip_id = bookmark.get("local_content_id", "")
        name = bookmark.get("bookmark_name", "")
        def skip(reason: str, video_path: str = "") -> None:
            skipped.append({"bookmark_row_number": str(row_number), "local_content_id": clip_id,
                            "bookmark_name": name, "video_path": video_path, "reason": reason})
        if clip_id in duplicate_rows:
            skip("duplicate_clip_id_ambiguous"); continue
        clip = clip_index.get(clip_id)
        if clip is None:
            skip("missing_clip_record"); continue
        video = clip.get("video_path", "").strip()
        if not video:
            skip("missing_video_path"); continue
        try:
            source = Path(video).expanduser()
            if not source.is_file():
                skip("video_file_missing", video); continue
            source_key = str(source.resolve()).casefold()
        except (OSError, ValueError):
            skip("video_path_unreadable", video); continue
        duration_from_report = finite_number(clip.get("clip_duration_seconds"))
        if duration_from_report is None or duration_from_report <= 0:
            skip("missing_or_invalid_clip_duration", video); continue
        prior = corrections.get(row_number)
        if prior:
            if (prior.get("local_content_id") != clip_id or prior.get("bookmark_name", "") != name
                    or prior.get("video_path", "") != video
                    or prior.get("status") != "MANUALLY_MARKED"):
                raise InputReportError(f"saved manual mark no longer matches bookmark row {row_number}")
            saved_position = finite_number(prior.get("position_seconds"))
            if saved_position is not None and 0 <= saved_position <= duration_from_report:
                continue
        result = media_cache.get(source_key)
        if result is None:
            if probe is None:
                probe = resolve_executable(ffprobe_bin)
            try:
                result = probe_media(probe, source)
            except RenderError as exc:
                result = str(exc)
            media_cache[source_key] = result
        if isinstance(result, str):
            skip("media_probe_failed: " + result, video); continue
        actual_duration, has_video, _has_audio = result
        if not has_video:
            skip("source_has_no_video_stream", video); continue
        duration = min(duration_from_report, actual_duration)
        queue.append({"bookmark_row_number": row_number, "local_content_id": clip_id,
                      "bookmark_name": name, "video_path": str(source), "source_video_path": video,
                      "game": clip.get("game", "Unknown"),
                      "created_at_utc": clip.get("created_at_utc", ""), "duration": duration,
                      "actual_duration": actual_duration})

    queue.sort(key=lambda item: (_created_sort_key(item["created_at_utc"]), item["local_content_id"], item["bookmark_row_number"]))
    return queue, skipped, corrections, digest


def _lua_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r") + '"'


def _write_mpv_script(path: Path, marks_path: Path, request_path: Path) -> None:
    script = f'''local mark_path = {_lua_string(str(marks_path))}
local request_path = {_lua_string(str(request_path))}

local function mark_position()
    local position = mp.get_property_number("time-pos")
    if not position then
        mp.osd_message("Wait until the video starts, then mark again", 2)
        return
    end
    local file = io.open(mark_path, "a")
    if not file then
        mp.osd_message("Could not save mark", 2)
        return
    end
    file:write(string.format("%.9f\\n", position))
    file:flush()
    file:close()
    mp.osd_message(string.format("Manual bookmark marked at %.3f seconds", position), 2)
end

mp.add_forced_key_binding("m", "medal-highlight-mark", mark_position)
mp.add_periodic_timer(0.1, function()
    local request = io.open(request_path, "r")
    if request then
        request:close()
        os.remove(request_path)
        mark_position()
    end
end)
'''
    path.write_text(script, encoding="utf-8")


def _write_skips(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=SKIP_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


class ManualReviewWindow:
    def __init__(self, root: tk.Tk, queue: list[dict[str, Any]], corrections: dict[int, dict[str, str]],
                 digest: str, output_path: Path, mpv_bin: str):
        self.root = root
        self.queue = queue
        self.corrections = corrections
        self.digest = digest
        self.output_path = output_path
        self.mpv_bin = mpv_bin
        self.index = 0
        self.process: subprocess.Popen[Any] | None = None
        self.player_path: str | None = None
        self.temporary = tempfile.TemporaryDirectory(prefix="medal-highlight-review-", dir=output_path.parent)
        self.temp_path = Path(self.temporary.name)
        self.marks_path = self.temp_path / "marks.txt"
        self.request_path = self.temp_path / "mark.request"
        self.script_path = self.temp_path / "manual_mark.lua"
        self.marks_path.touch()
        _write_mpv_script(self.script_path, self.marks_path, self.request_path)
        self.read_offset = 0
        self._pending_navigation: int | None = None

        root.title("Medal Highlight Reel — Manual Bookmark Review")
        root.geometry("660x270")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.header = tk.Label(root, font=("Segoe UI", 12, "bold"), anchor="w")
        self.header.pack(fill="x", padx=16, pady=(16, 4))
        self.details = tk.Label(root, justify="left", anchor="w", wraplength=620)
        self.details.pack(fill="x", padx=16, pady=4)
        self.status = tk.Label(root, anchor="w", fg="#245a32")
        self.status.pack(fill="x", padx=16, pady=8)
        instructions = tk.Label(root, text="Pause/seek in mpv, then press M in the player or click Mark current time.", anchor="w")
        instructions.pack(fill="x", padx=16, pady=4)
        controls = tk.Frame(root)
        controls.pack(fill="x", padx=16, pady=10)
        tk.Button(controls, text="Previous", command=lambda: self.navigate(-1)).pack(side="left", padx=(0, 8))
        tk.Button(controls, text="Mark current time", command=self.request_mark).pack(side="left", padx=4)
        tk.Button(controls, text="Skip", command=self.skip).pack(side="left", padx=4)
        tk.Button(controls, text="Next", command=lambda: self.navigate(1)).pack(side="left", padx=4)
        tk.Button(controls, text="Reopen mpv", command=self.start_player).pack(side="right")
        self.show_current()
        self.root.after(150, self.poll_marks)

    def show_current(self) -> None:
        if self.index >= len(self.queue):
            self.header.config(text="Review complete")
            self.details.config(text=f"Saved manual timestamps: {len(self.corrections)}\nCorrections: {self.output_path}")
            self.status.config(text="You can now run plan with --manual-timestamps to use these marks.")
            self.stop_player()
            return
        item = self.queue[self.index]
        self.header.config(text=f"Bookmark {self.index + 1} of {len(self.queue)}  ·  {item['game']}  ·  {item['bookmark_name'] or '(unnamed bookmark)'}")
        self.details.config(text=f"Clip: {item['local_content_id']}   ·   Bookmark row: {item['bookmark_row_number']}\n"
                                 f"Video: {item['video_path']}\nClip duration: {item['duration']:.3f} seconds")
        prior = self.corrections.get(item["bookmark_row_number"])
        if prior:
            self.status.config(text=f"Saved mark: {prior.get('position_seconds')} seconds (mark again to replace)")
        else:
            self.status.config(text="No manual time saved for this bookmark yet.")
        if self.player_path != item["video_path"]:
            self.start_player()

    def start_player(self) -> None:
        if self.index >= len(self.queue):
            return
        self.stop_player()
        item = self.queue[self.index]
        try:
            self.process = subprocess.Popen([self.mpv_bin, "--no-config", "--force-window=yes", "--keep-open=yes",
                                             f"--script={self.script_path}", "--", item["video_path"]],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.player_path = item["video_path"]
            self.status.config(text="mpv opened. Pause/seek, then press M in mpv or use Mark current time.")
        except OSError as exc:
            messagebox.showerror("Could not open mpv", str(exc), parent=self.root)
            self.process = None
            self.player_path = None

    def stop_player(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        self.player_path = None

    def request_mark(self) -> None:
        if not self.process or self.process.poll() is not None:
            self.status.config(text="mpv is closed. Use Reopen mpv before marking.")
            return
        if self.request_path.exists():
            self.status.config(text="mpv is still processing the last mark request.")
            return
        try:
            self.request_path.write_text("mark", encoding="ascii")
            self.status.config(text="Waiting for mpv to report the current playback position…")
        except OSError as exc:
            self.status.config(text=f"Could not request a mark: {exc}")

    def poll_marks(self) -> None:
        self._drain_marks()
        if self.process and self.process.poll() is not None and self.index < len(self.queue):
            self.status.config(text="mpv is closed. Reopen it to continue marking this bookmark.")
        if self._pending_navigation is not None and (not self.request_path.exists()
                or not self.process or self.process.poll() is not None):
            try:
                self.request_path.unlink(missing_ok=True)
            except OSError:
                pass
            direction, self._pending_navigation = self._pending_navigation, None
            self._navigate_now(direction)
        if self.root.winfo_exists():
            self.root.after(150, self.poll_marks)

    def _drain_marks(self) -> None:
        try:
            with self.marks_path.open("r", encoding="ascii") as stream:
                stream.seek(self.read_offset)
                lines = stream.readlines()
                self.read_offset = stream.tell()
        except OSError:
            lines = []
        for line in lines:
            position = finite_number(line.strip())
            if position is not None:
                self.save_mark(position)

    def save_mark(self, position: float) -> None:
        if self.index >= len(self.queue):
            return
        item = self.queue[self.index]
        if position < 0 or position > item["duration"]:
            self.status.config(text=f"mpv position {position:.3f}s is outside the clip; no mark saved.")
            return
        row_number = item["bookmark_row_number"]
        previous = self.corrections.get(row_number)
        self.corrections[row_number] = {
            "bookmarks_csv_sha256": self.digest,
            "bookmark_row_number": str(row_number),
            "local_content_id": item["local_content_id"],
            "bookmark_name": item["bookmark_name"],
            "video_path": item["source_video_path"],
            "position_seconds": f"{position:.9f}",
            "source_duration_seconds": f"{item['duration']:.9f}",
            "marked_at_utc": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "status": "MANUALLY_MARKED",
        }
        try:
            save_manual_records(self.output_path, self.corrections)
            self.status.config(text=f"Saved mark at {position:.3f} seconds. Mark again to replace it.")
        except (OSError, csv.Error) as exc:
            if previous is None:
                self.corrections.pop(row_number, None)
            else:
                self.corrections[row_number] = previous
            messagebox.showerror("Could not save manual timestamp", str(exc), parent=self.root)

    def navigate(self, direction: int) -> None:
        self._drain_marks()
        if self.request_path.exists():
            self._pending_navigation = direction
            return
        self._navigate_now(direction)

    def _navigate_now(self, direction: int) -> None:
        if direction < 0 and self.index >= len(self.queue):
            self.index = max(0, len(self.queue) - 1)
        else:
            self.index = max(0, min(len(self.queue), self.index + direction))
        self.show_current()

    def skip(self) -> None:
        self.navigate(1)

    def close(self) -> None:
        self.stop_player()
        self.temporary.cleanup()
        self.root.destroy()


def run_manual_review(clips_path: Path, bookmarks_path: Path, output_path: Path,
                      mpv_bin: str = "mpv", ffprobe_bin: str = "ffprobe") -> tuple[int, int, Path]:
    output_path = output_path.expanduser().resolve()
    if output_path.suffix.casefold() != ".csv":
        raise ReviewError("manual timestamp output filename must end in .csv")
    protected = {clips_path.expanduser().resolve(), bookmarks_path.expanduser().resolve()}
    if output_path in protected or output_path.with_name(output_path.stem + "_review_skipped.csv") in protected:
        raise ReviewError("manual timestamp outputs must not overwrite either input report")
    clip_index, _bookmark_rows, duplicate_clips = load_reports(clips_path, bookmarks_path)
    for clip in [*clip_index.values(), *duplicate_clips]:
        try:
            protected.add(Path(clip.get("video_path", "")).expanduser().resolve())
        except (OSError, ValueError):
            continue
    queue, skipped, corrections, digest = build_review_queue(clips_path, bookmarks_path, output_path, ffprobe_bin)
    if output_path in protected or output_path.with_name(output_path.stem + "_review_skipped.csv") in protected:
        raise ReviewError("manual timestamp outputs must not overwrite a source video")
    skip_path = output_path.with_name(output_path.stem + "_review_skipped.csv")
    _write_skips(skip_path, skipped)
    if not queue:
        return len(corrections), len(skipped), skip_path
    if tk is None:
        raise ReviewError("this Python installation does not include Tkinter, which the manual review window needs")
    mpv = resolve_executable(mpv_bin)
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise ReviewError(f"could not start the review window: {exc}") from exc
    window = ManualReviewWindow(root, queue, corrections, digest, output_path, mpv)
    root.mainloop()
    return len(corrections), len(skipped), skip_path
