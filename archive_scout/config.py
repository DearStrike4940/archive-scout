from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path

from .constants import DEFAULT_IMAGE_EXTENSIONS, DEFAULT_VIDEO_EXTENSIONS, VERSION
from .scanning.keywords import keyword_rules_to_lines, parse_keyword_rules
from .utils import atomic_write_text, normalize_cdx_date, normalize_target, parse_cdx_parameter_lines


def normalize_extension(value: str) -> str:
    value = value.strip().casefold()
    if not value:
        return ""
    return value if value.startswith(".") else "." + value




REPORT_FIELD_NAMES: dict[str, tuple[str, ...]] = {
    "matches_ranked": (
        "rank", "score", "scan_run", "timestamp", "title", "original_url",
        "wayback_url", "local_file", "mime_type", "review_status", "tags",
        "note", "keyword_hits", "snippets", "interesting_links",
    ),
    "matched_urls": ("original_url",),
    "wayback_urls": ("wayback_url",),
    "interesting_links": ("source_url", "link"),
    "keyword_counts": ("count", "keyword"),
    "all_indexed_urls": ("timestamp", "mime_type", "state", "original_url"),
    "errors": (
        "last_seen", "operation", "category", "attempts", "retryable",
        "http_status", "timestamp", "source", "message",
    ),
    "site_issues": (
        "last_seen", "host", "stage", "category", "http_status", "occurrences", "message",
    ),
    "summary": (
        "heading", "generated", "output_directory", "operation", "scan_run",
        "keyword_set", "keyword_rules", "source_operation", "scan_started",
        "scan_completed", "targets", "date_range", "indexed_captures",
        "ranked_matches", "unresolved_errors", "site_issues", "states",
    ),
    "media_indexed": ("timestamp", "media_kind", "extension", "state", "original_url"),
    "media_downloaded": ("timestamp", "media_kind", "bytes_saved", "local_file", "original_url"),
    "media_wayback_urls": ("wayback_url",),
    "media_errors": ("last_seen", "category", "attempts", "timestamp", "original_url", "message"),
    "media_summary": (
        "heading", "generated", "indexed_media", "downloaded", "pending",
        "errors", "unresolved_media_errors", "snapshot_strategy",
        "included_extensions", "excluded_extensions",
    ),
    "analysis_summary": (
        "heading", "documents_processed", "forum_threads", "forum_posts", "extractions",
        "legacy_assets", "external_assets_found", "exact_duplicate_groups",
        "near_duplicate_groups", "grouped_documents", "snapshot_pairs",
        "changed_snapshot_pairs", "first_appearances", "provenance_edges",
    ),
    "forum_threads": (
        "canonical_key", "canonical_url", "title", "profile", "first_timestamp",
        "last_timestamp", "post_count", "document_count",
    ),
    "extractions": ("document_id", "extractor", "type", "field", "value", "context"),
    "legacy_assets": ("document_id", "url", "type", "player", "external", "archive_status", "context"),
    "duplicate_groups": ("group_id", "method", "representative_document_id", "document_id", "similarity"),
    "provenance": ("source_url", "source_timestamp", "mirror_url", "mirror_timestamp", "method", "similarity"),
    "snapshot_diffs": ("earlier_url", "earlier_timestamp", "later_timestamp", "summary_json"),
    "first_appearances": ("query", "original_url", "first_timestamp", "last_timestamp"),
}

REPORT_OUTPUT_NAMES = tuple(REPORT_FIELD_NAMES)
RANKED_REPORT_FIELDS = REPORT_FIELD_NAMES["matches_ranked"]
SUMMARY_REPORT_FIELDS = REPORT_FIELD_NAMES["summary"]
MEDIA_SUMMARY_FIELDS = REPORT_FIELD_NAMES["media_summary"]


def _ordered_enabled(values: list[str], allowed: tuple[str, ...]) -> list[str]:
    enabled = {str(value).strip() for value in values if str(value).strip()}
    return [name for name in allowed if name in enabled]


def _default_report_fields() -> dict[str, list[str]]:
    return {name: list(fields) for name, fields in REPORT_FIELD_NAMES.items()}


