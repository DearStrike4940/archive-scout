# Archive Scout 1.0.7.1

Reports now appears between Media and Archive analysis in both workspace modes, with complete file/field controls, scrolling, presets, sorting and output limits. Report visibility no longer erases historical scan evidence. New text captures retain their raw source bytes under `.txt` names; binary signatures and media HTML validation strengthen text/media separation.

This patch also fixes CDX input/request encoding, unbounded completed-page accumulation, long repeated page-count timeouts, inconsistent proxy fallback, invalid Range resumes, Hitlist pause checkpoints, an undefined rate-limit cleanup call and repeated local scan failures. The 10-worker / 0.125-second replay profile remains; local scans follow acquisition.

See [the complete v1.0.7.1 notes](docs/V1.0.7.1.md) for behavior, upgrade instructions and validation limits. Schema remains 8. The notes below describe the older 1.0.7 release; its destructive report-storage policy is superseded by 1.0.7.1.

# Archive Scout 1.0.7

Archive Scout 1.0.7 focuses on the post-1.0.6 performance/reliability issues without replacing the proven acquisition architecture. The v1.0.5-style Timemap and replay pacing remains the baseline, while startup no longer performs potentially heavy project preparation on Tk's Start path. The release also makes follow-up media available to download-only projects and turns report generation into a complete per-file/per-field configuration rather than a fixed export.

## Startup and throughput

- Start immediately creates a worker and displays indeterminate progress before bundle validation, database opening/migration, backups, or engine setup can do slow work. Errors from those steps return through the normal worker event channel instead of leaving the UI indefinitely at `Starting…`.
- The established fast profile remains unchanged: ten CDX workers, native Timemap `pageSize=9` automatic paging, 0.75-second CDX request spacing, ten replay workers, and 0.125-second replay request-start spacing.
- Existing page checkpoints, download staging, bounded scanner backlogs, content sniffing, and pause/resume behavior remain in place.

## Download-only media

- **Also download media during text and download-only runs** now applies to the acquisition-only workflow. Images/videos, embedded-media discovery, external-host permission, extension filters, size limit, and snapshot strategy use the same Media-page settings.
- Embedded discovery can inspect `downloaded_unscanned` capture files directly and queues media without inventing scan/document rows.
- Follow-up media is a separate CDX pipeline. The default `earliest` strategy uses `collapse=urlkey` so one archived timestamp is selected per media URL; the primary text query retains exactly the user's own collapse settings.
- Media discovery/download state remains in the existing durable schema-8 queues for resume.

## Fully customizable reports and leaner SQLite

- The new **Reports** page exposes every standard text/index/error/media/archive-analysis output and every field/column inside those outputs. A file can be disabled entirely, or kept with only selected fields.
- Disabled files are removed when their report family is regenerated, preventing stale outputs from being mistaken for current results.
- Report-only derived data is demand-driven. Snippets and Interesting Links are not even computed when no enabled output needs them. Keyword-hit JSON/field detail and untouched default `unreviewed` rows are not stored when their enabled report fields do not require them.
- URL/timestamp/path/queue/retry/error records remain core project state and are never removed merely because a report field is hidden, preserving Resume, Retry, Results, and Search with Hitlist correctness.
- Database schema remains version 8; existing projects open without migration. Older project files without report settings retain the historical all-reports/all-fields behavior.

## Automation compatibility

The GUI and `ArchiveScoutCLI` continue to use the same operations engine. JSON/JSONL automation output remains isolated from diagnostics, and v1.0.7 adds no Discord-specific dependency or hot-path activity work that could reduce indexing or download throughput.

# Archive Scout 1.0.6.6

Archive Scout 1.0.6.6 replaces the automatic indexing recovery behavior with a simpler native Timemap JSON pipeline based on the attached high-throughput Wayback downloader. The previous engine could keep successful pages but still abandon the entire numbered-page plan after one page failed twice, then rebuild that year as slower resume-key windows. On large targets that could repeat substantial work and make indexing appear nearly unusable.

## Native Timemap indexing

- Automatic and Timemap modes use only `https://web.archive.org/web/timemap/json` for numbered paging. A page is no longer sent through endpoints with different pagination/representation semantics.
- Timemap page counts and numbered pages are native JSON operations. There is no text-format request or text fallback for a numbered page.
- The page-count request receives at least five attempts before Archive Scout concludes that pagination is unavailable and falls back to resumable CDX traversal.
- Page requests keep the established `pageSize=9`, ten-worker, 0.75-second shared request-start spacing, and 1,000-page rolling scheduler.
- Page-count queries omit unused capture fields. Numbered pages request only timestamp, original URL, MIME type, status, digest, and length; the resume-only URL key is not transferred.

