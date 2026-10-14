"""Interactively adjust planned segment boundaries in mpv."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

try:
    import tkinter as tk
except ImportError:  # Tk is optional for plan/render-only installations.
    tk = None  # type: ignore[assignment]
if tk is not None:
    from tkinter import messagebox
else:
    messagebox = None

from .ffmpeg_runner import RenderError, probe_media, resolve_executable, resolve_mpv_executable
from .segment_planner import finite_number


class RefinementError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RefinementError(f"cannot read source plan {path}: {exc}") from exc
    return digest.hexdigest()


def _read_plan(path: Path) -> dict[str, Any]:
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RefinementError(f"cannot read plan {path}: {exc}") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RefinementError(f"invalid plan JSON in {path}: {exc}") from exc
    if not isinstance(plan, dict) or not isinstance(plan.get("segments"), list):
        raise RefinementError("plan must be a JSON object containing a segments list")
    for index, segment in enumerate(plan["segments"], start=1):
        if not isinstance(segment, dict):
            raise RefinementError(f"segment {index} is not an object")
        if not segment.get("video_path"):
            raise RefinementError(f"segment {index} has no video_path")
        start = finite_number(segment.get("source_start_seconds"))
        end = finite_number(segment.get("source_end_seconds"))
        duration = finite_number(segment.get("source_duration_seconds"))
        if start is None or end is None or duration is None or duration <= 0 or start < 0 or end <= start:
            raise RefinementError(f"segment {index} has invalid source bounds or duration")
    return plan


def _atomic_write(path: Path, plan: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            json.dump(plan, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _load_or_create(source: Path, output: Path) -> dict[str, Any]:
    source = source.expanduser().resolve()
    output = output.expanduser().resolve()
    if source == output:
        raise RefinementError("refined output must be a separate file from the source plan")
    digest = _sha256(source)
    if output.exists():
        plan = _read_plan(output)
        refinement = plan.get("refinement")
        if not isinstance(refinement, dict) or refinement.get("source_plan_sha256") != digest:
            raise RefinementError("existing refined plan came from a different source plan; choose another output path")
        return plan
    plan = _read_plan(source)
    plan = copy.deepcopy(plan)
    plan["refinement"] = {"source_plan": str(source), "source_plan_sha256": digest,
                          "status": "IN_PROGRESS", "last_saved_utc": None,
                          "current_segment_index": 0}
    for segment in plan["segments"]:
        segment["duration_seconds"] = round(float(segment["source_end_seconds"]) - float(segment["source_start_seconds"]), 6)
        segment["refinement_status"] = "UNCHANGED"
    _atomic_write(output, plan)
    return plan


def _lua_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r") + '"'


def _write_player_script(path: Path, position_path: Path, action_path: Path, loop_path: Path) -> None:
    script = f'''local position_path = {_lua_string(str(position_path))}
local action_path = {_lua_string(str(action_path))}
local loop_path = {_lua_string(str(loop_path))}
local active_boundary = "start"
local last_loop_a = nil
local last_loop_b = nil

local function write_action(action, value)
    local position = mp.get_property_number("time-pos")
    if not position then return end
    local file = io.open(action_path, "a")
    if file then
        if value then
            file:write(action .. "," .. value .. "," .. string.format("%.9f", position) .. "\\n")
        else
            file:write(action .. "," .. string.format("%.9f", position) .. "\\n")
        end
        file:flush()
        file:close()
        mp.osd_message(action .. " at " .. string.format("%.3f", position) .. "s", 1.5)
    end
end

local function update_position()
    local position = mp.get_property_number("time-pos")
    if position then
        local file = io.open(position_path .. ".tmp", "w")
        if file then
            file:write(string.format("%.9f", position))
            file:close()
            os.remove(position_path)
            os.rename(position_path .. ".tmp", position_path)
        end
    end
end

local function update_loop()
    local file = io.open(loop_path, "r")
    if not file then return end
    local line = file:read("*l")
    file:close()
    if not line then return end
    local raw_a, raw_b = line:match("^([%d%.]+),([%d%.]+)$")
    local loop_a = tonumber(raw_a)
    local loop_b = tonumber(raw_b)
    if not loop_a or not loop_b or loop_b <= loop_a then return end
    if loop_a ~= last_loop_a or loop_b ~= last_loop_b then
        mp.set_property_number("ab-loop-a", loop_a)
        mp.set_property_number("ab-loop-b", loop_b)
        last_loop_a = loop_a
        last_loop_b = loop_b
        local position = mp.get_property_number("time-pos")
        if position and (position < loop_a or position > loop_b) then
            mp.commandv("seek", loop_a, "absolute+exact")
        end
    end
end

mp.add_forced_key_binding("a", "refine-set-start", function() write_action("set_start") end)
mp.add_forced_key_binding("b", "refine-set-end", function() write_action("set_end") end)
mp.add_forced_key_binding("s", "refine-select-start", function() active_boundary = "start"; write_action("select_start") end)
mp.add_forced_key_binding("e", "refine-select-end", function() active_boundary = "end"; write_action("select_end") end)
mp.add_forced_key_binding("Ctrl+LEFT", "refine-nudge-left", function() write_action("nudge_" .. active_boundary, "-0.1") end)
mp.add_forced_key_binding("Ctrl+RIGHT", "refine-nudge-right", function() write_action("nudge_" .. active_boundary, "0.1") end)
mp.add_forced_key_binding("Ctrl+Shift+LEFT", "refine-nudge-left-large", function() write_action("nudge_" .. active_boundary, "-1") end)
mp.add_forced_key_binding("Ctrl+Shift+RIGHT", "refine-nudge-right-large", function() write_action("nudge_" .. active_boundary, "1") end)
mp.add_forced_key_binding("Ctrl+n", "refine-next-segment", function() write_action("navigate_next") end)
mp.add_forced_key_binding("Ctrl+p", "refine-previous-segment", function() write_action("navigate_previous") end)
mp.add_periodic_timer(0.1, update_position)
mp.add_periodic_timer(0.2, update_loop)
'''
    path.write_text(script, encoding="utf-8")


def _write_loop_bounds(path: Path, start: float, end: float) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(f"{start:.9f},{end:.9f}\n", encoding="ascii")
    os.replace(temporary, path)


def _format_time(seconds: float) -> str:
    whole_ms = max(0, round(seconds * 1000))
    hours, remainder = divmod(whole_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


class RefinementWindow:
    def __init__(self, root: tk.Tk, plan: dict[str, Any], output: Path,
                 mpv: str, step: float, large_step: float):
        self.root = root
        self.plan = plan
        self.output = output
        self.mpv = mpv
        self.step = step
        self.large_step = large_step
        saved_index = plan.get("refinement", {}).get("current_segment_index", 0)
        self.index = saved_index if isinstance(saved_index, int) and 0 <= saved_index <= len(plan["segments"]) else 0
        self.active_boundary = "start"
        self.position = 0.0
        self.process: subprocess.Popen[Any] | None = None
        self.player_segment: int | None = None
        self.temporary = tempfile.TemporaryDirectory(prefix="medal-highlight-refine-")
        self.temp_path = Path(self.temporary.name)
        self.player_session: Path | None = None
        self.position_path: Path | None = None
        self.action_path: Path | None = None
        self.loop_path: Path | None = None
        self.script_path: Path | None = None
        self.session_number = 0
        self.action_offset = 0

        root.title("Medal Highlight Reel — Segment Refinement")
        root.geometry("780x380")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.header = tk.Label(root, font=("Segoe UI", 12, "bold"), anchor="w")
        self.header.pack(fill="x", padx=16, pady=(14, 5))
        self.details = tk.Label(root, justify="left", anchor="w", wraplength=740)
        self.details.pack(fill="x", padx=16, pady=5)
        self.position_label = tk.Label(root, anchor="w", font=("Consolas", 11))
        self.position_label.pack(fill="x", padx=16, pady=4)
        self.status = tk.Label(root, anchor="w", fg="#245a32")
        self.status.pack(fill="x", padx=16, pady=5)
        tk.Label(root, text=f"In mpv: A/B set boundaries, S/E selects one, Ctrl+Left/Right nudges by {step * 1000:g} ms, Ctrl+Shift+Left/Right uses {large_step:g} s, Ctrl+N/P changes segments. N/P works in this window.",
                 anchor="w", wraplength=740, justify="left").pack(fill="x", padx=16, pady=5)
        controls = tk.Frame(root)
        controls.pack(fill="x", padx=16, pady=8)
        tk.Button(controls, text="Previous segment", command=lambda: self.navigate(-1)).pack(side="left", padx=(0, 6))
        tk.Button(controls, text="Reopen current clip (R)", command=self.start_player).pack(side="right", padx=3)
        tk.Button(controls, text="Set start here (A)", command=lambda: self.set_boundary("start")).pack(side="left", padx=3)
        tk.Button(controls, text="Set end here (B)", command=lambda: self.set_boundary("end")).pack(side="left", padx=3)
        tk.Button(controls, text="Start −", command=lambda: self.nudge("start", -1, self.step)).pack(side="left", padx=3)
        tk.Button(controls, text="Start +", command=lambda: self.nudge("start", 1, self.step)).pack(side="left", padx=3)
        controls2 = tk.Frame(root)
        controls2.pack(fill="x", padx=16, pady=4)
        tk.Button(controls2, text="Select start", command=lambda: self.select_boundary("start")).pack(side="left", padx=3)
        tk.Button(controls2, text="Select end", command=lambda: self.select_boundary("end")).pack(side="left", padx=3)
        tk.Button(controls2, text="End −", command=lambda: self.nudge("end", -1, self.step)).pack(side="left", padx=3)
        tk.Button(controls2, text="End +", command=lambda: self.nudge("end", 1, self.step)).pack(side="left", padx=3)
        tk.Button(controls2, text="Delete segment (D)", command=self.delete_segment).pack(side="left", padx=12)
        tk.Button(controls2, text="Next", command=lambda: self.navigate(1)).pack(side="right", padx=3)
        root.bind("<Left>", lambda _event: self.nudge(self.active_boundary, -1, self.step))
        root.bind("<Right>", lambda _event: self.nudge(self.active_boundary, 1, self.step))
        root.bind("<Shift-Left>", lambda _event: self.nudge(self.active_boundary, -1, self.large_step))
        root.bind("<Shift-Right>", lambda _event: self.nudge(self.active_boundary, 1, self.large_step))
        root.bind("<KeyPress-a>", lambda _event: self.set_boundary("start"))
        root.bind("<KeyPress-b>", lambda _event: self.set_boundary("end"))
        root.bind("<KeyPress-s>", lambda _event: self.select_boundary("start"))
        root.bind("<KeyPress-e>", lambda _event: self.select_boundary("end"))
        root.bind("<KeyPress-n>", lambda _event: self.navigate(1))
        root.bind("<KeyPress-p>", lambda _event: self.navigate(-1))
        root.bind("<KeyPress-r>", lambda _event: self.start_player())
        root.bind("<KeyPress-d>", lambda _event: self.delete_segment())
        self.show_current()
        root.after(100, self.poll_player)

    def current(self) -> dict[str, Any] | None:
        segments = self.plan["segments"]
        return segments[self.index] if self.index < len(segments) else None

    def show_current(self) -> None:
        segment = self.current()
        if segment is None:
            self.header.config(text="Refinement complete")
            deleted_count = len(self.plan["refinement"].get("deleted_segments", []))
            self.details.config(text=f"All remaining {len(self.plan['segments'])} segment(s) reviewed; {deleted_count} segment(s) deleted.\nRefined plan: {self.output}")
            if self.plan["segments"]:
                self.status.config(text="The refined plan is ready to pass to the render command.")
            else:
                self.status.config(text="No segments remain, so there is nothing to render.")
            self.plan["refinement"]["status"] = "COMPLETE"
            self.save()
            self.stop_player()
            return
        start = float(segment["source_start_seconds"])
        end = float(segment["source_end_seconds"])
        self.header.config(text=f"Segment {self.index + 1} of {len(self.plan['segments'])}  ·  {segment.get('segment_id', '')}  ·  {segment.get('game', 'Unknown')}")
        self.details.config(text=f"Clip: {segment.get('local_content_id', '')}\nVideo: {segment['video_path']}\n"
                                 f"Start: {_format_time(start)} ({start:.3f}s)    End: {_format_time(end)} ({end:.3f}s)    Length: {end-start:.3f}s")
        if self.player_segment != self.index:
            self.start_player()

    def start_player(self) -> None:
        segment = self.current()
        if segment is None:
            return
        if not self.stop_player():
            return
        path = Path(segment["video_path"]).expanduser()
        self.position = float(segment["source_start_seconds"])
        if not path.is_file():
            self.status.config(text=f"Source video is missing: {path}")
            return
        try:
            start = float(segment["source_start_seconds"])
            end = float(segment["source_end_seconds"])
            self.session_number += 1
            self.player_session = self.temp_path / f"player-{self.session_number:05d}"
            self.player_session.mkdir()
            self.position_path = self.player_session / "position.txt"
            self.action_path = self.player_session / "actions.txt"
            self.loop_path = self.player_session / "loop.txt"
            self.script_path = self.player_session / "refine.lua"
            self.action_path.touch()
            _write_player_script(self.script_path, self.position_path, self.action_path, self.loop_path)
            _write_loop_bounds(self.loop_path, start, end)
            self.action_offset = 0
            self.process = subprocess.Popen([self.mpv, "--no-config", "--force-window=yes", "--autofit=30%x30%", "--keep-open=yes",
                f"--start={start:.9f}", f"--ab-loop-a={start:.9f}", f"--ab-loop-b={end:.9f}",
                "--ab-loop-count=inf", f"--script={self.script_path}", "--", str(path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.player_segment = self.index
            self.status.config(text="mpv opened at the segment start and will loop between its current start and end. Seek freely through the source clip.")
        except OSError as exc:
            self.status.config(text=f"Could not open mpv: {exc}")
            self.process = None
            self._clear_player_session()

    def _clear_player_session(self) -> None:
        if self.player_session is not None:
            shutil.rmtree(self.player_session, ignore_errors=True)
        self.player_session = None
        self.position_path = None
        self.action_path = None
        self.loop_path = None
        self.script_path = None

    def stop_player(self) -> bool:
        process = self.process
        self.player_segment = None
        if process is None:
            self._clear_player_session()
            return True
        if process.poll() is not None:
            self.process = None
            self._clear_player_session()
            return True
        try:
            process.terminate()
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                self.status.config(text="The previous mpv process did not close. The clip was not reopened; try closing mpv manually, then press R again.")
                return False
        except OSError as exc:
            if process.poll() is None:
                self.status.config(text=f"Could not close the previous mpv window: {exc}")
                return False
        self.process = None
        self._clear_player_session()
        return True

    def select_boundary(self, boundary: str) -> None:
        self.active_boundary = boundary
        self.status.config(text=f"Selected {boundary} boundary. Left/Right adjusts by {self.step * 1000:.0f} ms; Shift uses {self.large_step:g} s.")

    def set_boundary(self, boundary: str, position: float | None = None) -> None:
        segment = self.current()
        if segment is None:
            return
        self.poll_position()
        position = self.position if position is None else position
        duration = float(segment["source_duration_seconds"])
        if position < 0 or position > duration:
            self.status.config(text="Position is outside the source clip; boundary not changed.")
            return
        start = position if boundary == "start" else float(segment["source_start_seconds"])
        end = position if boundary == "end" else float(segment["source_end_seconds"])
        if end <= start:
            self.status.config(text="Start must remain before end; boundary not changed.")
            return
        segment["source_start_seconds"] = round(start, 9)
        segment["source_end_seconds"] = round(end, 9)
        segment["duration_seconds"] = round(end - start, 9)
        segment["refinement_status"] = "EDITED"
        self.active_boundary = boundary
        self.save()
        if self.loop_path is not None:
            try:
                _write_loop_bounds(self.loop_path, start, end)
            except OSError as exc:
                self.status.config(text=f"Boundary saved, but could not update mpv's loop: {exc}")
                return
        self.show_current()
        self.status.config(text=f"Saved {boundary} at {_format_time(position)}.")

    def nudge(self, boundary: str, direction: int, amount: float) -> None:
        segment = self.current()
        if segment is None:
            return
        current = float(segment[f"source_{boundary}_seconds"])
        self.set_boundary(boundary, current + direction * amount)

    def navigate(self, direction: int) -> None:
        next_index = max(0, min(len(self.plan["segments"]), self.index + direction))
        if next_index == self.index:
            return
        self.index = next_index
        self.save()
        self.show_current()

    def delete_segment(self) -> None:
        segment = self.current()
        if segment is None or messagebox is None:
            return
        segment_id = str(segment.get("segment_id", f"segment-{self.index + 1}"))
        if not messagebox.askyesno("Delete segment", f"Remove {segment_id} from the refined plan?", parent=self.root):
            return
        if not self.stop_player():
            return
        from datetime import datetime, timezone
        removed = copy.deepcopy(segment)
        removed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.plan["refinement"].setdefault("deleted_segments", []).append(
            {"deleted_at_utc": removed_at, "segment": removed})
        del self.plan["segments"][self.index]
        summary = self.plan.setdefault("summary", {})
        summary["segments"] = len(self.plan["segments"])
        summary["selected_bookmark_rows"] = sum(len(item.get("bookmarks", [])) for item in self.plan["segments"])
        summary["merged_or_selected_clip_count"] = len({item.get("local_content_id", "") for item in self.plan["segments"]})
        summary["deleted_segments"] = len(self.plan["refinement"]["deleted_segments"])
        self.save()
        self.show_current()

    def save(self) -> None:
        from datetime import datetime, timezone
        self.plan["refinement"]["last_saved_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.plan["refinement"]["current_segment_index"] = self.index
        _atomic_write(self.output, self.plan)

    def poll_position(self) -> None:
        if self.position_path is None:
            return
        try:
            value = finite_number(self.position_path.read_text(encoding="ascii").strip())
            if value is not None:
                self.position = value
        except OSError:
            pass

    def poll_player(self) -> None:
        self.poll_position()
        segment = self.current()
        if segment:
            self.position_label.config(text=f"Player: {_format_time(self.position)} ({self.position:.3f}s)    Active boundary: {self.active_boundary}")
        try:
            if self.action_path is None:
                actions = []
            else:
                with self.action_path.open("r", encoding="ascii") as stream:
                    stream.seek(self.action_offset)
                    actions = stream.readlines()
                    self.action_offset = stream.tell()
        except OSError:
            actions = []
        for line in actions:
            try:
                fields = line.strip().split(",")
                action = fields[0]
                value = float(fields[1])
            except (ValueError, TypeError):
                continue
            if action in {"set_start", "set_end"}:
                self.set_boundary(action[4:], value)
            elif action in {"nudge_start", "nudge_end"}:
                segment = self.current()
                if segment is not None:
                    self.nudge(action[6:], 1 if value >= 0 else -1, abs(value))
            elif action in {"select_start", "select_end"}:
                self.select_boundary(action[7:])
            elif action == "navigate_next":
                self.navigate(1)
            elif action == "navigate_previous":
                self.navigate(-1)
        if self.process and self.process.poll() is not None and self.current() is not None:
            self.status.config(text="mpv is closed. Choose Reopen current clip or press R in this window.")
        self.root.after(100, self.poll_player)

    def close(self) -> None:
        if not self.stop_player():
            return
        self.temporary.cleanup()
        self.root.destroy()


def run_refinement(plan_path: Path, output_path: Path, mpv_bin: str = "mpv",
                   ffprobe_bin: str = "ffprobe", step: float = 0.1,
                   large_step: float = 1.0) -> tuple[int, Path]:
    if not math.isfinite(step) or not math.isfinite(large_step) or step <= 0 or large_step <= 0:
        raise RefinementError("nudge steps must be finite numbers greater than zero")
    plan_path = plan_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    plan = _load_or_create(plan_path, output_path)
    if not plan["segments"] and not plan.get("refinement", {}).get("deleted_segments"):
        raise RefinementError("plan contains no segments to refine")
    if tk is None:
        raise RefinementError("this Python installation does not include Tkinter, which refinement needs")
    mpv = resolve_mpv_executable(mpv_bin)
    ffprobe = resolve_executable(ffprobe_bin)
    checked: set[str] = set()
    for index, segment in enumerate(plan["segments"], start=1):
        key = str(Path(segment["video_path"]).expanduser().resolve()).casefold()
        if key in checked:
            continue
        checked.add(key)
        try:
            duration, has_video, _has_audio = probe_media(ffprobe, Path(segment["video_path"]).expanduser())
        except RenderError as exc:
            raise RefinementError(f"cannot preview segment {segment.get('segment_id', index)}: {exc}") from exc
        if not has_video:
            raise RefinementError(f"source for segment {segment.get('segment_id', index)} has no readable video stream")
        if float(segment["source_end_seconds"]) > duration:
            raise RefinementError(f"segment {segment.get('segment_id', index)} ends after the measured video duration ({duration:.3f}s)")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise RefinementError(f"could not start the refinement window: {exc}") from exc
    window = RefinementWindow(root, plan, output_path, mpv, step, large_step)
    root.mainloop()
    return len(plan["segments"]), output_path