@dataclass(slots=True)
class ReportConfig:
    """Presentation controls; never delete historical evidence or review state.

    Optional lean storage affects only enrichment of future scans. Acquisition
    and Hitlist do not create these scan payloads in either mode.
    """

    outputs: list[str] = field(default_factory=lambda: list(REPORT_OUTPUT_NAMES))
    fields: dict[str, list[str]] = field(default_factory=_default_report_fields)
    retain_scan_details: bool = True
    sort_order: str = "score"
    max_matches: int = 0
    snippet_limit: int = 0
    snippet_chars: int = 0
    link_limit: int = 0

    def normalized(self) -> "ReportConfig":
        source = self.fields if isinstance(self.fields, dict) else {}
        return ReportConfig(
            outputs=_ordered_enabled(self.outputs, REPORT_OUTPUT_NAMES),
            fields={
                name: _ordered_enabled(list(source.get(name, REPORT_FIELD_NAMES[name])), REPORT_FIELD_NAMES[name])
                for name in REPORT_OUTPUT_NAMES
            },
            retain_scan_details=bool(self.retain_scan_details),
            sort_order=self.sort_order if self.sort_order in {"score", "oldest", "newest", "url"} else "score",
            max_matches=max(0, int(self.max_matches)),
            snippet_limit=max(0, int(self.snippet_limit)),
            snippet_chars=max(0, int(self.snippet_chars)),
            link_limit=max(0, int(self.link_limit)),
        )

    def output_enabled(self, name: str) -> bool:
        return name in self.outputs

    def fields_for(self, name: str) -> list[str]:
        return list(self.fields.get(name, ()))

    def field_enabled(self, output: str, field_name: str) -> bool:
        return field_name in self.fields.get(output, ())

    @property
    def ranked_fields(self) -> list[str]:
        return self.fields_for("matches_ranked")

    @property
    def summary_fields(self) -> list[str]:
        return self.fields_for("summary")

    @property
    def media_summary_fields(self) -> list[str]:
        return self.fields_for("media_summary")

    @property
    def store_keyword_counts(self) -> bool:
        return self.retain_scan_details or (
            self.output_enabled("keyword_counts") and bool(self.fields_for("keyword_counts"))
        ) or (
            self.output_enabled("matches_ranked") and self.field_enabled("matches_ranked", "keyword_hits")
        )

    @property
    def store_keyword_fields(self) -> bool:
        return self.retain_scan_details or (self.output_enabled("matches_ranked") and self.field_enabled("matches_ranked", "keyword_hits"))

    @property
    def store_keyword_details(self) -> bool:
        return self.store_keyword_counts or self.store_keyword_fields

    @property
    def store_snippets(self) -> bool:
        return self.retain_scan_details or (self.output_enabled("matches_ranked") and self.field_enabled("matches_ranked", "snippets"))

    @property
    def store_interesting_links(self) -> bool:
        standalone = self.output_enabled("interesting_links") and bool(self.fields_for("interesting_links"))
        ranked = self.output_enabled("matches_ranked") and self.field_enabled("matches_ranked", "interesting_links")
        return self.retain_scan_details or standalone or ranked

    @property
    def create_default_reviews(self) -> bool:
        return self.retain_scan_details or (self.output_enabled("matches_ranked") and self.field_enabled("matches_ranked", "review_status"))

    def to_payload(self) -> dict:
        return asdict(self.normalized())


@dataclass(slots=True)
class KeywordSetConfig:
    name: str
    rules: list[str] = field(default_factory=list)
    selected: bool = True

    def normalized(self) -> "KeywordSetConfig":
        name = self.name.strip() or "Keyword set"
        normalized_rules = keyword_rules_to_lines(parse_keyword_rules(self.rules))
        return KeywordSetConfig(name=name, rules=normalized_rules, selected=bool(self.selected))

    def to_payload(self) -> dict:
        return asdict(self.normalized())


@dataclass(slots=True)
class MediaConfig:
    enabled: bool = False
    targets: list[str] = field(default_factory=list)
    include_images: bool = True
    include_videos: bool = True
    include_extensions: list[str] = field(
        default_factory=lambda: list(DEFAULT_IMAGE_EXTENSIONS) + list(DEFAULT_VIDEO_EXTENSIONS)
    )
    exclude_extensions: list[str] = field(default_factory=list)
    discover_embedded: bool = True
    allow_external_embeds: bool = False
    snapshot_strategy: str = "earliest"
    max_file_mb: float = 500.0
    preserve_paths: bool = False

    def normalized(self) -> "MediaConfig":
        targets = list(dict.fromkeys(normalize_target(value) for value in self.targets if value.strip()))
        include = [normalize_extension(value) for value in self.include_extensions]
        exclude = [normalize_extension(value) for value in self.exclude_extensions]
        include = list(dict.fromkeys(value for value in include if value))
        exclude = list(dict.fromkeys(value for value in exclude if value))
        strategy = self.snapshot_strategy.strip().casefold()
        if strategy not in {"earliest", "latest", "all"}:
            raise ValueError("media snapshot strategy must be earliest, latest, or all")
        return MediaConfig(
            enabled=bool(self.enabled),
            targets=targets,
            include_images=bool(self.include_images),
            include_videos=bool(self.include_videos),
            include_extensions=include,
            exclude_extensions=exclude,
            discover_embedded=bool(self.discover_embedded),
            allow_external_embeds=bool(self.allow_external_embeds),
            snapshot_strategy=strategy,
            max_file_mb=max(0.1, float(self.max_file_mb)),
            preserve_paths=False,
        )

    @property
    def max_file_bytes(self) -> int:
        return int(self.max_file_mb * 1024 * 1024)

    def to_payload(self) -> dict:
        return asdict(self.normalized())


