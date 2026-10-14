# Medal Highlight Reel

Plan and render short highlight reels from compatible `clips.csv` and `bookmarks.csv` reports produced by Medal Bookmark Auditor. The planner uses each bookmark's recovered position, selects a bounded time window around it, checks the referenced video file, and leaves an audit trail for anything it skips. FFmpeg renders normalized MP4 segments and joins them into one video.

This project does not read or modify Medal's database. It consumes the two CSV exports, so keep the original reports and source clips available while planning and rendering.

## Requirements

- Python 3.11 or later
- FFmpeg and ffprobe installed and available on `PATH` for rendering
- mpv and Python's Tkinter module for manual bookmark review and segment refinement
- The `clips.csv` and `bookmarks.csv` from a compatible `medal-audit extract` run

## Install

From the project folder, install the command locally:

```powershell
py -m pip install .
```

For development, use `py -m pip install -e .`. Confirm the command is available with `medal-highlights --help`.

## First make a plan

```powershell
medal-highlights plan `
  --clips .\reports\clips.csv `
  --bookmarks .\reports\bookmarks.csv `
  --config .\config\windows.example.json `
  --game Valorant `
  --bookmark-name Kill `
  --output-dir .\highlight-plan
```

This writes three review files:

- `plan.json` contains the renderable segments and their source positions.
- `segments.csv` is a spreadsheet-friendly view of the planned segments.
- `rejected.csv` lists bookmark rows not used, with a reason for each.

Use `--preview` to calculate the plan and print counts without creating report files. Use `--before-seconds 8 --after-seconds 6` to override the default window for this run. Repeat `--game` or `--bookmark-name` to select more than one exact name; matching ignores letter case.

## Manually mark missing bookmark times

If the audit report has a bookmark with missing timing data but the source clip is playable, open the manual review queue:

```powershell
medal-highlights review `
  --clips .\reports\clips.csv `
  --bookmarks .\reports\bookmarks.csv `
  --output .\reports\manual-timestamps.csv
```

The review window opens each eligible clip in mpv. Pause or seek to the event, then press **M** in mpv or click **Mark current time** in the review window. Marks save immediately; **Next**, **Previous**, and **Skip** let you move through the queue. Run the command again later to continue any unmarked rows. Only bookmarks marked `MISSING_TIMING_DATA` are queued. Missing/ambiguous clip records, absent files, invalid durations, and files without a readable video stream are listed in `manual-timestamps_review_skipped.csv`.

Use the saved correction file when creating the plan:

```powershell
medal-highlights plan `
  --clips .\reports\clips.csv `
  --bookmarks .\reports\bookmarks.csv `
  --manual-timestamps .\reports\manual-timestamps.csv `
  --config .\config\windows.example.json `
  --output-dir .\highlight-plan
```

Manual corrections are stored separately and labeled `MANUALLY_MARKED` in the plan. Each correction is tied to its bookmark row and a SHA-256 fingerprint of the exact bookmarks CSV. If that report changes, the project refuses to apply old marks to potentially different rows; review the updated report again.

## Refine segment boundaries

If a planned cut needs adjustment, refine a separate copy of `plan.json` before rendering:

```powershell
medal-highlights refine `
  --plan .\highlight-plan\plan.json `
  --output .\highlight-plan\refined-plan.json
```

The refinement window opens each source video at that segment's proposed start and loops playback between its proposed start and end. You can seek anywhere in the full clip in mpv to inspect surrounding context. The loop bounds update as you refine them. The default fine adjustment is 100 ms; the larger keyboard nudge is 1 second. In mpv, **A** sets the start to the current playback time, **B** sets the end, **S/E** selects which boundary to nudge, **Ctrl+Left/Right** nudges that boundary by 100 ms, and **Ctrl+Shift+Left/Right** nudges it by 1 second. The control window has a **Reopen current clip** button (or press **R** while that window is focused) if mpv is accidentally closed. You can also focus the control window and use its other buttons or keyboard shortcuts. Start and end must stay within the source duration and start must remain before end.

The refined JSON contains the complete plan and remains compatible with the renderer. It is written separately from the generated plan and atomically saved after each adjustment, so rerunning the same command with the same source and output resumes the saved work. If the source plan changes, choose a new output path rather than applying the old refinements to a different plan. To change the nudge sizes, use `--step-seconds 0.1 --large-step-seconds 1`.

## Render

Review `segments.csv`, then render the plan:

```powershell
medal-highlights render `
  --plan .\highlight-plan\refined-plan.json `
  --output .\highlight-reel.mp4
```

You can pass the original `plan.json` to `render` if you do not want to refine it.

Rendering checks the source files again and uses ffprobe to confirm each has a readable video stream and duration. Sources that disappeared or cannot be decoded are skipped and recorded in `highlight-reel_skipped.csv`. If none can be rendered, the command stops and points to that report. Existing outputs are protected; pass `--overwrite` to replace them.

The renderer encodes each selected portion to a common H.264/AAC format before joining. This makes clips with different dimensions, frame rates, or audio layouts compatible, at the cost of re-encoding. Defaults are 1920x1080 at 30 fps; use `--width`, `--height`, and `--fps` to change them. Width and height must be even for the selected pixel format.

## Window configuration

`config/windows.example.json` shows the format:

```json
{
  "default": { "before_seconds": 10, "after_seconds": 5 },
  "bookmarks": {
    "Kill": { "before_seconds": 6, "after_seconds": 4 },
    "ACE": { "before_seconds": 15, "after_seconds": 12 }
  }
}
```

The times are seconds surrounding the recovered bookmark position. A bookmark-specific setting overrides the corresponding default. Values must be finite and nonnegative; zero is allowed. The planner clamps the beginning and end of each window to the clip duration. When windows in the same source clip overlap or touch, they are combined into one continuous segment and retain all their bookmark details.

## Selection and edge cases

- Only `IN_RANGE` bookmark positions are included by default. `OUT_OF_RANGE` positions are kept out unless `--include-out-of-range` is set; included windows are still clamped to the clip's actual bounds.
- Missing timing data, invalid positions or durations, empty paths, and video paths that do not point to files are skipped and explained in `rejected.csv`.
- Bookmarks without a matching clip record are rejected. If duplicate clip IDs make a match ambiguous, those clips are excluded and reported.
- The source file is checked once during planning and again during rendering. ffprobe then checks the actual media streams and duration; the render window is clamped again to that measured duration.
- The planner never treats a missing or invalid value as zero timing. This keeps uncertain data distinguishable from a real bookmark at the start of a clip.
- A bookmark-name or game filter is exact, ignoring letter case. Filtered-out bookmark rows are listed as `bookmark_name_filter` or `game_filter` in the rejection report, making the selection auditable.

## Output safety

The tool does not delete source clips or change the input CSV files. Rendered intermediate files are temporary and removed after completion. Do not publish the CSVs or plan if they contain local file paths or other personal data you do not want to share.

## License

MIT. See [LICENSE](LICENSE).