## Isolated recovery and visible progress

- Every failed page is tracked independently in the existing durable `retry_pages` and `page_failures` state.
- One bad page receives five attempts without resetting the year, splitting its date range, or repeating completed Timemap pages.
- If a page remains unavailable, Archive Scout pauses with all successful page checkpoints intact and saves only the exact failed page queue for Resume.
- Resume continues those page numbers directly. Resume-key traversal is reserved for genuinely unsupported pagination or a repeatedly unavailable page-count operation.
- The Activity/progress stream now updates while pages inside a 1,000-page block complete, including completed-page and capture counts.
- Direct media indexing uses the same behavior.

## Compatibility

- Database schema remains version 8; no project migration is required.
- Existing v1.0.6.x page checkpoints, captures, unfinished numbered queues, downloads, and media remain compatible.
- Replay downloading and scanning behavior are unchanged.
- Release/package/workflow/Windows executable metadata reports 1.0.6.6.

# Archive Scout 1.0.6.5

Archive Scout 1.0.6.5 fixes the repeated `CDX text response was incomplete; retrying as JSON` slowdown during automatic indexing. The message was usually not evidence that Wayback had truncated the response. Archive Scout was asking the path-specific Timemap JSON service for line-oriented text first; that service could return valid JSON anyway, which the text parser rejected before downloading the same numbered page again.

## Indexing performance fix

- `/web/timemap/json` now uses JSON first and `/web/timemap/cdx` uses line-oriented CDX first. The generic `/cdx/search/cdx` endpoint continues to honor Archive Scout's requested low-memory preference.
- Archive Scout sniffs each successful body before parsing it. Valid JSON returned for a text request, or valid text returned for a JSON request, is parsed immediately without another network request.
- A normal automatic Timemap page therefore requires one successful request instead of a text attempt followed by a duplicate JSON attempt.
- Truly malformed or truncated responses still use the alternate-representation retry, endpoint fallback, bounded page requeue/subdivision, durable per-page checkpoints, and saved resume state.
- The recovery message now says `malformed or truncated` and appears only after the returned body genuinely cannot be parsed in its actual representation.

## Compatibility

- Database schema remains version 8; no project migration is required.
- Existing v1.0.6.x projects, page checkpoints, unfinished queues, downloaded captures, and media remain compatible.
- CDX concurrency, the 0.75-second shared request-start spacing, `pageSize=9`, rolling bounded scheduler, and coordinated rate-limit handling are unchanged.
- Release/package/workflow/Windows executable metadata now reports 1.0.6.5.

# Archive Scout 1.0.6.4

Archive Scout 1.0.6.4 is a cross-platform batching hotfix for the resource-efficiency release. It preserves the v1.0.6.3 CPU, memory, SQLite, and filesystem reductions while making download-only commit behavior deterministic on Windows as well as macOS and Linux. The **Index and download only (no scanning)** operation remains the leanest acquisition path and retains the same Wayback pacing, resume behavior, and Hitlist compatibility.

## Windows batching hotfix

- Download-only now stages a bounded group of durable `local_path` assignments and feeds workers from that local queue instead of committing `state=downloading` for every freed executor slot.
- Completion metadata is coalesced for up to one second or a bounded result count. Final files are already atomically complete and their paths are durable, so a crash in that interval is recovered by adopting the existing file rather than redownloading it.
- `download_attempts` is incremented with the batched completion/error write rather than the submission write.
- The Windows regression test closes SQLite before temporary-directory cleanup, preventing a failed assertion from being obscured by `WinError 32`.

## Resource-efficient acquisition

- Download-only no longer computes SHA-256 while replay bytes are arriving. It streams to disk, retains only the small sniff prefix required for text/replay validation, and records the hash later only if a maintenance operation actually needs it.
- Existing completed captures are sniffed with a small prefix read instead of reading the whole file merely to slice the first 16 KiB.
- Download-only selection streams directly from indexed capture rows in bounded pages instead of inserting every candidate into a project-sized temporary SQLite queue. Known-size captures still receive small-first priority.
- Completed downloads are coalesced across executor wakeups and written in count/time-bounded SQLite transactions; previously FIRST_COMPLETED could turn one finished request into one commit.
- Successful capture-error resolution is performed in one indexed UPDATE per completion batch.
- Compact Project now backfills deferred hashes for acquisition-only captures and includes raw capture files in exact-byte copy-on-write deduplication, preserving storage optimization without putting it in the replay hot path.

