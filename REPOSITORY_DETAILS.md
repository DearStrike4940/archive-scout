# Repository details

Product: Archive Scout
Version: 1.0.7.1
License: MIT
Python: 3.11+
Desktop UI: Tkinter/ttk
Primary project store: SQLite
Primary archive source: Internet Archive Wayback Machine
Optional AI relevance provider: OpenAI API

## Main packages

- `archive_scout/cdx`: Wayback indexing, pagination, transport recovery, request construction.
- `archive_scout/downloads`: textual replay downloading, validation, retry policy, rate limiting.
- `archive_scout/scanning`: keyword compilation, automaton prefiltering, scoring, rescanning, FTS helpers.
- `archive_scout/media`: media indexing, external embedded-media discovery, media downloading, reports.
- `archive_scout/ai`: optional report candidate selection, OpenAI relevance ranking, AI report generation.
- `archive_scout/database`: schema, migrations, repository operations.
- `archive_scout/parsing`: HTML, forum, embed, and legacy-player extraction.
- `archive_scout/analysis`: duplicate, timeline, provenance, comparison, and analysis workflows.
- `archive_scout/projects`: backup, repair, migration, merge, integrity, diagnostics.
- `archive_scout/ui`: desktop application and bounded event queue.

## Release artifacts

- `ArchiveScout-Windows-x64.zip`
- `ArchiveScout-Linux-x64.tar.gz`
- `ArchiveScout-macOS-Universal.zip`

Each platform package is accompanied by a SHA-256 checksum in the release workflow.