@dataclass(slots=True)
class AnalysisConfig:
    forum_profile: str = "auto"
    reconstruct_threads: bool = True
    extract_legacy_embeds: bool = True
    extractor_rules: list[str] = field(default_factory=list)
    search_external_assets: bool = False
    external_domains: list[str] = field(default_factory=list)
    external_asset_limit: int = 5000
    duplicate_threshold: float = 0.90
    compare_snapshots: bool = True
    build_provenance: bool = True
    merge_source: str = ""

    def normalized(self) -> "AnalysisConfig":
        profile = self.forum_profile.strip().casefold() or "auto"
        if profile not in {"auto", "generic", "vbulletin", "phpbb", "invision", "futaba", "2channel"}:
            raise ValueError("unsupported forum profile")
        domains = []
        for value in self.external_domains:
            value = value.strip().casefold()
            if value.startswith("http://") or value.startswith("https://"):
                from urllib.parse import urlsplit
                value = urlsplit(value).hostname or ""
            value = value.strip("./")
            if value and value not in domains:
                domains.append(value)
        rules = [value.strip() for value in self.extractor_rules if value.strip()]
        return AnalysisConfig(
            forum_profile=profile,
            reconstruct_threads=bool(self.reconstruct_threads),
            extract_legacy_embeds=bool(self.extract_legacy_embeds),
            extractor_rules=rules,
            search_external_assets=bool(self.search_external_assets),
            external_domains=domains,
            external_asset_limit=min(100000, max(1, int(self.external_asset_limit))),
            duplicate_threshold=min(1.0, max(0.5, float(self.duplicate_threshold))),
            compare_snapshots=bool(self.compare_snapshots),
            build_provenance=bool(self.build_provenance),
            merge_source=str(self.merge_source).strip(),
        )

    def to_payload(self) -> dict:
        return asdict(self.normalized())


@dataclass(slots=True)
class AIConfig:
    provider: str = "openai"
    model: str = "gpt-5-mini"
    candidate_limit: int = 200
    batch_size: int = 8
    minimum_relevance: int = 50
    excerpt_chars: int = 5000
    request_timeout: float = 120.0
    max_output_tokens: int = 1600

    def normalized(self) -> "AIConfig":
        provider = self.provider.strip().casefold() or "openai"
        if provider not in {"openai", "openrouter"}:
            raise ValueError("AI provider must be openai or openrouter")
        default_model = "anthropic/claude-sonnet-4.5" if provider == "openrouter" else "gpt-5-mini"
        model = self.model.strip() or default_model
        return AIConfig(
            provider=provider,
            model=model,
            candidate_limit=min(5000, max(10, int(self.candidate_limit))),
            batch_size=min(20, max(1, int(self.batch_size))),
            minimum_relevance=min(100, max(0, int(self.minimum_relevance))),
            excerpt_chars=min(16000, max(1000, int(self.excerpt_chars))),
            request_timeout=min(600.0, max(15.0, float(self.request_timeout))),
            max_output_tokens=min(12000, max(256, int(self.max_output_tokens))),
        )

    def to_payload(self) -> dict:
        return asdict(self.normalized())


@dataclass(slots=True)
class ResearchConfig:
    enabled: bool = True
    auto_build: bool = True
    vector_backend: str = "local-hash"
    vector_dimensions: int = 256
    candidate_limit: int = 3000
    result_limit: int = 200
    excerpt_chars: int = 3600
    entity_extraction: bool = True
    duplicate_clustering: bool = True
    ai_evidence_limit: int = 24

    def normalized(self) -> "ResearchConfig":
        backend = self.vector_backend.strip().casefold() or "local-hash"
        if backend not in {"local-hash", "fastembed"}:
            raise ValueError("research vector backend must be local-hash or fastembed")
        return ResearchConfig(
            enabled=bool(self.enabled),
            auto_build=bool(self.auto_build),
            vector_backend=backend,
            vector_dimensions=min(1024, max(64, int(self.vector_dimensions))),
            candidate_limit=min(20000, max(100, int(self.candidate_limit))),
            result_limit=min(2000, max(10, int(self.result_limit))),
            excerpt_chars=min(12000, max(800, int(self.excerpt_chars))),
            entity_extraction=bool(self.entity_extraction),
            duplicate_clustering=bool(self.duplicate_clustering),
            ai_evidence_limit=min(100, max(5, int(self.ai_evidence_limit))),
        )

    def to_payload(self) -> dict:
        return asdict(self.normalized())