## Lower memory and UI/database pressure

- Scan and rescan hashing reuses bytes already loaded for decoding instead of reopening the same file for a second full disk pass. The raw byte buffer is released before DOM/normalization/scoring work begins.
- Compact Project streams legacy document rows instead of fetching every large body row into memory at once.
- While an operation is active, the Dashboard stops issuing repeated exact COUNT queries against large capture/document/match/error tables. Live progress comes from operation counters; exact totals refresh before/after operations and while idle.
- Idle Dashboard recounts are less frequent, reducing page-cache churn on very large projects.

## CI portability

- The download-only path regression now compares resolved paths, fixing the macOS `/private/var/...` versus `/var/...` temporary-directory alias failure seen in the previous Tests workflow.
- Package metadata and workflow checks target 1.0.6.4 across the supported Python/platform matrix.

The established defaults remain unchanged: 10 download workers, 0.125-second replay spacing (up to eight starts/second), 0.75-second CDX spacing, 10 parallel CDX requests, automatic network backend/endpoint/index strategy, and coordinated 429/503 backoff.

# Archive Scout 1.0.6.2

Archive Scout 1.0.6.2 adds a dedicated acquisition-only workflow for very large Wayback projects. **Index and download only (no scanning)** indexes the target and downloads textual captures without creating scan jobs, documents, matches, research indexes, media jobs, or scan reports. SQLite remains only as the durable capture manifest, URL/timestamp mapping, retry state, and resume queue required by crash recovery and Search with Hitlist.

The download-only path has no scanner pool or keyword requirement. Replay files stream directly to disk, SHA-256 is retained from the streaming download path, completion state is written in groups, operation-progress persistence is relaxed to reduce SQLite churn, and saved captures remain `downloaded_unscanned` for later Hitlist search or local rescanning. The normal v1.0.6.1 download+scan pipeline remains available unchanged.

Settings now default to the established fast profile shown in the UI: 10 download workers, automatic scanner workers, 25 MB text-page limit, 0.75-second CDX spacing, 0.125-second replay spacing (up to eight starts/second), automatic network backend/endpoint/index strategy, 10 parallel CDX requests, automatic page-block selection, 30/300-second shared 429/503 pauses, 5/300-second retry backoff, and automatic safety backups disabled by default. The old Ogrish-specific preset has been removed and replaced by general web archive, legacy forum, lost-media, and blank-project presets.

# Archive Scout 1.0.6.1

Archive Scout 1.0.6.1 is a focused throughput hotfix for 1.0.6. It preserves the maximum-recall scanner, durable stage-based resume, Search with Hitlist, URL-derived filenames, storage compaction, auditable skip/error state, and per-page indexing checkpoints while restoring v1.0.5-style replay acquisition speed.

The replay path is again acquisition-first: ten workers and the configured 0.125-second shared request-start spacing run independently from scanner backlog. Downloaded captures are durably marked `downloaded_unscanned`; local scanners can catch up after acquisition instead of throttling Wayback retrieval. SQLite returns to WAL + `synchronous=NORMAL`, collision lookup uses an indexed `local_path`, and physical CoW deduplication is deferred to Compact Project rather than blocking downloads.

# Archive Scout 1.0.6

Archive Scout 1.0.6 is the maximum-recall, durable-resume, and storage-efficiency release. It keeps the fast Timemap acquisition model from 1.0.5 while separating replay downloading from local analysis so network throughput is no longer tied to HTML parsing and scoring.

## Maximum-recall scanning

- The complete raw source is searchable; the previous 500,000-character source cutoff is removed.
- Saved replay payloads are preserved as canonical bytes so future rescans can use improved decoding without another Wayback request.
- UTF-16/UTF-32 BOMs, declared/meta charsets, common legacy fallbacks, HTML entities, URL escapes, JavaScript/JSON hex escapes, raw markup, visible text, compact markup-split text, title, URL, and extracted links are covered.
- MIME type and filename extension are treated as evidence rather than an absolute verdict. Conflicting, extensionless, unknown, and generic octet-stream cases are downloaded/sniffed instead of being silently discarded when ambiguous.
- Ordinary case-insensitive literal rules use the native Aho-Corasick match spans directly; advanced regex/case-sensitive/whole-word rules retain their dedicated paths.

## Durable replay and resume

