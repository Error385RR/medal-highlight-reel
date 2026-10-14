# Medal Highlight Reel

Plan and render short highlight reels from compatible `clips.csv` and `bookmarks.csv` reports produced by Medal Bookmark Auditor. The planner uses each bookmark's recovered position, selects a bounded time window around it, checks the referenced video file, and leaves an audit trail for anything it skips. FFmpeg renders normalized MP4 segments and joins them into one video.

This project does not read or modify Medal's database. It consumes the two CSV exports, so keep the original reports and source clips available while planning and rendering.

## Requirements

- Python 3.11 or later
- FFmpeg and ffprobe installed and available on `PATH` for rendering
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

## Render

Review `segments.csv`, then render the plan:

```powershell
medal-highlights render `
  --plan .\highlight-plan\plan.json `
  --output .\highlight-reel.mp4
```

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
