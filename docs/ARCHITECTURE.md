# Architecture

Archive Scout is a project-oriented desktop application. The UI is deliberately separated from the indexing, downloading, scanning, media, AI, database, and analysis engines so long-running work can persist independently of what is currently displayed.

## Project storage

Each project contains `archive_scout.sqlite3`, downloaded captures, downloaded media, reports, backups, and derived exports. SQLite is the authoritative work-state store. The current schema version is 8; v1.0.7.1 does not change it.

Project connections use WAL mode, normal synchronous behavior, memory temporary storage, a bounded cache, memory mapping, busy timeouts, and foreign keys. Large queues are read with keyset pagination rather than large OFFSET scans.

## Indexing

The CDX layer builds request signatures from target/date/settings and persists work by target and time window. Broad requests can use numbered pages; resume-key requests are used where appropriate. The engine retains fixed shared request-start pacing while allowing bounded overlap so slow Wayback responses do not serialize otherwise independent work.

Transient failures can rotate transport backends/endpoints and subdivide the failed date window. Permanent archive-policy failures are recorded as site issues rather than retried indefinitely.

## Text downloading and scanning

Pending captures are selected from SQLite, with known smaller downloads scheduled first. Replay completes before local scan workers drain the durable `downloaded_unscanned` backlog. A scan failure is recorded once per operation and remains available for a later retry. Download-only bypasses document/scoring state, batches completion writes, and preserves paths before requests so completed files can be adopted after a crash.

New text captures have `.txt` filenames while retaining original bytes. Classification uses archive metadata and bounded byte inspection; recognized binary signatures override incorrect text MIME headers. Ambiguous content remains eligible for inspection, including legacy URLs. HTTP charset hints are retained for later decoding. Scanning and Hitlist use bounded native match batches while retaining cross-batch matches.

Report configuration controls presentation independently of acquired evidence. Output toggles do not clear stored scan history. Optional lean enrichment applies to future scans only, and later richer reports may require rescanning local files.

Literal rule prefiltering uses an Aho-Corasick automaton. Full scoring retains required, excluded, exact, regular-expression, whole-word, field, weight, and proximity semantics.

## External media

External media is intentionally a second-stage pipeline after text download and scan. Page content is mined for direct, lazy, CSS, social-metadata, legacy-player, and other embedded image/video references. Candidates are persisted in `media_discovery_queue`; unchanged source documents are tracked by content hash in `media_discovery_documents`.

Exact Wayback lookups are bounded and rate-limited. Resolved captures enter the normal media capture table and are streamed directly to disk. Excluded/robots-blocked hosts can be short-circuited at discovery/index/download stages.

## AI relevance

AI relevance is a separate analysis layer over existing deterministic matches. `ai_runs` stores the prompt/model/run metadata and `ai_results` stores one relevance result per match. The normal match score, review status, notes, tags, and scan data are not modified.

Only a bounded candidate set is sent to the configured AI provider. Archive Scout currently implements the OpenAI Responses API and uses strict structured output.

## UI threading

Tk owns the UI thread. Operations run in worker threads and communicate using a bounded/coalescing event queue. Database connections used by workers are opened inside their owning thread. The UI never relies on subprocesses for its core research workflow.

## Safety

Project merge, repair, backup, migration, export, and diagnostic operations use path containment and atomic-write patterns where applicable. The program never intentionally bypasses Wayback exclusions or robots restrictions.