- Replay downloads and scanning are separate stages. A completed file is persisted and checkpointed as `downloaded_unscanned` before local analysis starts.
- Scanner workers are independent from the ten replay workers.
- Interrupted `downloading` and `scanning` states recover safely on startup, completed files are reused, and valid `.part` files are preserved for retry/resume where supported.
- Numbered Timemap pages have durable completion records so a reboot does not require replaying already committed pages in the current scheduling group.

## Search with Hitlist

A new resumable Search with Hitlist mode accepts one keyword, pasted newline-separated terms, or a `.txt` hitlist. It searches indexed URLs and locally downloaded capture contents with the lightweight literal engine and reports exact coverage rather than implying that undownloaded bodies were searched. It intentionally omits research scoring, proximity, AI, and expensive enrichment.

## Storage efficiency

- Canonical capture files are authoritative; new documents no longer keep a second full visible-body copy in SQLite when the local capture exists.
- FTS5 is maintained as a contentless inverted index rather than another text corpus.
- Exact duplicate payloads can be replaced with safe copy-on-write clones on supported filesystems without hard-linking mutable user files.
- Automatic backups are compressed and bounded by both retention count and storage budget.
- WAL size is bounded/checkpointed, and Compact Project can reclaim redundant legacy body caches and other verified regenerable data without deleting unique capture/media payloads.

## Auditability and filenames

- Intentional non-text and URL-filter skips no longer inflate Open Errors. Skip reasons and classifier revision are persisted and can be reevaluated after configuration/classifier changes.
- The Dashboard separates active errors, recovery/network events, non-text skips, URL-filter skips, other intentional skips, pending downloads, and downloaded-but-unscanned captures.
- New text, image, and video captures use the same recognizable portable filename derived from the full original URL. Query information is preserved; deterministic timestamp/hash suffixes are used only when required for collision/length safety. Existing archives are not destructively renamed.

## Indexing and macOS

- v1.0.5's Timemap-first automatic profile remains: `pageSize=9`, ten bounded page workers, 0.75-second shared CDX spacing, rolling scheduling up to 1,000 pages, with resume-key/subdivision fallbacks.
- The macOS outer bundle remains `Archive Scout.app`, while its inner executable/process identity is `Wayback Machine Downloader` as a best-effort Discord automatic activity-name change. No Rich Presence dependency is added.

## Compatibility

- Public version: 1.0.6.
- Database schema: 8.
- Migration from supported v1.0.x schemas is automatic and non-destructive.
- Existing downloaded files remain usable and are not mass-renamed.

---

# Archive Scout 1.0.5

Archive Scout 1.0.5 focuses on acquisition speed during the indexing phase and simplifies media storage. Automatic CDX indexing now mirrors the reference downloader's proven Timemap architecture while retaining Archive Scout's persistent queues, database safety, recovery circuits, and resume fallback.

## Fast indexing

- Auto indexing is now Timemap-first and numbered-page based.
- The default grouping is `pageSize=9`, with ten concurrent CDX page workers and the existing 0.75-second shared CDX request spacing (about 80 request starts/minute).
- Up to 1,000 numbered pages are queued behind the bounded ten-worker pool so completed workers immediately pick up new pages instead of waiting at a small batch barrier.
- Page bodies are committed to SQLite as soon as each request finishes.
- Resume-key traversal remains available explicitly and is still the automatic fallback when Timemap pagination is unavailable, a page repeatedly times out, or a window must be subdivided.
- Direct-media CDX indexing uses the same Timemap-first rolling page pipeline.

## Media layout and downloading

- `media/` now contains only `images/` and `videos/` for new v1.0.5 output. No host or original-path subdirectories are created.
- Media filenames are taken directly from the URL's final path component. Timestamp/id prefixes are removed and percent-escaped URL spelling is preserved.
- Normal media replay uses Wayback's `if_` modifier; SWF uses `oe_`, matching the reference downloader.
- Media downloads continue to prioritize known-small files, use persistent pooled connections, ten workers, 0.125-second request spacing (eight starts/second), direct-to-disk streaming, and coordinated 429/503 pauses. v1.0.5 guarantees at least five transient attempts for media fetching.
- If the exact flat destination already exists, the file is hashed and reused without another network request, matching the reference downloader's preflight behavior.

## Compatibility

- Database schema remains version 7.
- Existing project files remain compatible. Auto mode always uses the fixed reference profile `pageSize=9`, including saved queues from older projects; a custom page-block value is honored only when `Index strategy` is explicitly set to `paged`.
- The fixed media layout ignores the older `preserve_paths` option while continuing to accept it in existing project JSON.
- Public version: 1.0.5.

---

# Archive Scout 1.0.4