@dataclass(slots=True)
class NetworkConfig:
    backend: str = "auto"
    trust_environment: bool = True
    endpoint_mode: str = "auto"
    index_strategy: str = "auto"
    page_blocks: int = 0
    cdx_workers: int = 10
    persistent_retries: bool = True
    retry_base_seconds: float = 5.0
    retry_max_seconds: float = 300.0
    failure_pause_threshold: int = 8
    connection_failure_pause_threshold: int = 3
    connection_retry_seconds: float = 3.0
    diagnostics: bool = True

    def normalized(self) -> "NetworkConfig":
        backend = self.backend.strip().casefold() or "auto"
        if backend not in {"auto", "httpx", "urllib3", "curl"}:
            raise ValueError("network backend must be auto, httpx, urllib3, or curl")
        endpoint = self.endpoint_mode.strip().casefold() or "auto"
        if endpoint not in {"auto", "cdx", "timemap"}:
            raise ValueError("CDX endpoint mode must be auto, cdx, or timemap")
        strategy = self.index_strategy.strip().casefold() or "auto"
        if strategy not in {"auto", "paged", "resume"}:
            raise ValueError("CDX index strategy must be auto, paged, or resume")
        return NetworkConfig(
            backend=backend,
            trust_environment=bool(self.trust_environment),
            endpoint_mode=endpoint,
            index_strategy=strategy,
            page_blocks=min(50, max(0, int(self.page_blocks))),
            cdx_workers=min(12, max(1, int(self.cdx_workers))),
            persistent_retries=bool(self.persistent_retries),
            retry_base_seconds=max(1.0, float(self.retry_base_seconds)),
            retry_max_seconds=max(float(self.retry_base_seconds), float(self.retry_max_seconds)),
            failure_pause_threshold=min(100, max(2, int(self.failure_pause_threshold))),
            connection_failure_pause_threshold=min(10, max(2, int(self.connection_failure_pause_threshold))),
            connection_retry_seconds=min(30.0, max(1.0, float(self.connection_retry_seconds))),
            diagnostics=bool(self.diagnostics),
        )

    def to_payload(self) -> dict:
        return asdict(self.normalized())


