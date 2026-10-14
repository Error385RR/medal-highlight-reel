"""Validate source media and render normalized segments with FFmpeg."""
from __future__ import annotations

import csv
import json
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


class RenderError(RuntimeError):
    pass


def resolve_executable(value: str) -> str:
    resolved = shutil.which(value)
    if resolved:
        return resolved
    if Path(value).is_file():
        return str(Path(value).resolve())
    raise RenderError(f"could not find executable {value!r}; install FFmpeg and ensure it is on PATH")


def resolve_mpv_executable(value: str) -> str:
    resolved = Path(resolve_executable(value))
    if os.name == "nt" and resolved.suffix.casefold() in {".com", ""}:
        gui_executable = resolved.with_suffix(".exe")
        if gui_executable.is_file():
            return str(gui_executable.resolve())
    return str(resolved)


def probe_media(ffprobe: str, path: Path) -> tuple[float, bool, bool]:
    try:
        result = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                                 "-show_entries", "stream=codec_type", "-of", "json", str(path)],
                                capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        duration = float(data.get("format", {}).get("duration", "nan"))
        stream_types = {s.get("codec_type") for s in data.get("streams", [])}
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("media duration is missing or invalid")
        return duration, "video" in stream_types, "audio" in stream_types
    except FileNotFoundError as exc:
        raise RenderError(f"could not start ffprobe: {exc}") from exc
    except OSError as exc:
        raise RenderError(f"could not run ffprobe: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip().splitlines()
        raise RenderError(detail[-1] if detail else "ffprobe could not read the media") from exc
    except (ValueError, json.JSONDecodeError, TypeError) as exc:
        raise RenderError(f"could not interpret ffprobe output for {path}: {exc}") from exc


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _write_skips(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=["segment_id", "local_content_id", "video_path", "reason"])
        writer.writeheader()
        writer.writerows(rows)


def render_plan(plan_path: Path, output_path: Path, ffmpeg_bin: str = "ffmpeg", ffprobe_bin: str = "ffprobe",
                overwrite: bool = False, width: int = 1920, height: int = 1080, fps: int = 30) -> tuple[int, list[dict[str, str]], Path]:
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RenderError(f"cannot read plan {plan_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RenderError(f"invalid JSON plan {plan_path}: {exc}") from exc
    segments = plan.get("segments") if isinstance(plan, dict) else None
    if not isinstance(segments, list) or not segments:
        raise RenderError("plan has no segments to render")
    if width <= 0 or height <= 0 or fps <= 0:
        raise RenderError("output width, height, and frame rate must be positive integers")
    if width % 2 or height % 2:
        raise RenderError("output width and height must be even numbers for yuv420p video")
    if any(not isinstance(segment, dict) for segment in segments):
        raise RenderError("plan contains an invalid segment entry")
    output_path = output_path.expanduser().resolve()
    if output_path.suffix.casefold() != ".mp4":
        raise RenderError("output filename must end in .mp4")
    skipped_path = output_path.with_name(output_path.stem + "_skipped.csv")
    if not overwrite:
        existing = [p for p in (output_path, skipped_path) if p.exists()]
        if existing:
            raise RenderError("output already exists: " + ", ".join(map(str, existing)) + " (use --overwrite)")
    ffmpeg = resolve_executable(ffmpeg_bin)
    ffprobe = resolve_executable(ffprobe_bin)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    source_paths = {str(Path(str(s.get("video_path", ""))).expanduser().resolve()).casefold() for s in segments}
    if str(output_path).casefold() in source_paths:
        raise RenderError("output path matches a source video; choose a different output filename")

    skipped: list[dict[str, str]] = []
    rendered = 0
    probe_cache: dict[str, tuple[float, bool, bool] | str] = {}
    with tempfile.TemporaryDirectory(prefix="medal-highlight-reel-", dir=output_path.parent) as temp_name:
        temp_dir = Path(temp_name)
        intermediate_names: list[str] = []
        for index, segment in enumerate(segments, start=1):
            segment_id = str(segment.get("segment_id", f"seg-{index:05d}"))
            source_text = str(segment.get("video_path", "")).strip()
            clip_id = str(segment.get("local_content_id", ""))
            def skip(reason: str) -> None:
                skipped.append({"segment_id": segment_id, "local_content_id": clip_id,
                                "video_path": source_text, "reason": reason})
            if not source_text:
                skip("missing_video_path"); continue
            try:
                source = Path(source_text).expanduser()
                if not source.is_file():
                    skip("video_file_missing_at_render_time"); continue
                source_key = str(source.resolve()).casefold()
            except (OSError, ValueError) as exc:
                skip(f"video_path_unreadable: {exc}"); continue
            cached = probe_cache.get(source_key)
            if cached is None:
                try:
                    cached = probe_media(ffprobe, source)
                except RenderError as exc:
                    cached = str(exc)
                probe_cache[source_key] = cached
            if isinstance(cached, str):
                skip(f"media_probe_failed: {cached}"); continue
            media_duration, has_video, has_audio = cached
            if not has_video:
                skip("source_has_no_video_stream"); continue
            start = _number(segment.get("source_start_seconds"))
            end = _number(segment.get("source_end_seconds"))
            if start is None or end is None or start < 0 or end <= start:
                skip("invalid_segment_bounds"); continue
            start = min(start, media_duration)
            end = min(end, media_duration)
            duration = end - start
            if duration <= 0:
                skip("segment_outside_actual_video_duration"); continue
            target = temp_dir / f"segment_{index:06d}.mp4"
            video_filter = (f"setpts=PTS-STARTPTS,scale={width}:{height}:force_original_aspect_ratio=decrease,"
                            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={fps},setsar=1,format=yuv420p")
            audio_input: list[str] = []
            audio_map = "0:a:0"
            if not has_audio:
                audio_input = ["-f", "lavfi", "-t", f"{duration:.6f}", "-i",
                               "anullsrc=channel_layout=stereo:sample_rate=48000"]
                audio_map = "1:a:0"
            audio_filter = f"aresample=48000,apad,atrim=duration={duration:.6f},asetpts=PTS-STARTPTS"
            command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.6f}",
                       "-i", str(source), *audio_input, "-t", f"{duration:.6f}", "-map", "0:v:0", "-map", audio_map,
                       "-vf", video_filter, "-af", audio_filter, "-c:v", "libx264", "-preset", "medium",
                       "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                       "-ac", "2", "-movflags", "+faststart", str(target)]
            try:
                subprocess.run(command, capture_output=True, text=True, check=True)
                if not target.is_file() or target.stat().st_size == 0:
                    skip("ffmpeg_created_no_segment_output"); continue
                intermediate_names.append(target.name)
                rendered += 1
            except FileNotFoundError as exc:
                raise RenderError(f"could not start ffmpeg: {exc}") from exc
            except OSError as exc:
                skip(f"ffmpeg could not read or write this segment: {exc}")
            except subprocess.CalledProcessError as exc:
                details = (exc.stderr or "").strip().splitlines()
                skip("ffmpeg_failed: " + (details[-1] if details else "unknown FFmpeg error"))

        if not intermediate_names:
            _write_skips(skipped_path, skipped)
            raise RenderError(f"no segments could be rendered; see {skipped_path}")
        manifest = temp_dir / "segments.ffconcat"
        manifest.write_text("ffconcat version 1.0\n" + "".join(f"file '{name}'\n" for name in intermediate_names), encoding="utf-8")
        temporary_output = temp_dir / "final.mp4"
        concat_command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "1",
                          "-i", str(manifest), "-c", "copy", "-movflags", "+faststart", str(temporary_output)]
        try:
            subprocess.run(concat_command, capture_output=True, text=True, check=True)
            if not temporary_output.is_file() or temporary_output.stat().st_size == 0:
                raise RenderError("FFmpeg produced an empty final video")
        except FileNotFoundError as exc:
            raise RenderError(f"could not start ffmpeg: {exc}") from exc
        except OSError as exc:
            raise RenderError(f"could not run FFmpeg to join the segments: {exc}") from exc
        except subprocess.CalledProcessError as exc:
            details = (exc.stderr or "").strip().splitlines()
            raise RenderError("FFmpeg could not join the rendered segments: " + (details[-1] if details else "unknown error")) from exc
        _write_skips(skipped_path, skipped)
        try:
            os.replace(temporary_output, output_path)
        except OSError as exc:
            raise RenderError(f"could not move finished video to {output_path}: {exc}") from exc
    return rendered, skipped, skipped_path
