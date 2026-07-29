# Architecture

## Components

- `archive_scout/app.py`: Tkinter desktop interface, project editing, progress display, and saved UI state.
- `archive_scout/engine.py`: CDX queries, validation, SQLite state, downloads, scanning, scoring, and reports.
- `archive_scout/defaults.py`: editable presets.
- `archive_scout/cli.py`: optional command-line runner for saved `project.json` files.
- `run_app.py`: PyInstaller entry point.
- `scripts/build_macos.sh`: Intel `.app` and DMG build.

## Data flow

1. Normalize targets, dates, CDX parameters, and keywords.
2. Compute a query signature.
3. Query CDX in yearly windows using resume keys.
4. Upsert the earliest capture for each original URL.
5. Select text-like captures for download.
6. Fetch raw replay content with bounded concurrency and rate limiting.
7. Extract title, visible text, and links.
8. Scan URL, title, text, source, and links.
9. Save capture text and analysis state.
10. Generate plain-text reports.

## Reliability

- SQLite uses WAL mode and normal synchronous durability.
- CDX progress is committed after each response page.
- Interrupted `downloading` rows return to `pending` on restart.
- Retryable HTTP responses use exponential backoff and `Retry-After` when available.
- A shared limiter spaces requests across worker threads.
- Response and local file sizes are bounded.
- Temporary writes are atomically replaced.
- Query signatures isolate changed CDX parameter sets.

## Packaging

PyInstaller builds a windowed `--onedir` application bundle. On macOS, `--windowed` produces an `.app`; `--onedir` avoids extracting the entire runtime into a temporary directory at every launch.

The GitHub workflow builds on an Intel macOS runner, sets a Monterey deployment target, ad-hoc signs the bundle, and creates a compressed DMG with an Applications shortcut.