@dataclass(slots=True)
class ProjectConfig:
    output_dir: Path
    targets: list[str]
    keywords: list[str]
    keyword_set_name: str = "Current keywords"
    keyword_sets: list[KeywordSetConfig | dict] = field(default_factory=list)
    from_year: int = 2000
    to_year: int = datetime.now().year
    from_date: str = ""
    to_date: str = ""
    cdx_filters: list[str] = field(default_factory=lambda: ["statuscode:200"])
    cdx_collapses: list[str] = field(default_factory=lambda: ["urlkey"])
    cdx_match_type: str = ""
    cdx_extra_params: list[str] = field(default_factory=list)
    workers: int = 10
    scan_workers: int = 0
    download_scope: str = "all_text"
    minimum_score: int = 1
    report: ReportConfig | dict = field(default_factory=ReportConfig)
    max_file_mb: float = 25.0
    page_size: int = 100000
    cdx_delay: float = 0.75
    download_delay: float = 0.125
    retries: int = 4
    rate_limit_base_pause: float = 30.0
    rate_limit_max_pause: float = 300.0
    rate_limit_max_wait: float = 900.0
    rate_limit_attempts: int = 8
    connect_timeout: float = 30.0
    read_timeout: float = 180.0
    max_attempts: int = 4
    user_agent: str = "ArchiveScout/1.0 public web archive research client"
    retry_error_categories: list[str] = field(default_factory=list)
    retry_capture_ids: list[int] = field(default_factory=list)
    retry_media_capture_ids: list[int] = field(default_factory=list)
    media: MediaConfig | dict = field(default_factory=MediaConfig)
    analysis: AnalysisConfig | dict = field(default_factory=AnalysisConfig)
    ai: AIConfig | dict = field(default_factory=AIConfig)
    research: ResearchConfig | dict = field(default_factory=ResearchConfig)
    network: NetworkConfig | dict = field(default_factory=NetworkConfig)
    target_settings: dict[str, dict] = field(default_factory=dict)
    auto_backup: bool = False
    backup_keep: int = 5
    backup_max_mb: float = 1024.0
    compact_storage: bool = True
    hitlist_keywords: list[str] = field(default_factory=list)
    hitlist_file: str = ""
    import_source: str = ""

    def normalized_keyword_sets(self) -> list[KeywordSetConfig]:
        sets: list[KeywordSetConfig] = []
        for value in self.keyword_sets:
            if isinstance(value, KeywordSetConfig):
                item = value
            elif isinstance(value, dict):
                item = KeywordSetConfig(
                    name=str(value.get("name") or "Keyword set"),
                    rules=list(value.get("rules") or value.get("keywords") or []),
                    selected=bool(value.get("selected", True)),
                )
            else:
                continue
            item = item.normalized()
            if item.rules:
                sets.append(item)
        if not sets and self.keywords:
            sets.append(KeywordSetConfig(self.keyword_set_name, list(self.keywords), True).normalized())
        unique: dict[str, KeywordSetConfig] = {}
        for item in sets:
            base = item.name
            name = base
            suffix = 2
            while name.casefold() in unique:
                name = f"{base} {suffix}"
                suffix += 1
            if name != item.name:
                item = KeywordSetConfig(name, item.rules, item.selected)
            unique[name.casefold()] = item
        return list(unique.values())

    def selected_keyword_sets(self) -> list[KeywordSetConfig]:
        return [item for item in self.normalized_keyword_sets() if item.selected]

    def normalized(self) -> "ProjectConfig":
        targets = list(dict.fromkeys(normalize_target(value) for value in self.targets if value.strip()))
        keyword_sets = self.normalized_keyword_sets()
        first = keyword_sets[0] if keyword_sets else KeywordSetConfig(self.keyword_set_name, [], True)
        output_dir = Path(self.output_dir).expanduser().resolve()
        from_date = normalize_cdx_date(self.from_date or str(self.from_year), end=False)
        to_date = normalize_cdx_date(self.to_date or str(self.to_year), end=True)
        filters = list(dict.fromkeys(value.strip() for value in self.cdx_filters if value.strip()))
        collapses = list(dict.fromkeys(value.strip() for value in self.cdx_collapses if value.strip()))
        match_type = self.cdx_match_type.strip()
        if match_type not in {"", "exact", "prefix", "host", "domain"}:
            raise ValueError("matchType must be exact, prefix, host, domain, or blank")
        extra_params = [f"{key}={value}" for key, value in parse_cdx_parameter_lines(self.cdx_extra_params)]
        media = self.media if isinstance(self.media, MediaConfig) else MediaConfig(**self.media)
        media = media.normalized()
        report = self.report if isinstance(self.report, ReportConfig) else ReportConfig(**self.report)
        report = report.normalized()
        analysis = self.analysis if isinstance(self.analysis, AnalysisConfig) else AnalysisConfig(**self.analysis)
        analysis = analysis.normalized()
        ai = self.ai if isinstance(self.ai, AIConfig) else AIConfig(**self.ai)
        ai = ai.normalized()
        research = self.research if isinstance(self.research, ResearchConfig) else ResearchConfig(**self.research)
        research = research.normalized()
        network = self.network if isinstance(self.network, NetworkConfig) else NetworkConfig(**self.network)
        network = network.normalized()
        target_settings: dict[str, dict] = {}
        for raw_target, raw_settings in (self.target_settings or {}).items():
            target = normalize_target(str(raw_target))
            if not target or not isinstance(raw_settings, dict):
                continue
            cleaned = {str(key): value for key, value in raw_settings.items() if value not in (None, "", [], {})}
            if cleaned:
                target_settings[target] = cleaned
        return ProjectConfig(
            output_dir=output_dir,
            targets=targets,
            keywords=list(first.rules),
            keyword_set_name=first.name,
            keyword_sets=keyword_sets,
            from_year=int(from_date[:4]),
            to_year=int(to_date[:4]),
            from_date=from_date,
            to_date=to_date,
            cdx_filters=filters,
            cdx_collapses=collapses,
            cdx_match_type=match_type,
            cdx_extra_params=extra_params,
            workers=min(32, max(1, int(self.workers))),
            scan_workers=min(32, max(0, int(self.scan_workers))),
            download_scope=self.download_scope if self.download_scope in {"all_text", "keyword_urls", "index_only"} else "all_text",
            minimum_score=max(1, int(self.minimum_score)),
            report=report,
            max_file_mb=max(0.1, float(self.max_file_mb)),
            page_size=min(150000, max(100, int(self.page_size))),
            cdx_delay=max(0.0, float(self.cdx_delay)),
            download_delay=max(0.0, float(self.download_delay)),
            retries=min(12, max(1, int(self.retries))),
            rate_limit_base_pause=max(1.0, float(self.rate_limit_base_pause)),
            rate_limit_max_pause=max(float(self.rate_limit_base_pause), float(self.rate_limit_max_pause)),
            rate_limit_max_wait=(900.0 if float(self.rate_limit_max_wait) <= 0 else max(30.0, float(self.rate_limit_max_wait))),
            rate_limit_attempts=(8 if int(self.rate_limit_attempts) <= 0 else min(1000, int(self.rate_limit_attempts))),
            connect_timeout=max(1.0, float(self.connect_timeout)),
            read_timeout=max(1.0, float(self.read_timeout)),
            max_attempts=min(20, max(1, int(self.max_attempts))),
            user_agent=self.user_agent.strip() or "ArchiveScout/1.0 public web archive research client",
            retry_error_categories=list(dict.fromkeys(value.strip() for value in self.retry_error_categories if value.strip())),
            retry_capture_ids=sorted({int(value) for value in self.retry_capture_ids if int(value) > 0}),
            retry_media_capture_ids=sorted({int(value) for value in self.retry_media_capture_ids if int(value) > 0}),
            media=media,
            analysis=analysis,
            ai=ai,
            research=research,
            network=network,
            target_settings=target_settings,
            auto_backup=bool(self.auto_backup),
            backup_keep=min(50, max(1, int(self.backup_keep))),
            backup_max_mb=max(64.0, float(self.backup_max_mb)),
            compact_storage=bool(self.compact_storage),
            hitlist_keywords=list(dict.fromkeys(str(value).strip() for value in self.hitlist_keywords if str(value).strip())),
            hitlist_file=str(self.hitlist_file).strip(),
            import_source=str(self.import_source).strip(),
        )

    def settings_for_target(self, target: str) -> dict:
        normalized = normalize_target(target)
        return dict(self.target_settings.get(normalized) or {})

    def for_target(self, target: str) -> "ProjectConfig":
        settings = self.settings_for_target(target)
        allowed = {
            "from_date", "to_date", "cdx_filters", "cdx_collapses", "cdx_match_type",
            "cdx_extra_params", "page_size", "cdx_delay", "download_delay", "workers", "scan_workers",
        }
        overrides = {key: value for key, value in settings.items() if key in allowed}
        return replace(self, targets=[normalize_target(target)], **overrides).normalized()

    @property
    def max_file_bytes(self) -> int:
        return int(self.max_file_mb * 1024 * 1024)

    def to_payload(self) -> dict:
        config = self.normalized()
        payload = asdict(config)
        payload["output_dir"] = str(config.output_dir)
        payload["keyword_sets"] = [item.to_payload() for item in config.normalized_keyword_sets()]
        payload["media"] = config.media.to_payload() if isinstance(config.media, MediaConfig) else dict(config.media)
        payload["report"] = config.report.to_payload() if isinstance(config.report, ReportConfig) else dict(config.report)
        payload["analysis"] = config.analysis.to_payload() if isinstance(config.analysis, AnalysisConfig) else dict(config.analysis)
        payload["ai"] = config.ai.to_payload() if isinstance(config.ai, AIConfig) else dict(config.ai)
        payload["research"] = config.research.to_payload() if isinstance(config.research, ResearchConfig) else dict(config.research)
        payload["network"] = config.network.to_payload() if isinstance(config.network, NetworkConfig) else dict(config.network)
        payload["version"] = VERSION
        return payload


