# Archive Scout

Archive Scout is a resumable macOS desktop application for researching public Wayback Machine captures. Add one or more domains or paths, choose traditional CDX query options, download archived text pages, scan them for custom keywords, and export plain-text reports.

The included preset searches `ogrishforum.com` and `forum.ogrish.com` for September 11-related terms, but every target and keyword is editable.

## Download for Intel Mac

[Download the latest Intel Mac DMG](https://github.com/DearStrike4940/archive-scout/releases/latest/download/ArchiveScout-macOS-Intel.dmg)

Targeted platform: Intel Macs running macOS Monterey 12 or newer. Test each release on a real Monterey Mac; PyInstaller recommends building on the oldest macOS version you intend to support for the strongest backward compatibility.

The default community build is ad-hoc signed rather than Apple-notarized. On first launch, Control-click Archive Scout in Applications, choose Open, then confirm Open. See [Mac installation](docs/MAC_INSTALLATION.md).

## What it does

- Provides a native-looking Tkinter interface bundled as a normal `.app`.
- Accepts multiple domains and paths, one per line.
- Supports exact CDX start and end dates.
- Supports repeated `filter` values.
- Supports `collapse=urlkey` and `collapse=digest`.
- Supports `matchType` values: exact, prefix, host, and domain.
- Accepts additional validated CDX parameters as `key=value` lines.
- Keeps the earliest capture found for each original URL.
- Resumes CDX pagination and downloads after interruption.
- Downloads several text captures concurrently.
- Scans URLs, titles, visible text, HTML source, and extracted links.
- Supports literal phrases and `re:` regular expressions.
- Writes ranked results and supporting lists as UTF-8 `.txt` files.
- Uses SQLite WAL mode for fast, durable local state.
- Requires no packages after the app is installed.

## Quick start

1. Download the DMG from the link above.
2. Open it and drag Archive Scout into Applications.
3. Open Archive Scout.
4. Choose a preset or enter your own sites and paths.
5. Enter keywords, one per line.
6. Review the CDX Options tab.
7. Choose an output folder and click Start.
8. Open the generated `reports` folder when the run finishes.

Example targets:

```text
example.com/*
forum.example.com/*
example.com/archive/*
forum.example.com/showthread.php?*
```

Example keywords:

```text
World Trade Center
September 11
jumper footage
re:\bWTC\b
```

## CDX options

Archive Scout exposes the most useful CDX controls directly:

- Start and end date: `YYYY`, `YYYYMM`, `YYYYMMDD`, or `YYYYMMDDhhmmss`.
- Filters: one value per line, such as `statuscode:200` or `mimetype:text/html`.
- Collapse: URL-key and digest collapse can be enabled independently.
- `matchType`: automatic, exact, prefix, host, or domain.
- Page size: controls the CDX pagination limit.
- Additional parameters: one decoded `key=value` pair per line.

The app shows a complete request preview before you run it. Critical parameters such as `url`, `from`, `to`, `output`, `fl`, pagination keys, and `limit` are controlled by the app so the response remains resumable and parseable.

See [CDX options](docs/CDX_OPTIONS.md) for examples and cautions.

## Output

Each project folder contains:

```text
archive_scout.sqlite3
project.json
captures/
reports/
  matches_ranked.txt
  matched_urls.txt
  wayback_urls.txt
  interesting_links.txt
  keyword_counts.txt
  all_indexed_urls.txt
  errors.txt
  summary.txt
```

## Performance

Archive Scout is designed for older Intel Macs as well as newer systems.

- CDX queries are split into bounded yearly windows.
- Resume keys are saved after every CDX page.
- Downloads use a bounded worker pool instead of creating unlimited requests.
- A shared rate limiter reduces HTTP 429 errors.
- Text detection avoids downloading known binary objects.
- Pages are scanned immediately after download.
- The fast scope option downloads only URLs that already contain a keyword.
- Query signatures prevent results from different CDX parameter sets from mixing.

For a 2015 Intel Mac, begin with four download workers. Six is normally reasonable on a stable connection. Raising the worker count does not make the Wayback Machine itself respond faster and can increase rate limiting.

## Create your own GitHub repository

Follow [GitHub repository setup](docs/GITHUB_SETUP.md). It covers:

- creating the repository,
- replacing the username placeholder,
- pushing the included files,
- enabling the release workflow,
- building the Intel `.app` and DMG on GitHub,
- and publishing the permanent download link.

## Run from source

Python 3.11 or newer is recommended.

```bash
git clone https://github.com/DearStrike4940/archive-scout.git
cd archive-scout
python3 run_app.py
```

No runtime packages are required when running from source. PyInstaller is needed only to build the downloadable Mac application.

Run the tests:

```bash
python3 -m unittest discover -s tests -v
```

Build on an Intel Mac:

```bash
python3 -m pip install -r requirements-build.txt
bash scripts/build_macos.sh
```

## Responsible use

Archive Scout is intended for lawful research and preservation of publicly archived material. Respect the Internet Archive's availability, rate limits, terms, copyright restrictions, privacy interests, and the sensitivity of the material you research. Do not use the software to overwhelm archive services or republish restricted content.

Archive Scout is an independent project and is not affiliated with the Internet Archive.

## License

MIT. See [LICENSE](LICENSE).