Archive Scout 1.0.4 is the high-throughput replay release. It preserves schema version 7 and the v1.0.3 resume-key CDX architecture while bringing text replay throughput much closer to the fast standalone Wayback downloader profile.

## High-throughput text replay

- New projects default to 10 text replay workers with 0.125-second request-start spacing, allowing up to eight replay starts per second while retaining the process-wide Wayback host gate.
- Untouched v1.0.3 replay defaults (4 workers / 0.5 seconds) migrate automatically; customized replay settings are preserved.
- Rust-backed `ahocorasick-rs` accelerates literal keyword discovery and releases the GIL while matching.
- `selectolax`/Lexbor accelerates HTML title/text/link extraction, with the existing Python parser retained as a fallback.
- Literal prefiltering now discovers candidates and positive matches in one traversal instead of scanning normalized fields twice.
- Document hashing reuses the already-normalized visible body instead of normalizing it again.

## Text formats

Archive Scout now explicitly treats `.htm`, `.shtm`, `.dhtm`, `.xhtm`, `.phtm`, `.cgi`, `.php`, `.dat`, and `.txt` as scannable text-page formats, alongside the existing HTML/XML/JSON/script formats.

## Index-only reports

`Index URLs only` now writes `reports/all_indexed_urls.txt`, `reports/summary.txt`, `reports/errors.txt`, and `reports/site_issues.txt` immediately after CDX indexing. `Regenerate reports only` also works on an index-only project even when no scan run exists.

## Compatibility

- Public version: 1.0.4.
- Database schema remains version 7.
- Existing projects remain compatible.
- Faster replay defaults retain coordinated HTTP 429/503 pauses, retries, bounded in-flight work, and saved resume state.

---

# Archive Scout 1.0.3

Archive Scout 1.0.3 is the final performance-focused release. It keeps the 1.0.2 interface and feature set, but replaces the slowest acquisition architecture and applies a full hot-path optimization pass across indexing, media, scanning, analysis, reports, SQLite, and Research Intelligence.

## Indexing

- Automatic broad indexing is now resume-key-first instead of numbered-page-first.
- Default resume batches increase from 50,000 to 100,000 CDX rows.
- Existing unfinished 1.0.2 numbered queues are converted safely on resume; already indexed captures remain in SQLite.
- `urlkey` is retained in CDX fields so continuation ordering is explicit and reliable while compact stored row shape remains unchanged.
- Healthy target-years begin as one large resumable window and subdivide only when Wayback actually times out or rejects the request.
- Explicit paged indexing remains available for compatibility and troubleshooting.
- Shared process-wide request pacing, host gating, finite 429/503 pause budgets, transport fallback, and exact saved recovery state remain intact.

## Media

- Direct media indexing uses the same resume-key-first traversal.
- Automatic `page_blocks=0` is no longer capable of degrading into `pageSize=1`.
- Earliest/latest exact embedded-media lookups request only the capture needed instead of traversing a large result set.
- Embedded discovery avoids a second full HTML parser pass and batches local discovery writes.
- External asset/media lookups use bounded concurrency under the same shared Wayback limiter.
- Media extension and allow/exclude policies are compiled and reused instead of rebuilt for every candidate URL.

## Local scanning and analysis

- Literal candidate collection reuses output sets instead of allocating one per field.
- Regex matches are streamed instead of materialized into temporary lists.
- Proximity scoring uses ordered two-pointer distance calculation instead of Cartesian keyword-position comparisons.
- Single-keyword matches skip sentence/paragraph/proximity passes that cannot change their score.
- HTML title extraction is folded into the existing parser pass; URL extraction and extension checks avoid unnecessary temporary objects.
- Duplicate SimHash generation, snapshot comparison, first-appearance searching, extraction work, and analysis writes were tightened for large projects.

## Database, reports, and Research Intelligence

- Compact CDX rows are unpacked once per insert instead of repeating mapping/position work for every field.
- Error identity lookups are sargable and backed by targeted indexes.
- Additional indexes accelerate document-match, duplicate, forum, and legacy-asset relationship queries.
- Research Intelligence bulk-fetches candidate entities/relationships and avoids project-sized Python sets during stale-vector cleanup.
- Evidence graph rebuilding is skipped when its dependencies are unchanged.
- Report replacement and several analysis stages avoid unnecessary filesystem/database work.

## Compatibility

- Public version: 1.0.3.
- Database schema remains version 7.
- Existing 1.0.0–1.0.2 projects remain supported.
- GUI, CLI/bot automation, AI providers, external embedded media, review/report workflows, project recovery, diagnostics, and Research Intelligence remain available.