def save_project_config(config: ProjectConfig) -> Path:
    config = config.normalized()
    path = config.output_dir / "project.json"
    atomic_write_text(path, json.dumps(config.to_payload(), indent=2, ensure_ascii=False) + "\n")
    return path


def load_project_config(path: Path) -> ProjectConfig:
    payload = json.loads(path.read_text(encoding="utf-8"))
    keyword_sets = list(payload.get("keyword_sets") or [])
    media_payload = payload.get("media") or {}
    report_payload = payload.get("report") or {}
    analysis_payload = payload.get("analysis") or {}
    ai_payload = payload.get("ai") or {}
    research_payload = payload.get("research") or {}
    network_payload = payload.get("network") or {}
    saved_version = str(payload.get("version") or "")
    loaded_page_size = int(payload.get("page_size", 100000))
    loaded_cdx_delay = float(payload.get("cdx_delay", 0.75))
    loaded_page_blocks = int(network_payload.get("page_blocks", 0))
    loaded_cdx_workers = int(network_payload.get("cdx_workers", 10))
    loaded_workers = int(payload.get("workers", 10))
    loaded_download_delay = float(payload.get("download_delay", 0.125))

    # Preserve compatibility with early prerelease projects without exposing old
    # product branding. Only untouched historical default combinations are
    # upgraded; user-customized transport settings remain exactly as saved.
    if loaded_page_size == 5000 and loaded_cdx_delay == 1.0 and loaded_page_blocks == 1:
        loaded_page_size = 25000
        loaded_cdx_delay = 0.75
        loaded_page_blocks = 6
        if "cdx_workers" not in network_payload:
            loaded_cdx_workers = 6
    untouched_legacy_defaults = (
        loaded_page_size == 25000
        and loaded_cdx_delay == 0.75
        and loaded_page_blocks == 6
        and loaded_cdx_workers == 6
    )
    if untouched_legacy_defaults:
        loaded_page_size = 50000
        loaded_page_blocks = 0
        loaded_cdx_workers = 10
    if (
        loaded_page_size == 50000
        and loaded_cdx_delay == 0.75
        and loaded_page_blocks == 9
        and loaded_cdx_workers == 10
        and "page_blocks" in network_payload
    ):
        loaded_page_blocks = 0
    # v1.0.2 used 50,000-row resume pages and automatic numbered paging.
    # When that complete untouched transport profile is encountered, upgrade it
    # to the final fast defaults. Deliberately customized network profiles keep
    # their saved values.
    if (
        loaded_page_size == 50000
        and loaded_cdx_delay == 0.75
        and loaded_page_blocks == 0
        and loaded_cdx_workers == 10
        and str(network_payload.get("index_strategy", "auto")).casefold() == "auto"
    ):
        loaded_page_size = 100000
    # v1.0.5 makes automatic indexing follow the fast standalone downloader's
    # Timemap pattern: page-count once, then keep ten page requests continuously
    # in flight using the historical pageSize=9 grouping. Upgrade only the
    # untouched v1.0.4 automatic indexing profile.
    if (
        saved_version not in {"1.0.6.2", "1.0.6.3", "1.0.6.4", "1.0.6.5", "1.0.6.6", "1.0.7", "1.0.7.1"}
        and loaded_page_size == 100000
        and loaded_cdx_delay == 0.75
        and loaded_page_blocks == 0
        and loaded_cdx_workers == 10
        and str(network_payload.get("index_strategy", "auto")).casefold() == "auto"
    ):
        loaded_page_blocks = 9
    # A short-lived v1.0.7 draft used one Interesting Links boolean. Preserve
    # that preference when opening such a project while upgrading it to the
    # comprehensive report configuration.
    if not report_payload and payload.get("include_interesting_links") is False:
        compatible_fields = _default_report_fields()
        compatible_fields["matches_ranked"] = [
            name for name in RANKED_REPORT_FIELDS if name != "interesting_links"
        ]
        report_payload = {
            "outputs": [name for name in REPORT_OUTPUT_NAMES if name != "interesting_links"],
            "fields": compatible_fields,
        }

    report_fields_payload = report_payload.get("fields")
    if not isinstance(report_fields_payload, dict):
        # Upgrade the first v1.0.7 draft, which exposed only three specialized
        # field lists, into the complete per-output/per-field matrix.
        report_fields_payload = _default_report_fields()
        if "ranked_fields" in report_payload:
            report_fields_payload["matches_ranked"] = list(report_payload["ranked_fields"])
        if "summary_fields" in report_payload:
            report_fields_payload["summary"] = list(report_payload["summary_fields"])
        if "media_summary_fields" in report_payload:
            report_fields_payload["media_summary"] = list(report_payload["media_summary_fields"])
    else:
        report_fields_payload = {
            str(name): list(values) if isinstance(values, (list, tuple)) else []
            for name, values in report_fields_payload.items()
        }

    # v1.0.3 shipped conservative replay defaults (4 workers, 0.5 s
    # between request starts). Upgrade only that untouched pair to the v1.0.4
    # high-throughput replay profile: ten persistent connections and eight
    # request starts per second. Any custom value remains user-controlled.
    if loaded_workers == 4 and loaded_download_delay == 0.5:
        loaded_workers = 10
        loaded_download_delay = 0.125
    return ProjectConfig(
        output_dir=Path(payload.get("output_dir") or path.parent),
        targets=list(payload.get("targets") or []),
        keywords=list(payload.get("keywords") or []),
        keyword_set_name=str(payload.get("keyword_set_name") or "Current keywords"),
        keyword_sets=keyword_sets,
        from_year=int(payload.get("from_year", 2000)),
        to_year=int(payload.get("to_year", datetime.now().year)),
        from_date=str(payload.get("from_date") or payload.get("from_year", 2000)),
        to_date=str(payload.get("to_date") or payload.get("to_year", datetime.now().year)),
        cdx_filters=list(payload["cdx_filters"]) if "cdx_filters" in payload else ["statuscode:200"],
        cdx_collapses=list(payload["cdx_collapses"]) if "cdx_collapses" in payload else ["urlkey"],
        cdx_match_type=str(payload.get("cdx_match_type", "")),
        cdx_extra_params=list(payload.get("cdx_extra_params") or []),
        workers=loaded_workers,
        scan_workers=int(payload.get("scan_workers", 0)),
        download_scope=str(payload.get("download_scope", "all_text")),
        minimum_score=int(payload.get("minimum_score", 1)),
        report=ReportConfig(
            retain_scan_details=bool(report_payload.get("retain_scan_details", True)),
            sort_order=str(report_payload.get("sort_order", "score")),
            max_matches=int(report_payload.get("max_matches", 0)),
            snippet_limit=int(report_payload.get("snippet_limit", 0)),
            snippet_chars=int(report_payload.get("snippet_chars", 0)),
            link_limit=int(report_payload.get("link_limit", 0)),
            outputs=list(report_payload["outputs"]) if "outputs" in report_payload else list(REPORT_OUTPUT_NAMES),
            fields={
                name: (
                    list(report_fields_payload[name])
                    if name in report_fields_payload
                    else list(REPORT_FIELD_NAMES[name])
                )
                for name in REPORT_OUTPUT_NAMES
            },
        ),
        max_file_mb=float(payload.get("max_file_mb", 25.0)),
        page_size=loaded_page_size,
        cdx_delay=loaded_cdx_delay,
        download_delay=loaded_download_delay,
        retries=int(payload.get("retries", 4)),
        rate_limit_base_pause=float(payload.get("rate_limit_base_pause", 30.0)),
        rate_limit_max_pause=float(payload.get("rate_limit_max_pause", 300.0)),
        rate_limit_max_wait=float(payload.get("rate_limit_max_wait", 900.0)),
        rate_limit_attempts=int(payload.get("rate_limit_attempts", 8)),
        connect_timeout=float(payload.get("connect_timeout", 30.0)),
        read_timeout=float(payload.get("read_timeout", 180.0)),
        max_attempts=int(payload.get("max_attempts", 4)),
        user_agent=str(payload.get("user_agent", "ArchiveScout/1.0 public web archive research client")),
        retry_error_categories=list(payload.get("retry_error_categories") or []),
        retry_capture_ids=[int(value) for value in payload.get("retry_capture_ids") or []],
        retry_media_capture_ids=[int(value) for value in payload.get("retry_media_capture_ids") or []],
        media=MediaConfig(
            enabled=bool(media_payload.get("enabled", False)),
            targets=list(media_payload.get("targets") or []),
            include_images=bool(media_payload.get("include_images", True)),
            include_videos=bool(media_payload.get("include_videos", True)),
            include_extensions=list(media_payload.get("include_extensions") or list(DEFAULT_IMAGE_EXTENSIONS) + list(DEFAULT_VIDEO_EXTENSIONS)),
            exclude_extensions=list(media_payload.get("exclude_extensions") or []),
            discover_embedded=bool(media_payload.get("discover_embedded", True)),
            allow_external_embeds=bool(media_payload.get("allow_external_embeds", False)),
            snapshot_strategy=str(media_payload.get("snapshot_strategy", "earliest")),
            max_file_mb=float(media_payload.get("max_file_mb", 500.0)),
            preserve_paths=False,
        ),
        analysis=AnalysisConfig(
            forum_profile=str(analysis_payload.get("forum_profile", "auto")),
            reconstruct_threads=bool(analysis_payload.get("reconstruct_threads", True)),
            extract_legacy_embeds=bool(analysis_payload.get("extract_legacy_embeds", True)),
            extractor_rules=list(analysis_payload.get("extractor_rules") or []),
            search_external_assets=bool(analysis_payload.get("search_external_assets", False)),
            external_domains=list(analysis_payload.get("external_domains") or []),
            external_asset_limit=int(analysis_payload.get("external_asset_limit", 5000)),
            duplicate_threshold=float(analysis_payload.get("duplicate_threshold", 0.90)),
            compare_snapshots=bool(analysis_payload.get("compare_snapshots", True)),
            build_provenance=bool(analysis_payload.get("build_provenance", True)),
            merge_source=str(analysis_payload.get("merge_source", "")),
        ),
        ai=AIConfig(
            provider=str(ai_payload.get("provider", "openai")),
            model=str(ai_payload.get("model", "gpt-5-mini")),
            candidate_limit=int(ai_payload.get("candidate_limit", 200)),
            batch_size=int(ai_payload.get("batch_size", 8)),
            minimum_relevance=int(ai_payload.get("minimum_relevance", 50)),
            excerpt_chars=int(ai_payload.get("excerpt_chars", 5000)),
            request_timeout=float(ai_payload.get("request_timeout", 120.0)),
            max_output_tokens=int(ai_payload.get("max_output_tokens", 1600)),
        ),
        research=ResearchConfig(
            enabled=bool(research_payload.get("enabled", True)),
            auto_build=bool(research_payload.get("auto_build", True)),
            vector_backend=str(research_payload.get("vector_backend", "local-hash")),
            vector_dimensions=int(research_payload.get("vector_dimensions", 256)),
            candidate_limit=int(research_payload.get("candidate_limit", 3000)),
            result_limit=int(research_payload.get("result_limit", 200)),
            excerpt_chars=int(research_payload.get("excerpt_chars", 3600)),
            entity_extraction=bool(research_payload.get("entity_extraction", True)),
            duplicate_clustering=bool(research_payload.get("duplicate_clustering", True)),
            ai_evidence_limit=int(research_payload.get("ai_evidence_limit", 24)),
        ),
        network=NetworkConfig(
            backend=str(network_payload.get("backend", "auto")),
            trust_environment=bool(network_payload.get("trust_environment", True)),
            endpoint_mode=str(network_payload.get("endpoint_mode", "auto")),
            index_strategy=str(network_payload.get("index_strategy", "auto")),
            page_blocks=loaded_page_blocks,
            cdx_workers=loaded_cdx_workers,
            persistent_retries=bool(network_payload.get("persistent_retries", True)),
            retry_base_seconds=float(network_payload.get("retry_base_seconds", 5.0)),
            retry_max_seconds=float(network_payload.get("retry_max_seconds", 300.0)),
            failure_pause_threshold=int(network_payload.get("failure_pause_threshold", 8)),
            connection_failure_pause_threshold=int(network_payload.get("connection_failure_pause_threshold", 3)),
            connection_retry_seconds=float(network_payload.get("connection_retry_seconds", 3.0)),
            diagnostics=bool(network_payload.get("diagnostics", True)),
        ),
        target_settings=dict(payload.get("target_settings") or {}),
        auto_backup=bool(payload.get("auto_backup", False)),
        backup_keep=int(payload.get("backup_keep", 5)),
        backup_max_mb=float(payload.get("backup_max_mb", 1024.0)),
        compact_storage=bool(payload.get("compact_storage", True)),
        hitlist_keywords=list(payload.get("hitlist_keywords") or []),
        hitlist_file=str(payload.get("hitlist_file") or ""),
        import_source=str(payload.get("import_source", "")),
    ).normalized()
