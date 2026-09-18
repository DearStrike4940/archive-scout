from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import traceback
import webbrowser
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from ..ai.relevance import AIReviewError, run_ai_review
from ..ai.reports import generate_ai_reports
from ..cdx.client import RateLimitDeferred
from ..cdx.parameters import build_cdx_params, build_num_pages_params, cdx_year_window, cdx_endpoints, cdx_paged_endpoints, preferred_index_strategy
from ..utils import cdx_request_url
from ..config import (
    AIConfig,
    AnalysisConfig,
    KeywordSetConfig,
    MediaConfig,
    NetworkConfig,
    ProjectConfig,
    ReportConfig,
    ResearchConfig,
    REPORT_FIELD_NAMES,
    REPORT_OUTPUT_NAMES,
    load_project_config,
    save_project_config,
)
from ..constants import APP_NAME, DEFAULT_IMAGE_EXTENSIONS, DEFAULT_VIDEO_EXTENSIONS, OPERATION_MODES, REVIEW_STATUSES, SCOPE_LABELS, VERSION
from ..database.connection import open_database
from ..database.repositories import (
    ai_result_rows,
    delete_scan_run,
    ignore_errors,
    list_ai_runs,
    list_error_categories,
    list_errors,
    list_scan_runs,
    list_site_issues,
    rename_scan_run,
    result_count,
    result_rows,
    resolve_site_issue,
    save_note,
    set_match_tags,
    set_review,
)
from ..defaults import PRESETS
from ..downloads.downloader import replay_url
from ..events import ConnectivityPaused, ProgressEvent, Stopped
from ..operations import run_project
from ..reports.compare import generate_scan_comparison
from ..reports.export import export_review_package, export_scan
from ..reports.text import generate_reports
from ..research.ai import run_grounded_answer
from ..research.search import search_research
from ..runtime import FrozenBundleError, bundled_resource, ensure_frozen_bundle_available
from ..scanning.full_text import search_documents
from ..utils import normalize_cdx_date
from ..projects.backups import list_project_backups, restore_project_backup
from .dashboard import read_dashboard_counts
from .event_queue import CoalescingEventQueue
from .theme import REVIEW_COLORS, apply_text_theme, apply_theme
from .widgets import ToolTip

MODE_LABELS = OPERATION_MODES
MODE_HELP = {
    "all": "Queries CDX, downloads pending text captures, scans every selected keyword set, and writes reports.",
    "download_only": "Indexes the target and downloads every text capture without creating scan jobs, documents, matches, research indexes, or scan reports. If Media is enabled, the same optional media pipeline runs afterward. Run Search with Hitlist whenever you want to search the saved text.",
    "external_media_after_scan": "Indexes the site, downloads and scans all selected text pages, then indexes only external media URLs found in those saved pages and downloads them after discovery finishes.",
    "index": "Queries CDX and stores capture metadata without downloading pages.",
    "download": "Downloads pending text captures and scans them with every selected keyword set.",
    "resume": "Continues interrupted pending work without automatically retrying earlier errors.",
    "rescan": "Reads saved files locally and creates new scan runs without making Wayback requests.",
    "retry_errors": "Retries unresolved text-page and media errors. Valid local text files are rescanned before redownloading.",
    "report": "Recreates reports from the latest completed scan without downloading or rescanning.",
    "integrity": "Checks saved files and database links without deleting project data.",
    "repair": "Creates a safety backup, repairs stuck states and missing-file records, and rebuilds full-text indexes.",
    "backup": "Creates a consistent SQLite backup inside the project backup folder.",
    "diagnostics": "Exports a diagnostic ZIP with sanitized settings, integrity information, recent errors, and network events.",
    "import_folder": "Imports an existing folder of saved HTML and text pages into this project.",
    "media_all": "Indexes and downloads the selected image and video extensions.",
    "media_index": "Indexes selected image and video URLs without downloading the files.",
    "media_download": "Downloads pending media records already stored in this project.",
    "media_retry": "Retries only unresolved media download errors.",
    "analysis": "Reconstructs forum threads, extracts identifiers and legacy embeds, clusters duplicates, compares snapshots, and builds provenance reports.",
    "research_index": "Builds or refreshes the local Research Intelligence vector, entity, duplicate, and evidence-relationship index without making Wayback requests.",
    "forum_rebuild": "Rebuilds forum threads and posts from saved pages without rerunning the rest of the archive analysis.",
    "merge_project": "Merges captures, downloads, scans, reviews, notes, tags, media, and extraction results from another Archive Scout project.",
    "hitlist": "Performs a fast literal search across every indexed URL and every locally saved capture without scoring or research enrichment.",
    "compact": "Safely compresses regenerable database text, rebuilds lean indexes, and uses exact-byte copy-on-write deduplication where supported.",
}
REVIEW_LABELS = {
    "Unreviewed": "unreviewed",
    "Relevant": "relevant",
    "Possibly relevant": "possibly_relevant",
    "False positive": "false_positive",
    "Duplicate": "duplicate",
    "Dead end": "dead_end",
    "Needs follow-up": "needs_follow_up",
}


def app_support_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "archive-scout"


def open_path(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif os.name == "nt":
        os.startfile(str(path))
    else:
        for command in ("xdg-open", "gio"):
            if shutil.which(command):
                subprocess.Popen([command, str(path)] if command == "xdg-open" else [command, "open", str(path)])
                return
        raise RuntimeError("No desktop file opener was found: " + str(path))


class ArchiveScoutApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} {VERSION}")
        self._app_icon: tk.PhotoImage | None = None
        self.apply_application_icon()
        self.geometry("1180x820")
        self.minsize(940, 680)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.events: CoalescingEventQueue[ProgressEvent] = CoalescingEventQueue(max_events=256)
        self.stop_event = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self.closing_after_stop = False
        self.last_paths: dict[str, Path] = {}
        self.keyword_sets: list[dict] = []
        self.current_keyword_set = -1
        self.scan_run_map: dict[str, int] = {}
        self.result_row_map: dict[str, dict] = {}
        self.error_row_map: dict[str, dict] = {}
        self.site_issue_row_map: dict[str, dict] = {}
        self.ai_run_map: dict[str, int] = {}
        self.ai_result_row_map: dict[str, dict] = {}
        self.research_result_row_map: dict[str, dict] = {}
        self.result_sort_column = "score"
        self.result_sort_reverse = True
        self.target_settings: dict[str, dict] = {}
        self.nav_buttons: dict[str, ttk.Button] = {}
        self.page_names: list[str] = []
        self.result_page = 0
        self.result_page_size = 500
        self.dashboard_refresh_job: str | None = None
        self.log_line_count = 0
        self.create_variables()
        self.resolved_theme, self.colors = apply_theme(self, self.theme_var.get(), float(self.font_scale_var.get()))
        self.create_ui()
        first_run = not self.state_path().exists()
        self.load_app_state()
        self.apply_interface_theme()
        self.output_var.trace_add("write", lambda *_args: self.after_idle(self.refresh_dashboard))
        self.after(100, self.process_events)
        self.dashboard_refresh_job = self.after(250, self.dashboard_refresh_loop)
        if first_run:
            self.after(450, self.show_welcome)


    def apply_application_icon(self) -> None:
        try:
            png_path = bundled_resource("assets", "archivescout.png")
            if png_path.exists():
                self._app_icon = tk.PhotoImage(file=str(png_path))
                self.iconphoto(True, self._app_icon)
            if os.name == "nt":
                ico_path = bundled_resource("assets", "archivescout.ico")
                if ico_path.exists():
                    self.iconbitmap(default=str(ico_path))
        except (OSError, tk.TclError):
            self._app_icon = None

    def create_variables(self) -> None:
        default_output = Path.home() / "Downloads" / "ArchiveScout"
        self.output_var = tk.StringVar(value=str(default_output))
        self.preset_var = tk.StringVar(value="General web archive research")
        self.mode_var = tk.StringVar(value="Index, download, scan, and report")
        self.operation_help_var = tk.StringVar(value=MODE_HELP["all"])
        self.scope_var = tk.StringVar(value="All archived text pages (thorough)")
        self.from_date_var = tk.StringVar(value="2001")
        self.to_date_var = tk.StringVar(value="2010")
        self.cdx_match_type_var = tk.StringVar(value="Automatic")
        self.collapse_urlkey_var = tk.BooleanVar(value=True)
        self.collapse_digest_var = tk.BooleanVar(value=False)
        self.page_size_var = tk.StringVar(value="100000")
        self.workers_var = tk.StringVar(value="10")
        self.scan_workers_var = tk.StringVar(value="0")
        self.max_file_var = tk.StringVar(value="25")
        self.minimum_score_var = tk.StringVar(value="1")
        self.report_output_vars = {name: tk.BooleanVar(value=True) for name in REPORT_OUTPUT_NAMES}
        self.report_retain_var = tk.BooleanVar(value=True)
        self.report_sort_var = tk.StringVar(value="score")
        self.report_max_var = tk.StringVar(value="0")
        self.report_snippets_var = tk.StringVar(value="0")
        self.report_chars_var = tk.StringVar(value="0")
        self.report_links_var = tk.StringVar(value="0")
        self.report_field_vars = {
            name: {field: tk.BooleanVar(value=True) for field in REPORT_FIELD_NAMES[name]}
            for name in REPORT_OUTPUT_NAMES
        }
        self.cdx_delay_var = tk.StringVar(value="0.75")
        self.download_delay_var = tk.StringVar(value="0.125")
        self.rate_limit_base_var = tk.StringVar(value="30")
        self.rate_limit_max_var = tk.StringVar(value="300")
        self.rate_limit_wait_var = tk.StringVar(value="15")
        self.status_var = tk.StringVar(value="Ready")
        self.progress_var = tk.DoubleVar(value=0)
        self.theme_var = tk.StringVar(value="System")
        self.interface_mode_var = tk.StringVar(value="Simple")
        self.font_scale_var = tk.StringVar(value="1.0")
        self.network_backend_var = tk.StringVar(value="auto")
        self.network_endpoint_var = tk.StringVar(value="auto")
        self.network_strategy_var = tk.StringVar(value="auto")
        self.network_page_blocks_var = tk.StringVar(value="0")
        self.network_cdx_workers_var = tk.StringVar(value="10")
        self.network_trust_env_var = tk.BooleanVar(value=True)
        self.network_persistent_var = tk.BooleanVar(value=True)
        self.network_retry_base_var = tk.StringVar(value="5")
        self.network_retry_max_var = tk.StringVar(value="300")
        self.network_failure_limit_var = tk.StringVar(value="8")
        self.auto_backup_var = tk.BooleanVar(value=False)
        self.backup_keep_var = tk.StringVar(value="5")
        self.backup_max_var = tk.StringVar(value="1024")
        self.hitlist_file_var = tk.StringVar()
        self.import_source_var = tk.StringVar()
        self.dashboard_project_var = tk.StringVar(value="No project opened")
        self.dashboard_captures_var = tk.StringVar(value="0")
        self.dashboard_documents_var = tk.StringVar(value="0")
        self.dashboard_matches_var = tk.StringVar(value="0")
        self.dashboard_errors_var = tk.StringVar(value="0")
        self.dashboard_recovery_var = tk.StringVar(value="0")
        self.dashboard_skipped_non_text_var = tk.StringVar(value="0")
        self.dashboard_skipped_url_var = tk.StringVar(value="0")
        self.dashboard_pending_var = tk.StringVar(value="0")
        self.dashboard_waiting_scan_var = tk.StringVar(value="0")
        self.result_page_var = tk.StringVar(value="Page 1")
        self.keyword_set_var = tk.StringVar()
        self.keyword_set_selected_var = tk.BooleanVar(value=True)
        self.media_enabled_var = tk.BooleanVar(value=False)
        self.media_images_var = tk.BooleanVar(value=True)
        self.media_videos_var = tk.BooleanVar(value=True)
        self.media_embedded_var = tk.BooleanVar(value=True)
        self.media_external_var = tk.BooleanVar(value=False)
        self.media_strategy_var = tk.StringVar(value="earliest")
        self.media_max_var = tk.StringVar(value="500")
        self.media_preserve_var = tk.BooleanVar(value=False)
        self.result_scan_var = tk.StringVar()
        self.result_filter_var = tk.StringVar()
        self.result_review_filter_var = tk.StringVar(value="All")
        self.result_min_var = tk.StringVar(value="1")
        self.review_status_var = tk.StringVar(value="Unreviewed")
        self.review_tags_var = tk.StringVar()
        self.fts_query_var = tk.StringVar()
        self.fts_field_var = tk.StringVar(value="all")
        self.fts_domain_var = tk.StringVar()
        self.ai_scan_var = tk.StringVar()
        self.ai_run_var = tk.StringVar()
        self.ai_api_key_var = tk.StringVar()
        self.ai_provider_var = tk.StringVar(value="openai")
        self.ai_model_var = tk.StringVar(value="gpt-5-mini")
        self.ai_candidate_limit_var = tk.StringVar(value="200")
        self.ai_batch_size_var = tk.StringVar(value="8")
        self.ai_min_relevance_var = tk.StringVar(value="50")
        self.ai_excerpt_chars_var = tk.StringVar(value="5000")
        self.research_query_var = tk.StringVar()
        self.research_backend_var = tk.StringVar(value="local-hash")
        self.research_auto_var = tk.BooleanVar(value=True)
        self.research_limit_var = tk.StringVar(value="100")
        self.error_category_var = tk.StringVar(value="All")
        self.forum_profile_var = tk.StringVar(value="auto")
        self.analysis_threads_var = tk.BooleanVar(value=True)
        self.analysis_embeds_var = tk.BooleanVar(value=True)
        self.analysis_external_var = tk.BooleanVar(value=False)
        self.analysis_external_limit_var = tk.StringVar(value="5000")
        self.analysis_duplicate_var = tk.StringVar(value="0.90")
        self.analysis_compare_var = tk.BooleanVar(value=True)
        self.analysis_provenance_var = tk.BooleanVar(value=True)
        self.analysis_merge_source_var = tk.StringVar(value="")

    def create_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        header = ttk.Frame(self, padding=(16, 12, 16, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text=APP_NAME, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="A resilient Wayback research workspace", style="Muted.TLabel").grid(row=1, column=0, sticky="w")
        controls = ttk.Frame(header)
        controls.grid(row=0, column=1, rowspan=2, sticky="e")
        ttk.Label(controls, text="Mode:").grid(row=0, column=0, padx=(0, 4))
        mode_box = ttk.Combobox(controls, textvariable=self.interface_mode_var, values=("Simple", "Advanced"), state="readonly", width=10)
        mode_box.grid(row=0, column=1, padx=(0, 10))
        mode_box.bind("<<ComboboxSelected>>", lambda _e: self.refresh_navigation())
        ttk.Label(controls, text="Theme:").grid(row=0, column=2, padx=(0, 4))
        theme_box = ttk.Combobox(controls, textvariable=self.theme_var, values=("System", "Light", "Dark"), state="readonly", width=9)
        theme_box.grid(row=0, column=3, padx=(0, 10))
        theme_box.bind("<<ComboboxSelected>>", lambda _e: self.apply_interface_theme())
        ttk.Label(controls, text="Preset:").grid(row=0, column=4, padx=(0, 4))
        preset = ttk.Combobox(controls, textvariable=self.preset_var, values=list(PRESETS), state="readonly", width=24)
        preset.grid(row=0, column=5)
        preset.bind("<<ComboboxSelected>>", lambda _event: self.apply_preset())

        project = ttk.Frame(self, padding=(16, 8))
        project.grid(row=1, column=0, sticky="ew")
        project.columnconfigure(1, weight=1)
        ttk.Label(project, text="Project folder:").grid(row=0, column=0, sticky="w")
        ttk.Entry(project, textvariable=self.output_var).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(project, text="Browse…", command=self.choose_output).grid(row=0, column=2)
        ttk.Button(project, text="Open", command=self.open_output).grid(row=0, column=3, padx=(6, 0))
        ttk.Label(project, text="Operation:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        operation = ttk.Combobox(project, textvariable=self.mode_var, values=list(MODE_LABELS), state="readonly")
        operation.grid(row=1, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(8, 0))
        operation.bind("<<ComboboxSelected>>", lambda _event: self.update_operation_help())
        ttk.Label(project, textvariable=self.operation_help_var, wraplength=1040, style="Muted.TLabel").grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))

        workspace = ttk.Frame(self)
        workspace.grid(row=2, column=0, sticky="nsew", padx=(14, 14), pady=6)
        workspace.columnconfigure(1, weight=1)
        workspace.rowconfigure(0, weight=1)
        self.sidebar = ttk.Frame(workspace, style="Sidebar.TFrame", width=184, padding=(6, 10))
        self.sidebar.grid(row=0, column=0, sticky="nsw", padx=(0, 8))
        self.sidebar.grid_propagate(False)
        self.sidebar.columnconfigure(0, weight=1)
        ttk.Label(self.sidebar, text="WORKSPACE", style="Sidebar.TLabel", font=("TkDefaultFont", 9, "bold")).grid(row=0, column=0, sticky="w", padx=10, pady=(0, 8))

        self.notebook = ttk.Notebook(workspace, style="Sidebar.TNotebook")
        self.notebook.grid(row=0, column=1, sticky="nsew")
        self.create_dashboard_tab()
        self.create_targets_tab()
        self.create_keywords_tab()
        self.create_cdx_tab()
        self.create_media_tab()
        self.create_reports_tab()
        self.create_analysis_tab()
        self.create_settings_tab()
        self.create_results_tab()
        self.create_ai_tab()
        self.create_research_tab()
        self.create_history_tab()
        self.create_errors_tab()
        self.create_activity_tab()
        self.notebook.bind("<<NotebookTabChanged>>", lambda _e: self.update_navigation_selection())
        self.refresh_navigation()

        footer = ttk.Frame(self, padding=(14, 8, 14, 12), style="Panel.TFrame")
        footer.grid(row=3, column=0, sticky="ew")
        footer.columnconfigure(2, weight=1)
        self.start_button = ttk.Button(footer, text="Start operation", command=self.start, style="Accent.TButton")
        self.start_button.grid(row=0, column=0)
        self.stop_button = ttk.Button(footer, text="Pause & save", command=self.stop, state="disabled")
        self.stop_button.grid(row=0, column=1, padx=(8, 0))
        ttk.Label(footer, textvariable=self.status_var, style="Status.TLabel", anchor="w").grid(row=0, column=2, sticky="ew", padx=10)
        ttk.Button(footer, text="Save", command=self.save_project).grid(row=0, column=3, padx=3)
        ttk.Button(footer, text="Load…", command=self.load_project).grid(row=0, column=4, padx=3)
        ttk.Button(footer, text="Reports", command=self.open_reports).grid(row=0, column=5, padx=3)
        self.progress = ttk.Progressbar(footer, variable=self.progress_var, maximum=100)
        self.progress.grid(row=1, column=0, columnspan=6, sticky="ew", pady=(8, 0))
        ToolTip(self.stop_button, "Stops after the current request and preserves the exact pending queue for Resume.")
        self.apply_preset()
        self.bind_shortcuts()

    def create_dashboard_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=16)
        tab.columnconfigure((0, 1, 2, 3), weight=1)
        tab.rowconfigure(3, weight=1)
        self.notebook.add(tab, text="Dashboard")
        ttk.Label(tab, text="Project dashboard", style="Section.TLabel").grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(tab, textvariable=self.dashboard_project_var, style="Muted.TLabel").grid(row=1, column=0, columnspan=4, sticky="w", pady=(2, 12))
        cards = (
            ("Indexed captures", self.dashboard_captures_var),
            ("Saved documents", self.dashboard_documents_var),
            ("Ranked matches", self.dashboard_matches_var),
            ("Open errors", self.dashboard_errors_var),
        )
        for column, (title, variable) in enumerate(cards):
            card = ttk.Frame(tab, padding=14, style="Panel.TFrame")
            card.grid(row=2, column=column, sticky="ew", padx=(0 if column == 0 else 6, 6 if column < 3 else 0))
            ttk.Label(card, text=title, style="CardTitle.TLabel").pack(anchor="w")
            ttk.Label(card, textvariable=variable, style="CardValue.TLabel").pack(anchor="w", pady=(5, 0))
        status = ttk.Frame(tab, padding=(4, 10, 4, 0))
        status.grid(row=3, column=0, columnspan=4, sticky="ew")
        ttk.Label(status, text="Pending text:").pack(side="left")
        ttk.Label(status, textvariable=self.dashboard_pending_var).pack(side="left", padx=(4, 16))
        ttk.Label(status, text="Saved unscanned:").pack(side="left")
        ttk.Label(status, textvariable=self.dashboard_waiting_scan_var).pack(side="left", padx=(4, 16))
        ttk.Label(status, text="Skipped non-text:").pack(side="left")
        ttk.Label(status, textvariable=self.dashboard_skipped_non_text_var).pack(side="left", padx=(4, 16))
        ttk.Label(status, text="Skipped URL filter:").pack(side="left")
        ttk.Label(status, textvariable=self.dashboard_skipped_url_var).pack(side="left", padx=(4, 16))
        ttk.Label(status, text="Recovery events:").pack(side="left")
        ttk.Label(status, textvariable=self.dashboard_recovery_var).pack(side="left", padx=(4, 0))
        actions = ttk.LabelFrame(tab, text="Project maintenance", padding=12)
        actions.grid(row=4, column=0, columnspan=2, sticky="nsew", pady=(14, 0), padx=(0, 6))
        ttk.Button(actions, text="Refresh dashboard", command=self.refresh_dashboard).pack(fill="x", pady=3)
        ttk.Button(actions, text="Create backup", command=lambda: self.start(self.build_config(require_keywords=False), "backup")).pack(fill="x", pady=3)
        ttk.Button(actions, text="Restore a backup…", command=self.restore_backup_ui).pack(fill="x", pady=3)
        ttk.Button(actions, text="Check integrity", command=lambda: self.start(self.build_config(require_keywords=False), "integrity")).pack(fill="x", pady=3)
        ttk.Button(actions, text="Repair and rebuild indexes", command=lambda: self.start(self.build_config(require_keywords=False), "repair")).pack(fill="x", pady=3)
        ttk.Button(actions, text="Compact project storage", command=lambda: self.start(self.build_config(require_keywords=False), "compact")).pack(fill="x", pady=3)
        ttk.Button(actions, text="Export diagnostics", command=lambda: self.start(self.build_config(require_keywords=False), "diagnostics")).pack(fill="x", pady=3)
        quick = ttk.LabelFrame(tab, text="Quick start", padding=12)
        quick.grid(row=4, column=2, columnspan=2, sticky="nsew", pady=(14, 0), padx=(6, 0))
        quick_text = (
            "1. Add one or more sites.\n"
            "2. Choose or import keyword sets.\n"
            "3. Select a date range.\n"
            "4. Start the operation.\n\n"
            "Network failures are retried through several independent connection methods. "
            "Progress is written to the project database before any long wait."
        )
        ttk.Label(quick, text=quick_text, justify="left", wraplength=430).pack(anchor="nw")
        ttk.Button(quick, text="Go to Sites and paths", command=lambda: self.show_page("Sites and paths")).pack(anchor="w", pady=(14, 4))
        ttk.Button(quick, text="Go to Network settings", command=lambda: self.show_page("Settings")).pack(anchor="w", pady=4)
        self.dashboard_tab = tab

    def bind_shortcuts(self) -> None:
        modifier = "Command" if sys.platform == "darwin" else "Control"
        self.bind_all(f"<{modifier}-s>", lambda _e: self.save_project())
        self.bind_all(f"<{modifier}-o>", lambda _e: self.load_project())
        self.bind_all(f"<{modifier}-Return>", lambda _e: self.start())
        self.bind_all("<Escape>", lambda _e: self.stop() if self.worker_thread and self.worker_thread.is_alive() else None)
        self.bind_all("<F5>", lambda _e: self.refresh_dashboard())
        self.bind_all(f"<{modifier}-f>", lambda _e: self.show_page("Results and search"))

    def refresh_navigation(self) -> None:
        allowed_simple = {"Dashboard", "Sites and paths", "Keyword sets", "Media", "Reports", "Archive analysis", "Results and search", "AI relevance", "Research intelligence", "Errors", "Activity"}
        for button in self.nav_buttons.values():
            button.destroy()
        self.nav_buttons.clear()
        self.page_names = [self.notebook.tab(tab_id, "text") for tab_id in self.notebook.tabs()]
        row = 1
        for name in self.page_names:
            if self.interface_mode_var.get() == "Simple" and name not in allowed_simple:
                continue
            button = ttk.Button(self.sidebar, text=name, style="Sidebar.TButton", command=lambda value=name: self.show_page(value))
            button.grid(row=row, column=0, sticky="ew", pady=1)
            self.nav_buttons[name] = button
            row += 1
        ttk.Separator(self.sidebar).grid(row=row, column=0, sticky="ew", padx=8, pady=10)
        ttk.Label(self.sidebar, text=f"Version {VERSION}", style="Sidebar.TLabel").grid(row=row + 1, column=0, sticky="sw", padx=10)
        selected = self.notebook.tab(self.notebook.select(), "text") if self.notebook.select() else "Dashboard"
        if self.interface_mode_var.get() == "Simple" and selected not in allowed_simple:
            self.show_page("Dashboard")
        self.update_navigation_selection()

    def update_navigation_selection(self) -> None:
        if not getattr(self, "notebook", None) or not self.notebook.select():
            return
        selected = self.notebook.tab(self.notebook.select(), "text")
        for name, button in self.nav_buttons.items():
            button.configure(style="SidebarActive.TButton" if name == selected else "Sidebar.TButton")
        if selected == "Dashboard":
            self.refresh_dashboard()

    def show_page(self, name: str) -> None:
        for tab_id in self.notebook.tabs():
            if self.notebook.tab(tab_id, "text") == name:
                self.notebook.select(tab_id)
                self.update_navigation_selection()
                return

    def apply_interface_theme(self) -> None:
        try:
            self.resolved_theme, self.colors = apply_theme(self, self.theme_var.get(), float(self.font_scale_var.get() or 1.0))
            apply_text_theme(self, self.colors)
            self.update_navigation_selection()
            if hasattr(self, "results_tree"):
                for status, color in REVIEW_COLORS.items():
                    if self.resolved_theme == "dark":
                        continue
                    self.results_tree.tag_configure(status, background=color)
        except Exception as exc:
            self.status_var.set(f"Could not apply theme: {exc}")

    def show_welcome(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("Welcome to Archive Scout")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("620x420")
        frame = ttk.Frame(dialog, padding=24)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Welcome to Archive Scout", style="Title.TLabel").pack(anchor="w")
        ttk.Label(frame, text="Built for long-running public web-archive research", style="Muted.TLabel").pack(anchor="w", pady=(0, 18))
        ttk.Label(frame, text="The Simple workspace keeps the common workflow visible. Advanced mode exposes CDX, media, analysis, network, review, repair, and migration controls. Archive Scout saves its indexing queue continuously and can switch between multiple HTTP connection methods when one stack cannot reach the Internet Archive.", wraplength=550, justify="left").pack(anchor="w")
        ttk.Button(frame, text="Open the dashboard", style="Accent.TButton", command=dialog.destroy).pack(anchor="e", pady=(24, 0))

    def dashboard_refresh_loop(self) -> None:
        try:
            selected = self.notebook.tab(self.notebook.select(), "text") if self.notebook.select() else ""
            active = bool(self.worker_thread and self.worker_thread.is_alive())
            # Exact COUNT(*) dashboard queries are deliberately suspended while
            # an operation owns the project database. Large projects can contain
            # millions of capture/match/error rows; recounting them every second
            # steals page cache and I/O from acquisition/scanning without adding
            # useful precision. Live operation counters arrive through ProgressEvent.
            if not active and selected == "Dashboard":
                self.refresh_dashboard()
                interval = 5000
            elif active:
                interval = 1000
            else:
                interval = 15000
            self.dashboard_refresh_job = self.after(interval, self.dashboard_refresh_loop)
        except tk.TclError:
            self.dashboard_refresh_job = None

    def update_dashboard_from_progress(self, event: ProgressEvent) -> None:
        detail = dict(event.detail or {})
        if "pending" in detail:
            self.dashboard_pending_var.set(f"{int(detail['pending'] or 0):,}")
        if "downloaded_unscanned" in detail:
            self.dashboard_waiting_scan_var.set(f"{int(detail['downloaded_unscanned'] or 0):,}")
        elif "scan_backlog" in detail:
            self.dashboard_waiting_scan_var.set(f"{int(detail['scan_backlog'] or 0):,}")

    def refresh_dashboard(self) -> None:
        root = Path(self.output_var.get()).expanduser()
        self.dashboard_project_var.set(str(root))
        try:
            counts = read_dashboard_counts(root / "archive_scout.sqlite3")
            self.dashboard_captures_var.set(f"{counts['captures']:,}")
            self.dashboard_documents_var.set(f"{counts['documents']:,}")
            self.dashboard_matches_var.set(f"{counts['matches']:,}")
            self.dashboard_errors_var.set(f"{counts['errors']:,}")
            self.dashboard_recovery_var.set(f"{counts.get('recovery_events', 0):,}")
            self.dashboard_skipped_non_text_var.set(f"{counts.get('skipped_non_text', 0):,}")
            self.dashboard_skipped_url_var.set(f"{counts.get('skipped_url_filter', 0):,}")
            self.dashboard_pending_var.set(f"{counts.get('pending', 0):,}")
            self.dashboard_waiting_scan_var.set(f"{counts.get('downloaded_unscanned', 0):,}")
        except Exception as exc:
            if not (self.worker_thread and self.worker_thread.is_alive()):
                self.dashboard_project_var.set(f"{root} — {exc}")

    def restore_backup_ui(self) -> None:
        root = Path(self.output_var.get()).expanduser()
        backups = list_project_backups(root)
        selected = filedialog.askopenfilename(
            title="Restore Archive Scout database backup",
            initialdir=str(root / "backups"),
            filetypes=[("SQLite database", "*.sqlite3"), ("All files", "*")],
        )
        if not selected:
            return
        if not messagebox.askyesno(APP_NAME, "Restore this database backup? A safety copy of the current database will be created first."):
            return
        try:
            safety = restore_project_backup(root, Path(selected))
            messagebox.showinfo(APP_NAME, f"Backup restored. Safety copy:\n{safety}")
            self.refresh_dashboard()
            self.refresh_history()
            self.refresh_results(reset_page=True)
            self.refresh_errors()
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not restore backup:\n{exc}")

    def create_targets_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        self.notebook.add(tab, text="Sites and paths")
        ttk.Label(tab, text="One Wayback target per line. Examples: example.com/* or forum.example.com/path/*").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.targets_text = tk.Text(tab, wrap="none", undo=True, font="TkFixedFont")
        self.targets_text.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(tab, orient="vertical", command=self.targets_text.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.targets_text.configure(yscrollcommand=scroll.set)
        controls = ttk.Frame(tab)
        controls.grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Button(controls, text="Configure current target…", command=self.configure_target_settings).grid(row=0, column=0)
        ttk.Label(controls, text="Per-target settings override the global date, CDX, worker, and delay values.", style="Muted.TLabel").grid(row=0, column=1, padx=(10, 0))

    def configure_target_settings(self) -> None:
        try:
            index = self.targets_text.index("insert")
            line = self.targets_text.get(f"{index.split('.')[0]}.0", f"{index.split('.')[0]}.end").strip()
        except Exception:
            line = ""
        if not line:
            messagebox.showinfo(APP_NAME, "Place the cursor on a target line first.")
            return
        existing = dict(self.target_settings.get(line) or {})
        dialog = tk.Toplevel(self)
        dialog.title(f"Target settings — {line}")
        dialog.transient(self)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        fields = {
            "from_date": tk.StringVar(value=str(existing.get("from_date", ""))),
            "to_date": tk.StringVar(value=str(existing.get("to_date", ""))),
            "cdx_match_type": tk.StringVar(value=str(existing.get("cdx_match_type", ""))),
            "page_size": tk.StringVar(value=str(existing.get("page_size", ""))),
            "workers": tk.StringVar(value=str(existing.get("workers", ""))),
            "cdx_delay": tk.StringVar(value=str(existing.get("cdx_delay", ""))),
            "download_delay": tk.StringVar(value=str(existing.get("download_delay", ""))),
        }
        labels = {
            "from_date": "Start date", "to_date": "End date", "cdx_match_type": "matchType",
            "page_size": "CDX result limit", "workers": "Download workers",
            "cdx_delay": "CDX delay", "download_delay": "Download delay",
        }
        for row, key in enumerate(fields):
            ttk.Label(frame, text=labels[key] + ":").grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(frame, textvariable=fields[key], width=28).grid(row=row, column=1, sticky="ew", padx=(10, 0), pady=4)
        def save() -> None:
            values = {key: variable.get().strip() for key, variable in fields.items() if variable.get().strip()}
            for key in ("page_size", "workers"):
                if key in values:
                    values[key] = int(values[key])
            for key in ("cdx_delay", "download_delay"):
                if key in values:
                    values[key] = float(values[key])
            self.target_settings[line] = values
            dialog.destroy()
        buttons = ttk.Frame(frame)
        buttons.grid(row=len(fields), column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Clear override", command=lambda: (self.target_settings.pop(line, None), dialog.destroy())).pack(side="left", padx=4)
        ttk.Button(buttons, text="Save", command=save, style="Accent.TButton").pack(side="left", padx=4)

    def create_keywords_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)
        self.notebook.add(tab, text="Keyword sets")
        controls = ttk.Frame(tab)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        controls.columnconfigure(1, weight=1)
        ttk.Label(controls, text="Set:").grid(row=0, column=0)
        self.keyword_set_box = ttk.Combobox(controls, textvariable=self.keyword_set_var, state="readonly")
        self.keyword_set_box.grid(row=0, column=1, sticky="ew", padx=6)
        self.keyword_set_box.bind("<<ComboboxSelected>>", self.switch_keyword_set)
        ttk.Checkbutton(controls, text="Use in next scan", variable=self.keyword_set_selected_var, command=self.save_current_keyword_set).grid(row=0, column=2, padx=6)
        ttk.Button(controls, text="New", command=self.new_keyword_set).grid(row=0, column=3, padx=2)
        ttk.Button(controls, text="Duplicate", command=self.duplicate_keyword_set).grid(row=0, column=4, padx=2)
        ttk.Button(controls, text="Delete", command=self.delete_keyword_set).grid(row=0, column=5, padx=2)
        ttk.Button(controls, text="Import…", command=self.import_keyword_set).grid(row=0, column=6, padx=2)
        ttk.Button(controls, text="Export…", command=self.export_keyword_set).grid(row=0, column=7, padx=2)
        ttk.Label(
            tab,
            text="One rule per line. Prefixes: required:, exclude:, exact:, regex:, high:. Options: | weight=3 | whole | case | label=Name",
            wraplength=1000,
        ).grid(row=1, column=0, sticky="w", pady=(0, 6))
        self.keywords_text = tk.Text(tab, wrap="none", undo=True, font="TkFixedFont")
        self.keywords_text.grid(row=2, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(tab, orient="vertical", command=self.keywords_text.yview)
        scroll.grid(row=2, column=1, sticky="ns")
        self.keywords_text.configure(yscrollcommand=scroll.set)

    def create_cdx_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(6, weight=1)
        self.notebook.add(tab, text="CDX options")
        ttk.Label(tab, text="Start date:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(tab, textvariable=self.from_date_var, width=22).grid(row=0, column=1, sticky="w", padx=(10, 0), pady=4)
        ttk.Label(tab, text="End date:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(tab, textvariable=self.to_date_var, width=22).grid(row=1, column=1, sticky="w", padx=(10, 0), pady=4)
        ttk.Label(tab, text="Accepted: YYYY, YYYYMM, YYYYMMDD, YYYYMMDDhhmmss, MM/DD/YYYY, or YYYY-MM-DD.").grid(row=2, column=1, sticky="w", padx=(10, 0))
        ttk.Label(tab, text="matchType:").grid(row=3, column=0, sticky="w", pady=(10, 4))
        ttk.Combobox(tab, textvariable=self.cdx_match_type_var, values=("Automatic", "exact", "prefix", "host", "domain"), state="readonly", width=19).grid(row=3, column=1, sticky="w", padx=(10, 0), pady=(10, 4))
        collapse = ttk.Frame(tab)
        collapse.grid(row=4, column=1, sticky="w", padx=(10, 0), pady=4)
        ttk.Checkbutton(collapse, text="collapse=urlkey", variable=self.collapse_urlkey_var).grid(row=0, column=0)
        ttk.Checkbutton(collapse, text="collapse=digest", variable=self.collapse_digest_var).grid(row=0, column=1, padx=(18, 0))
        ttk.Label(tab, text="Resume batch size (CDX rows):").grid(row=5, column=0, sticky="w", pady=4)
        ttk.Entry(tab, textvariable=self.page_size_var, width=22).grid(row=5, column=1, sticky="w", padx=(10, 0), pady=4)
        options = ttk.Frame(tab)
        options.grid(row=6, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
        options.columnconfigure(0, weight=1)
        options.columnconfigure(1, weight=1)
        options.rowconfigure(1, weight=1)
        ttk.Label(options, text="Filters, one per line").grid(row=0, column=0, sticky="w")
        ttk.Label(options, text="Additional key=value parameters").grid(row=0, column=1, sticky="w", padx=(12, 0))
        self.cdx_filters_text = tk.Text(options, height=8, wrap="none", font="TkFixedFont")
        self.cdx_filters_text.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        self.cdx_extra_text = tk.Text(options, height=8, wrap="none", font="TkFixedFont")
        self.cdx_extra_text.grid(row=1, column=1, sticky="nsew", padx=(12, 0), pady=(4, 0))
        ttk.Button(tab, text="Preview CDX request", command=self.preview_cdx).grid(row=8, column=0, columnspan=2, sticky="w", pady=(10, 0))

    def create_media_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(3, weight=1)
        self.notebook.add(tab, text="Media")
        options = ttk.Frame(tab)
        options.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Checkbutton(options, text="Also download media during text and download-only runs", variable=self.media_enabled_var).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(options, text="Images", variable=self.media_images_var).grid(row=0, column=1, padx=(16, 0))
        ttk.Checkbutton(options, text="Videos", variable=self.media_videos_var).grid(row=0, column=2, padx=(8, 0))
        ttk.Checkbutton(options, text="Discover media linked inside saved pages", variable=self.media_embedded_var).grid(row=0, column=3, padx=(16, 0))
        ttk.Checkbutton(options, text="Allow external hosts", variable=self.media_external_var).grid(row=0, column=4, padx=(8, 0))
        ttk.Label(
            tab,
            text="The ‘Index, download, scan, then download external embedded media’ operation waits until every saved text page has been scanned before it looks up and downloads external image/video links found in those pages.",
            wraplength=1080,
            style="Muted.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        labels = ttk.Frame(tab)
        labels.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 4))
        labels.columnconfigure(0, weight=1)
        labels.columnconfigure(1, weight=1)
        ttk.Label(labels, text="Media sites/paths (blank uses Sites and paths)").grid(row=0, column=0, sticky="w")
        ttk.Label(labels, text="Include extensions, one per line").grid(row=0, column=1, sticky="w", padx=(12, 0))
        editors = ttk.Frame(tab)
        editors.grid(row=3, column=0, columnspan=2, sticky="nsew")
        editors.columnconfigure(0, weight=1)
        editors.columnconfigure(1, weight=1)
        editors.columnconfigure(2, weight=1)
        editors.rowconfigure(0, weight=1)
        self.media_targets_text = tk.Text(editors, wrap="none", font="TkFixedFont")
        self.media_targets_text.grid(row=0, column=0, sticky="nsew")
        self.media_include_text = tk.Text(editors, wrap="none", font="TkFixedFont")
        self.media_include_text.grid(row=0, column=1, sticky="nsew", padx=(12, 0))
        right = ttk.Frame(editors)
        right.grid(row=0, column=2, sticky="nsew", padx=(12, 0))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        ttk.Label(right, text="Exclude extensions, one per line").grid(row=0, column=0, sticky="w")
        self.media_exclude_text = tk.Text(right, wrap="none", font="TkFixedFont")
        self.media_exclude_text.grid(row=1, column=0, sticky="nsew")
        settings = ttk.Frame(tab)
        settings.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Label(settings, text="Snapshot:").grid(row=0, column=0)
        ttk.Combobox(settings, textvariable=self.media_strategy_var, values=("earliest", "latest", "all"), state="readonly", width=10).grid(row=0, column=1, padx=(5, 15))
        ttk.Label(settings, text="Maximum media size (MB):").grid(row=0, column=2)
        ttk.Entry(settings, textvariable=self.media_max_var, width=10).grid(row=0, column=3, padx=(5, 15))
        ttk.Label(settings, text="Media layout: media/images and media/videos (flat)").grid(row=0, column=4, sticky="w")
        ttk.Label(
            tab,
            text="For media added after a text or download-only run, Snapshot=earliest uses collapse=urlkey by default (one archived timestamp per media URL). The primary text CDX query is never changed; latest/all explicitly override that media-only collapse.",
            wraplength=1080,
            style="Muted.TLabel",
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))


    def create_reports_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(3, weight=1)
        self.notebook.add(tab, text="Reports")
        ttk.Label(tab, text="Report contents", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            tab,
            text=(
                "Customize generated text-scan, index, media and archive-analysis reports. "
                "These settings never change which pages are acquired or matched, and never erase saved scan evidence. "
                "Use Regenerate reports in Scan history to apply them without downloading again. "
                "Hitlist, AI and manual export formats are separate operation outputs."
            ),
            wraplength=1080,
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="ew", pady=(4, 10))

        controls = ttk.Frame(tab)
        controls.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        for column, (label, variable) in enumerate((
            ("Minimum score", self.minimum_score_var),
            ("Match limit", self.report_max_var),
            ("Snippets / match", self.report_snippets_var),
            ("Snippet characters", self.report_chars_var),
            ("Links / match", self.report_links_var),
        )):
            ttk.Label(controls, text=label).grid(row=0, column=column, sticky="w", padx=(0, 10))
            ttk.Entry(controls, textvariable=variable, width=12).grid(row=1, column=column, sticky="w", padx=(0, 10))
        ttk.Label(controls, text="Sort matches").grid(row=0, column=5, sticky="w")
        ttk.Combobox(controls, textvariable=self.report_sort_var, values=("score", "oldest", "newest", "url"), state="readonly", width=12).grid(row=1, column=5, sticky="w")
        ttk.Label(controls, text="Limits: 0 = all available. Match sorting/limits apply consistently to all match-derived reports.", style="Muted.TLabel").grid(row=2, column=0, columnspan=6, sticky="w", pady=4)
        ttk.Checkbutton(controls, text="Retain full scan details for later reports, review and AI (recommended)", variable=self.report_retain_var).grid(row=3, column=0, columnspan=6, sticky="w")
        ttk.Label(controls, text="Unchecked: future scans omit unused enrichment; enabling it later requires a local rescan. Download-only is lean in either mode.", wraplength=1000, style="Muted.TLabel").grid(row=4, column=0, columnspan=6, sticky="w")
        presets = ttk.Frame(controls)
        presets.grid(row=5, column=0, columnspan=6, sticky="w", pady=(6, 0))
        for label, preset in (("All files and fields", "all"), ("Matched URLs only", "urls"), ("No generated reports", "none")):
            ttk.Button(presets, text=label, command=lambda value=preset: self.set_report_preset(value)).pack(side="left", padx=(0, 8))

        book = ttk.Notebook(tab)
        book.grid(row=3, column=0, sticky="nsew")
        groups = (
            ("Text and index", ("matches_ranked", "matched_urls", "wayback_urls", "interesting_links", "keyword_counts", "all_indexed_urls", "summary")),
            ("Errors", ("errors", "site_issues")),
            ("Media", ("media_indexed", "media_downloaded", "media_wayback_urls", "media_errors", "media_summary")),
            ("Archive analysis", ("analysis_summary", "forum_threads", "extractions", "legacy_assets", "duplicate_groups", "provenance", "snapshot_diffs", "first_appearances")),
        )
        output_labels = {
            "matches_ranked": "Ranked matches (matches_ranked.txt)",
            "matched_urls": "Matched URLs (matched_urls.txt)",
            "wayback_urls": "Wayback URLs (wayback_urls.txt)",
            "interesting_links": "Interesting Links (interesting_links.txt)",
            "keyword_counts": "Keyword counts (keyword_counts.txt)",
            "all_indexed_urls": "All indexed URLs (all_indexed_urls.txt)",
            "errors": "Errors (errors.txt)",
            "site_issues": "Site issues (site_issues.txt)",
            "summary": "Text/index summary (summary.txt)",
            "media_indexed": "Indexed media (media_indexed.txt)",
            "media_downloaded": "Downloaded media (media_downloaded.txt)",
            "media_wayback_urls": "Media Wayback URLs (media_wayback_urls.txt)",
            "media_errors": "Media errors (media_errors.txt)",
            "media_summary": "Media summary (media_summary.txt)",
            "analysis_summary": "Analysis summary (analysis_summary.txt)",
            "forum_threads": "Forum threads (forum_threads.tsv)",
            "extractions": "Extractions (extractions.tsv)",
            "legacy_assets": "Legacy assets (legacy_assets.tsv)",
            "duplicate_groups": "Duplicate groups (duplicate_groups.tsv)",
            "provenance": "Provenance (provenance.tsv)",
            "snapshot_diffs": "Snapshot diffs (snapshot_diffs.tsv)",
            "first_appearances": "First appearances (first_appearances.tsv)",
        }

        for group_label, names in groups:
            container = ttk.Frame(book)
            container.columnconfigure(0, weight=1)
            container.rowconfigure(0, weight=1)
            canvas = tk.Canvas(container, highlightthickness=0)
            scroll_y = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
            scroll_x = ttk.Scrollbar(container, orient="horizontal", command=canvas.xview)
            canvas.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
            canvas.grid(row=0, column=0, sticky="nsew")
            scroll_y.grid(row=0, column=1, sticky="ns")
            scroll_x.grid(row=1, column=0, sticky="ew")
            page = ttk.Frame(canvas, padding=8)
            window = canvas.create_window((0, 0), window=page, anchor="nw")
            page.bind("<Configure>", lambda _event, c=canvas: c.configure(scrollregion=c.bbox("all")))
            canvas.bind("<Configure>", lambda event, c=canvas, w=window, p=page: c.itemconfigure(w, width=max(event.width, p.winfo_reqwidth())))
            page.columnconfigure(0, weight=1)
            page.columnconfigure(1, weight=1)
            book.add(container, text=group_label)
            for index, name in enumerate(names):
                row, column = divmod(index, 2)
                box = ttk.LabelFrame(page, text=output_labels[name], padding=7)
                box.grid(row=row, column=column, sticky="nsew", padx=(0, 6) if column == 0 else (6, 0), pady=5)
                box.columnconfigure((0, 1, 2, 3), weight=1)
                ttk.Checkbutton(box, text="Generate this file", variable=self.report_output_vars[name]).grid(
                    row=0, column=0, columnspan=2, sticky="w"
                )
                ttk.Button(
                    box, text="All fields", width=10,
                    command=lambda value=name: self.set_report_fields(value, True),
                ).grid(row=0, column=2, sticky="e", padx=2)
                ttk.Button(
                    box, text="No fields", width=10,
                    command=lambda value=name: self.set_report_fields(value, False),
                ).grid(row=0, column=3, sticky="e", padx=2)
                for field_index, field_name in enumerate(REPORT_FIELD_NAMES[name]):
                    field_row = 1 + field_index // 4
                    field_column = field_index % 4
                    ttk.Checkbutton(
                        box,
                        text=field_name.replace("_", " ").title(),
                        variable=self.report_field_vars[name][field_name],
                    ).grid(row=field_row, column=field_column, sticky="w", padx=(0, 8), pady=2)

    def set_report_fields(self, output_name: str, enabled: bool) -> None:
        for variable in self.report_field_vars.get(output_name, {}).values():
            variable.set(bool(enabled))

    def set_report_preset(self, preset: str) -> None:
        for name, variable in self.report_output_vars.items():
            variable.set(preset == "all" or (preset == "urls" and name == "matched_urls"))
            if preset in {"all", "urls"}:
                self.set_report_fields(name, True)

    def create_analysis_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(3, weight=1)
        self.notebook.add(tab, text="Archive analysis")

        top = ttk.Frame(tab)
        top.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Checkbutton(top, text="Reconstruct forum threads and posts", variable=self.analysis_threads_var).grid(row=0, column=0, sticky="w")
        ttk.Label(top, text="Forum profile:").grid(row=0, column=1, padx=(18, 4))
        ttk.Combobox(
            top,
            textvariable=self.forum_profile_var,
            values=("auto", "generic", "vbulletin", "phpbb", "invision", "futaba", "2channel"),
            state="readonly",
            width=12,
        ).grid(row=0, column=2)
        ttk.Checkbutton(top, text="Recover legacy embeds and players", variable=self.analysis_embeds_var).grid(row=0, column=3, padx=(18, 0))
        ttk.Checkbutton(top, text="Compare snapshots", variable=self.analysis_compare_var).grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Checkbutton(top, text="Build source-to-mirror provenance", variable=self.analysis_provenance_var).grid(row=1, column=1, columnspan=2, sticky="w", padx=(18, 0), pady=(6, 0))
        ttk.Label(top, text="Near-duplicate threshold:").grid(row=1, column=3, sticky="e", padx=(18, 4), pady=(6, 0))
        ttk.Entry(top, textvariable=self.analysis_duplicate_var, width=8).grid(row=1, column=4, sticky="w", pady=(6, 0))

        external = ttk.Frame(tab)
        external.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 6))
        external.columnconfigure(4, weight=1)
        ttk.Checkbutton(external, text="Search Wayback for discovered external assets", variable=self.analysis_external_var).grid(row=0, column=0, sticky="w")
        ttk.Label(external, text="Maximum lookups:").grid(row=0, column=1, padx=(14, 4))
        ttk.Entry(external, textvariable=self.analysis_external_limit_var, width=9).grid(row=0, column=2)
        ttk.Label(external, text="Only explicitly allowed domains are searched.").grid(row=0, column=3, padx=(14, 0), sticky="w")

        labels = ttk.Frame(tab)
        labels.grid(row=2, column=0, columnspan=2, sticky="ew")
        labels.columnconfigure(0, weight=1)
        labels.columnconfigure(1, weight=1)
        ttk.Label(labels, text="Custom extractors: name :: regex or name :: field :: regex").grid(row=0, column=0, sticky="w")
        ttk.Label(labels, text="Allowed external domains, one per line").grid(row=0, column=1, sticky="w", padx=(12, 0))

        editors = ttk.Frame(tab)
        editors.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(4, 8))
        editors.columnconfigure(0, weight=1)
        editors.columnconfigure(1, weight=1)
        editors.rowconfigure(0, weight=1)
        self.analysis_extractors_text = tk.Text(editors, wrap="none", font="TkFixedFont")
        self.analysis_extractors_text.grid(row=0, column=0, sticky="nsew")
        self.analysis_domains_text = tk.Text(editors, wrap="none", font="TkFixedFont")
        self.analysis_domains_text.grid(row=0, column=1, sticky="nsew", padx=(12, 0))

        merge = ttk.LabelFrame(tab, text="Project and shared-review merge", padding=8)
        merge.grid(row=4, column=0, columnspan=2, sticky="ew")
        merge.columnconfigure(1, weight=1)
        ttk.Label(merge, text="Source project folder:").grid(row=0, column=0, sticky="w")
        ttk.Entry(merge, textvariable=self.analysis_merge_source_var).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(merge, text="Browse…", command=self.choose_merge_source).grid(row=0, column=2)
        ttk.Label(
            merge,
            text="Choose the ‘Merge another Archive Scout project’ operation to copy captures, documents, media, scan history, reviews, notes, tags, and extraction results into this project.",
            wraplength=920,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

    def choose_merge_source(self) -> None:
        selected = filedialog.askdirectory(title="Choose Archive Scout project to merge")
        if selected:
            self.analysis_merge_source_var.set(selected)

    def create_settings_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=14)
        tab.columnconfigure(1, weight=1)
        tab.columnconfigure(3, weight=1)
        self.notebook.add(tab, text="Settings")

        ttk.Label(tab, text="Performance", style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        performance = [
            ("Download workers (10 = fast default)", self.workers_var),
            ("Scanner workers (0 = automatic)", self.scan_workers_var),
            ("Maximum text-page size (MB)", self.max_file_var),
            ("CDX request spacing (seconds)", self.cdx_delay_var),
            ("Download request spacing (0.125 = 8/sec)", self.download_delay_var),
            ("429/503 initial shared pause (seconds)", self.rate_limit_base_var),
            ("429/503 maximum shared pause (seconds)", self.rate_limit_max_var),
        ]
        for row, (label, variable) in enumerate(performance, start=1):
            ttk.Label(tab, text=label + ":").grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(tab, textvariable=variable, width=18).grid(row=row, column=1, sticky="w", padx=(10, 24), pady=4)
        scope_row = len(performance) + 1
        ttk.Label(tab, text="Download scope:").grid(row=scope_row, column=0, sticky="w", pady=4)
        ttk.Combobox(tab, textvariable=self.scope_var, values=list(SCOPE_LABELS), state="readonly", width=38).grid(row=scope_row, column=1, sticky="w", padx=(10, 24), pady=4)

        ttk.Label(tab, text="Network recovery", style="Section.TLabel").grid(row=0, column=2, columnspan=2, sticky="w", pady=(0, 6))
        network_rows = [
            ("Connection backend", self.network_backend_var, ("auto", "httpx", "urllib3", "curl")),
            ("CDX endpoint", self.network_endpoint_var, ("auto", "cdx", "timemap")),
            ("Index strategy (auto = fast parallel)", self.network_strategy_var, ("auto", "paged", "resume")),
        ]
        for row, (label, variable, values) in enumerate(network_rows, start=1):
            ttk.Label(tab, text=label + ":").grid(row=row, column=2, sticky="w", pady=4)
            ttk.Combobox(tab, textvariable=variable, values=values, state="readonly", width=18).grid(row=row, column=3, sticky="w", padx=(10, 0), pady=4)
        numeric = [
            ("Parallel CDX requests", self.network_cdx_workers_var),
            ("Page blocks (custom paged mode only)", self.network_page_blocks_var),
            ("Retry base (seconds)", self.network_retry_base_var),
            ("Retry ceiling (seconds)", self.network_retry_max_var),
            ("Failures before graceful pause", self.network_failure_limit_var),
        ]
        for offset, (label, variable) in enumerate(numeric, start=4):
            ttk.Label(tab, text=label + ":").grid(row=offset, column=2, sticky="w", pady=4)
            ttk.Entry(tab, textvariable=variable, width=18).grid(row=offset, column=3, sticky="w", padx=(10, 0), pady=4)
        ttk.Checkbutton(tab, text="Honor system proxy and certificate environment", variable=self.network_trust_env_var).grid(row=9, column=2, columnspan=2, sticky="w", pady=4)
        ttk.Checkbutton(tab, text="Keep retrying recoverable windows during this run", variable=self.network_persistent_var).grid(row=10, column=2, columnspan=2, sticky="w", pady=4)
        ttk.Label(
            tab,
            text="Auto uses persistent pooled connections and independent fallback stacks. Broad CDX targets use yearly page queues, bounded parallel page retrieval, text-first responses, and exact failed-page resume. Repeated failures are saved and gracefully paused instead of producing a traceback or a tight retry loop.",
            wraplength=520,
            style="Muted.TLabel",
        ).grid(row=11, column=2, columnspan=2, sticky="w", pady=(8, 0))

        separator = ttk.Separator(tab, orient="horizontal")
        separator.grid(row=12, column=0, columnspan=4, sticky="ew", pady=14)
        ttk.Label(tab, text="Interface and project safety", style="Section.TLabel").grid(row=13, column=0, columnspan=4, sticky="w")
        ttk.Label(tab, text="Font scale:").grid(row=14, column=0, sticky="w", pady=4)
        font_entry = ttk.Entry(tab, textvariable=self.font_scale_var, width=18)
        font_entry.grid(row=14, column=1, sticky="w", padx=(10, 24), pady=4)
        ttk.Button(tab, text="Apply scale", command=self.apply_interface_theme).grid(row=14, column=1, sticky="e", padx=(0, 24))
        ttk.Checkbutton(tab, text="Create automatic safety backups", variable=self.auto_backup_var).grid(row=15, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Label(tab, text="Backups to keep:").grid(row=16, column=0, sticky="w", pady=4)
        ttk.Entry(tab, textvariable=self.backup_keep_var, width=18).grid(row=16, column=1, sticky="w", padx=(10, 24), pady=4)
        ttk.Label(tab, text="Backup disk budget (MB):").grid(row=17, column=0, sticky="w", pady=4)
        ttk.Entry(tab, textvariable=self.backup_max_var, width=18).grid(row=17, column=1, sticky="w", padx=(10, 24), pady=4)
        ttk.Label(tab, text="Import existing archive folder:").grid(row=14, column=2, sticky="w", pady=4)
        ttk.Entry(tab, textvariable=self.import_source_var).grid(row=14, column=3, sticky="ew", padx=(10, 0), pady=4)
        ttk.Button(tab, text="Browse…", command=self.choose_import_source).grid(row=14, column=3, sticky="w", padx=(10, 0))
        ttk.Label(tab, text="Choose ‘Import an existing archive folder’ from Operation after selecting a source.", style="Muted.TLabel", wraplength=460).grid(row=15, column=2, columnspan=2, sticky="w")

    def choose_hitlist_file(self) -> None:
        selected = filedialog.askopenfilename(title="Choose hitlist", filetypes=[("Text files", "*.txt"), ("All files", "*")])
        if selected:
            self.hitlist_file_var.set(selected)

    def choose_import_source(self) -> None:
        selected = filedialog.askdirectory(title="Choose an existing archive folder")
        if selected:
            self.import_source_var.set(selected)

    def create_results_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=8)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)
        self.notebook.add(tab, text="Results and search")
        hitlist = ttk.LabelFrame(tab, text="Search with Hitlist", padding=8)
        hitlist.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        hitlist.columnconfigure(0, weight=1)
        self.hitlist_text = tk.Text(hitlist, height=4, wrap="word", font="TkFixedFont")
        self.hitlist_text.grid(row=0, column=0, columnspan=3, sticky="ew")
        ttk.Entry(hitlist, textvariable=self.hitlist_file_var).grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(hitlist, text="Hitlist file…", command=self.choose_hitlist_file).grid(row=1, column=1, padx=6, pady=(6, 0))
        ttk.Button(hitlist, text="Search now", command=lambda: self.start(self.build_config(require_keywords=False), "hitlist")).grid(row=1, column=2, pady=(6, 0))
        filters = ttk.Frame(tab)
        filters.grid(row=0, column=0, sticky="ew")
        filters.columnconfigure(1, weight=1)
        ttk.Label(filters, text="Scan:").grid(row=0, column=0)
        self.result_scan_box = ttk.Combobox(filters, textvariable=self.result_scan_var, state="readonly", width=38)
        self.result_scan_box.grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Label(filters, text="Min score:").grid(row=0, column=2)
        ttk.Entry(filters, textvariable=self.result_min_var, width=7).grid(row=0, column=3, padx=5)
        ttk.Label(filters, text="Review:").grid(row=0, column=4)
        ttk.Combobox(filters, textvariable=self.result_review_filter_var, values=("All", *REVIEW_LABELS), state="readonly", width=18).grid(row=0, column=5, padx=5)
        ttk.Label(filters, text="Filter:").grid(row=0, column=6)
        ttk.Entry(filters, textvariable=self.result_filter_var, width=20).grid(row=0, column=7, padx=5)
        ttk.Button(filters, text="Refresh", command=lambda: self.refresh_results(reset_page=True)).grid(row=0, column=8)
        ttk.Button(filters, text="‹", width=3, command=self.previous_result_page).grid(row=0, column=9, padx=(8, 2))
        ttk.Label(filters, textvariable=self.result_page_var, width=17, anchor="center").grid(row=0, column=10)
        ttk.Button(filters, text="›", width=3, command=self.next_result_page).grid(row=0, column=11, padx=(2, 0))
        search = ttk.Frame(tab)
        search.grid(row=1, column=0, sticky="ew", pady=(6, 4))
        search.columnconfigure(1, weight=1)
        ttk.Label(search, text="Instant full-text search:").grid(row=0, column=0)
        ttk.Entry(search, textvariable=self.fts_query_var).grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Combobox(search, textvariable=self.fts_field_var, values=("all", "title", "body", "url"), state="readonly", width=8).grid(row=0, column=2, padx=5)
        ttk.Label(search, text="Domain:").grid(row=0, column=3)
        ttk.Entry(search, textvariable=self.fts_domain_var, width=18).grid(row=0, column=4, padx=5)
        ttk.Button(search, text="Search", command=self.run_fts_search).grid(row=0, column=5)
        pane = ttk.Panedwindow(tab, orient="vertical")
        pane.grid(row=2, column=0, sticky="nsew")
        top = ttk.Frame(pane)
        top.columnconfigure(0, weight=1)
        top.rowconfigure(0, weight=1)
        columns = ("score", "review", "timestamp", "title", "url", "hits")
        self.results_tree = ttk.Treeview(top, columns=columns, show="headings", selectmode="browse")
        widths = {"score": 70, "review": 120, "timestamp": 125, "title": 260, "url": 420, "hits": 100}
        for column in columns:
            self.results_tree.heading(column, text=column.title(), command=lambda name=column: self.sort_result_tree(name))
            self.results_tree.column(column, width=widths[column], anchor="w")
        self.results_tree.grid(row=0, column=0, sticky="nsew")
        self.results_tree.bind("<<TreeviewSelect>>", self.load_selected_result)
        scroll = ttk.Scrollbar(top, orient="vertical", command=self.results_tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.results_tree.configure(yscrollcommand=scroll.set)
        pane.add(top, weight=3)
        bottom = ttk.Frame(pane, padding=(0, 6, 0, 0))
        bottom.columnconfigure(1, weight=1)
        bottom.rowconfigure(1, weight=1)
        bottom.rowconfigure(2, weight=1)
        ttk.Label(bottom, text="Status:").grid(row=0, column=0, sticky="w")
        ttk.Combobox(bottom, textvariable=self.review_status_var, values=list(REVIEW_LABELS), state="readonly", width=20).grid(row=0, column=1, sticky="w", padx=5)
        ttk.Label(bottom, text="Tags:").grid(row=0, column=2)
        ttk.Entry(bottom, textvariable=self.review_tags_var, width=30).grid(row=0, column=3, padx=5)
        ttk.Button(bottom, text="Save review", command=self.save_selected_review).grid(row=0, column=4, padx=4)
        ttk.Button(bottom, text="Next unreviewed", command=self.select_next_unreviewed).grid(row=0, column=5, padx=2)
        ttk.Button(bottom, text="Open local", command=self.open_selected_local).grid(row=0, column=6, padx=2)
        ttk.Button(bottom, text="Open Wayback", command=self.open_selected_wayback).grid(row=0, column=7, padx=2)
        ttk.Button(bottom, text="Copy URL", command=self.copy_selected_url).grid(row=0, column=8, padx=2)
        ttk.Label(bottom, text="Notes:").grid(row=1, column=0, sticky="nw", pady=(6, 0))
        self.result_detail_text = tk.Text(bottom, height=4, wrap="word")
        self.result_detail_text.grid(row=1, column=1, columnspan=8, sticky="nsew", padx=(5, 0), pady=(6, 0))
        ttk.Label(bottom, text="Matching snippets:").grid(row=2, column=0, sticky="nw", pady=(6, 0))
        self.result_snippets_text = tk.Text(bottom, height=5, wrap="word", state="disabled")
        self.result_snippets_text.grid(row=2, column=1, columnspan=8, sticky="nsew", padx=(5, 0), pady=(6, 0))
        exports = ttk.Frame(bottom)
        exports.grid(row=3, column=1, columnspan=8, sticky="w", pady=(6, 0))
        ttk.Button(exports, text="Export CSV", command=lambda: self.export_results("csv")).grid(row=0, column=0, padx=2)
        ttk.Button(exports, text="Export JSON", command=lambda: self.export_results("json")).grid(row=0, column=1, padx=2)
        ttk.Button(exports, text="Export Markdown", command=lambda: self.export_results("markdown")).grid(row=0, column=2, padx=2)
        ttk.Button(exports, text="Review package", command=self.export_review_package_ui).grid(row=0, column=3, padx=2)
        pane.add(bottom, weight=1)

    def create_ai_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(4, weight=1)
        self.notebook.add(tab, text="AI relevance")

        ttk.Label(tab, text="AI relevance review", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            tab,
            text=(
                "After a normal scan, describe what you are actually searching for. Archive Scout selects a bounded set of "
                "existing report matches, sends compact excerpts to the configured OpenAI or OpenRouter provider, and stores a separate explainable relevance ranking. "
                "Deterministic Archive Scout scores and reviews are never replaced."
            ),
            style="Muted.TLabel", wraplength=1080,
        ).grid(row=1, column=0, sticky="ew", pady=(2, 8))

        settings = ttk.Frame(tab)
        settings.grid(row=2, column=0, sticky="ew")
        settings.columnconfigure(1, weight=1)
        ttk.Label(settings, text="Scan:").grid(row=0, column=0, sticky="w")
        self.ai_scan_box = ttk.Combobox(settings, textvariable=self.ai_scan_var, state="readonly", width=36)
        self.ai_scan_box.grid(row=0, column=1, sticky="ew", padx=(5, 12))
        self.ai_scan_box.bind("<<ComboboxSelected>>", lambda _e: self.refresh_ai_runs())
        ttk.Label(settings, text="Model:").grid(row=0, column=2, sticky="e")
        ttk.Entry(settings, textvariable=self.ai_model_var, width=18).grid(row=0, column=3, padx=(5, 12))
        ttk.Label(settings, text="Candidates:").grid(row=0, column=4)
        ttk.Entry(settings, textvariable=self.ai_candidate_limit_var, width=7).grid(row=0, column=5, padx=(5, 12))
        ttk.Label(settings, text="Minimum relevance:").grid(row=0, column=6)
        ttk.Entry(settings, textvariable=self.ai_min_relevance_var, width=6).grid(row=0, column=7, padx=(5, 0))

        ttk.Label(settings, text="Provider:").grid(row=1, column=0, sticky="w", pady=(7, 0))
        provider_box = ttk.Combobox(settings, textvariable=self.ai_provider_var, values=("openai", "openrouter"), state="readonly", width=14)
        provider_box.grid(row=1, column=1, sticky="w", padx=(5, 12), pady=(7, 0))
        provider_box.bind("<<ComboboxSelected>>", lambda _e: self.on_ai_provider_changed())
        ttk.Label(settings, text="API key:").grid(row=1, column=2, sticky="e", pady=(7, 0))
        key_entry = ttk.Entry(settings, textvariable=self.ai_api_key_var, show="•")
        key_entry.grid(row=1, column=3, columnspan=2, sticky="ew", padx=(5, 12), pady=(7, 0))
        ttk.Label(
            settings,
            text="Session only; environment variables are also supported. Keys are never written to the project.",
            style="Muted.TLabel",
        ).grid(row=1, column=5, columnspan=3, sticky="w", pady=(7, 0))

        prompt_frame = ttk.LabelFrame(tab, text="What are you searching for?", padding=8)
        prompt_frame.grid(row=3, column=0, sticky="ew", pady=(10, 8))
        prompt_frame.columnconfigure(0, weight=1)
        self.ai_prompt_text = tk.Text(prompt_frame, height=4, wrap="word")
        self.ai_prompt_text.grid(row=0, column=0, columnspan=6, sticky="ew")
        actions = ttk.Frame(prompt_frame)
        actions.grid(row=1, column=0, sticky="ew", pady=(7, 0))
        actions.columnconfigure(5, weight=1)
        self.ai_start_button = ttk.Button(actions, text="Analyze report", command=self.start_ai_review_ui, style="Accent.TButton")
        self.ai_start_button.grid(row=0, column=0, padx=(0, 5))
        ttk.Button(actions, text="Refresh previous reviews", command=self.refresh_ai_runs).grid(row=0, column=1, padx=5)
        ttk.Label(actions, text="Previous review:").grid(row=0, column=2, padx=(12, 4))
        self.ai_run_box = ttk.Combobox(actions, textvariable=self.ai_run_var, state="readonly", width=48)
        self.ai_run_box.grid(row=0, column=3, sticky="ew")
        self.ai_run_box.bind("<<ComboboxSelected>>", lambda _e: self.refresh_ai_results())
        ttk.Button(actions, text="Open reports", command=self.open_reports).grid(row=0, column=4, padx=(8, 0))

        pane = ttk.Panedwindow(tab, orient="vertical")
        pane.grid(row=4, column=0, sticky="nsew")
        top = ttk.Frame(pane)
        top.columnconfigure(0, weight=1)
        top.rowconfigure(0, weight=1)
        columns = ("relevance", "confidence", "archive_score", "timestamp", "title", "url", "category")
        self.ai_results_tree = ttk.Treeview(top, columns=columns, show="headings", selectmode="browse")
        widths = {
            "relevance": 80, "confidence": 80, "archive_score": 90, "timestamp": 120,
            "title": 230, "url": 380, "category": 150,
        }
        for column in columns:
            label = "Archive score" if column == "archive_score" else column.replace("_", " ").title()
            self.ai_results_tree.heading(column, text=label)
            self.ai_results_tree.column(column, width=widths[column], anchor="w")
        self.ai_results_tree.grid(row=0, column=0, sticky="nsew")
        self.ai_results_tree.bind("<<TreeviewSelect>>", self.load_selected_ai_result)
        scroll = ttk.Scrollbar(top, orient="vertical", command=self.ai_results_tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.ai_results_tree.configure(yscrollcommand=scroll.set)
        pane.add(top, weight=3)

        bottom = ttk.Frame(pane, padding=(0, 6, 0, 0))
        bottom.columnconfigure(0, weight=1)
        bottom.rowconfigure(0, weight=1)
        self.ai_detail_text = tk.Text(bottom, height=7, wrap="word", state="disabled")
        self.ai_detail_text.grid(row=0, column=0, columnspan=4, sticky="nsew")
        ttk.Button(bottom, text="Open local", command=self.open_selected_ai_local).grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Button(bottom, text="Open Wayback", command=self.open_selected_ai_wayback).grid(row=1, column=1, sticky="w", padx=5, pady=(6, 0))
        ttk.Button(bottom, text="Copy URL", command=self.copy_selected_ai_url).grid(row=1, column=2, sticky="w", padx=5, pady=(6, 0))
        pane.add(bottom, weight=1)

    def create_research_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(4, weight=1)
        self.notebook.add(tab, text="Research intelligence")
        ttk.Label(tab, text="Research Intelligence", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            tab,
            text=(
                "Search the entire saved project as one evidence corpus. Archive Scout combines a local vector index, full-text search, "
                "entities, duplicate clusters, archive scores, and page-to-page relationships. Deep review is optional and sends only a "
                "bounded evidence set to your selected AI provider; archived text is always treated as untrusted source material."
            ),
            style="Muted.TLabel", wraplength=1080,
        ).grid(row=1, column=0, sticky="ew", pady=(2, 8))
        settings = ttk.Frame(tab)
        settings.grid(row=2, column=0, sticky="ew")
        settings.columnconfigure(1, weight=1)
        ttk.Label(settings, text="Local vector backend:").grid(row=0, column=0, sticky="w")
        ttk.Combobox(settings, textvariable=self.research_backend_var, values=("local-hash", "fastembed"), state="readonly", width=16).grid(row=0, column=1, sticky="w", padx=(5, 12))
        ttk.Checkbutton(settings, text="Refresh research index after normal scans", variable=self.research_auto_var).grid(row=0, column=2, sticky="w", padx=(0, 12))
        ttk.Label(settings, text="Result limit:").grid(row=0, column=3, sticky="e")
        ttk.Entry(settings, textvariable=self.research_limit_var, width=7).grid(row=0, column=4, padx=(5, 0))

        query = ttk.LabelFrame(tab, text="Research question", padding=8)
        query.grid(row=3, column=0, sticky="ew", pady=(10, 8))
        query.columnconfigure(0, weight=1)
        ttk.Entry(query, textvariable=self.research_query_var).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.research_search_button = ttk.Button(query, text="Search evidence", command=self.start_research_search_ui, style="Accent.TButton")
        self.research_search_button.grid(row=0, column=1, padx=3)
        self.research_ai_button = ttk.Button(query, text="Deep AI review", command=self.start_research_ai_ui)
        self.research_ai_button.grid(row=0, column=2, padx=3)
        self.research_index_button = ttk.Button(query, text="Build / refresh index", command=self.start_research_index_ui)
        self.research_index_button.grid(row=0, column=3, padx=(8, 0))

        pane = ttk.Panedwindow(tab, orient="vertical")
        pane.grid(row=4, column=0, sticky="nsew")
        top = ttk.Frame(pane)
        top.columnconfigure(0, weight=1)
        top.rowconfigure(0, weight=1)
        columns = ("score", "vector", "archive", "timestamp", "title", "url")
        self.research_results_tree = ttk.Treeview(top, columns=columns, show="headings", selectmode="browse")
        widths = {"score": 72, "vector": 72, "archive": 72, "timestamp": 120, "title": 260, "url": 420}
        for column in columns:
            self.research_results_tree.heading(column, text=column.title())
            self.research_results_tree.column(column, width=widths[column], anchor="w")
        self.research_results_tree.grid(row=0, column=0, sticky="nsew")
        self.research_results_tree.bind("<<TreeviewSelect>>", self.load_selected_research_result)
        scroll = ttk.Scrollbar(top, orient="vertical", command=self.research_results_tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.research_results_tree.configure(yscrollcommand=scroll.set)
        pane.add(top, weight=3)

        bottom = ttk.Frame(pane, padding=(0, 6, 0, 0))
        bottom.columnconfigure(0, weight=1)
        bottom.rowconfigure(0, weight=1)
        self.research_detail_text = tk.Text(bottom, height=9, wrap="word", state="disabled")
        self.research_detail_text.grid(row=0, column=0, columnspan=3, sticky="nsew")
        ttk.Button(bottom, text="Open Wayback", command=self.open_selected_research_wayback).grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Button(bottom, text="Copy URL", command=self.copy_selected_research_url).grid(row=1, column=1, sticky="w", padx=5, pady=(6, 0))
        pane.add(bottom, weight=1)

    def create_history_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=8)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)
        self.notebook.add(tab, text="Scan history")
        columns = ("id", "set", "status", "started", "documents", "matches", "seconds", "operation")
        self.history_tree = ttk.Treeview(tab, columns=columns, show="headings", selectmode="extended")
        for column in columns:
            self.history_tree.heading(column, text=column.title())
            self.history_tree.column(column, width=110 if column not in {"set", "operation"} else 180)
        self.history_tree.grid(row=0, column=0, sticky="nsew")
        buttons = ttk.Frame(tab)
        buttons.grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Button(buttons, text="Refresh", command=self.refresh_history).grid(row=0, column=0, padx=2)
        ttk.Button(buttons, text="Rename", command=self.rename_selected_scan).grid(row=0, column=1, padx=2)
        ttk.Button(buttons, text="Regenerate reports", command=self.regenerate_selected_scan).grid(row=0, column=2, padx=2)
        ttk.Button(buttons, text="Delete scan results", command=self.delete_selected_scan).grid(row=0, column=3, padx=2)
        ttk.Button(buttons, text="Compare two scans", command=self.compare_selected_scans).grid(row=0, column=4, padx=2)

    def create_errors_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=8)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=2)
        tab.rowconfigure(3, weight=1)
        self.notebook.add(tab, text="Errors")

        controls = ttk.Frame(tab)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(controls, text="Category:").grid(row=0, column=0)
        self.error_category_box = ttk.Combobox(controls, textvariable=self.error_category_var, state="readonly", values=("All",), width=24)
        self.error_category_box.grid(row=0, column=1, padx=5)
        ttk.Button(controls, text="Refresh", command=self.refresh_errors).grid(row=0, column=2, padx=2)
        ttk.Button(controls, text="Retry selected errors", command=self.retry_selected_errors).grid(row=0, column=3, padx=2)
        ttk.Button(controls, text="Ignore selected", command=self.ignore_selected_errors).grid(row=0, column=4, padx=2)
        columns = ("operation", "category", "attempts", "retryable", "last_seen", "url", "message")
        self.errors_tree = ttk.Treeview(tab, columns=columns, show="headings", selectmode="extended")
        for column in columns:
            self.errors_tree.heading(column, text=column.title())
            self.errors_tree.column(column, width=110 if column not in {"url", "message"} else 300)
        self.errors_tree.grid(row=1, column=0, sticky="nsew")

        site_header = ttk.Frame(tab)
        site_header.grid(row=2, column=0, sticky="ew", pady=(10, 5))
        ttk.Label(site_header, text="Site-specific Wayback issues", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            site_header,
            text="Archive exclusions, robots restrictions, missing replays, rate limits, and other host-level problems are summarized here instead of being buried in generic errors.",
            style="Muted.TLabel", wraplength=780,
        ).grid(row=1, column=0, sticky="w")
        ttk.Button(site_header, text="Refresh", command=self.refresh_site_issues).grid(row=0, column=1, rowspan=2, padx=(12, 4))
        ttk.Button(site_header, text="Mark resolved", command=self.resolve_selected_site_issues).grid(row=0, column=2, rowspan=2, padx=4)
        site_columns = ("host", "stage", "category", "status", "count", "last_seen", "message")
        self.site_issues_tree = ttk.Treeview(tab, columns=site_columns, show="headings", selectmode="extended")
        widths = {"host": 180, "stage": 130, "category": 150, "status": 70, "count": 65, "last_seen": 150, "message": 430}
        for column in site_columns:
            self.site_issues_tree.heading(column, text=column.replace("_", " ").title())
            self.site_issues_tree.column(column, width=widths[column], anchor="w")
        self.site_issues_tree.grid(row=3, column=0, sticky="nsew")


    def create_activity_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)
        self.notebook.add(tab, text="Activity")
        self.log_text = tk.Text(tab, wrap="word", state="disabled", font="TkFixedFont")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(tab, orient="vertical", command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)

    def lines_from(self, widget: tk.Text) -> list[str]:
        return [line.strip() for line in widget.get("1.0", "end").splitlines() if line.strip()]

    def replace_text(self, widget: tk.Text, values: list[str]) -> None:
        widget.delete("1.0", "end")
        widget.insert("1.0", "\n".join(values))

    def update_operation_help(self) -> None:
        self.operation_help_var.set(MODE_HELP.get(MODE_LABELS.get(self.mode_var.get(), "all"), ""))

    def save_current_keyword_set(self) -> None:
        if 0 <= self.current_keyword_set < len(self.keyword_sets):
            self.keyword_sets[self.current_keyword_set]["rules"] = self.lines_from(self.keywords_text)
            self.keyword_sets[self.current_keyword_set]["selected"] = self.keyword_set_selected_var.get()

    def refresh_keyword_set_box(self, selected_index: int | None = None) -> None:
        names = [item["name"] for item in self.keyword_sets]
        self.keyword_set_box.configure(values=names)
        if not names:
            self.current_keyword_set = -1
            self.keyword_set_var.set("")
            self.replace_text(self.keywords_text, [])
            return
        index = selected_index if selected_index is not None else min(max(self.current_keyword_set, 0), len(names)-1)
        self.current_keyword_set = index
        self.keyword_set_var.set(names[index])
        self.keyword_set_selected_var.set(bool(self.keyword_sets[index].get("selected", True)))
        self.replace_text(self.keywords_text, list(self.keyword_sets[index].get("rules") or []))

    def switch_keyword_set(self, _event=None) -> None:
        self.save_current_keyword_set()
        name = self.keyword_set_var.get()
        for index, item in enumerate(self.keyword_sets):
            if item["name"] == name:
                self.refresh_keyword_set_box(index)
                break

    def new_keyword_set(self) -> None:
        self.save_current_keyword_set()
        name = simpledialog.askstring(APP_NAME, "Name for the new keyword set:", initialvalue="New keyword set")
        if not name:
            return
        existing = {item["name"].casefold() for item in self.keyword_sets}
        base = name.strip()
        candidate = base
        suffix = 2
        while candidate.casefold() in existing:
            candidate = f"{base} {suffix}"
            suffix += 1
        self.keyword_sets.append({"name": candidate, "rules": [], "selected": True})
        self.refresh_keyword_set_box(len(self.keyword_sets)-1)

    def duplicate_keyword_set(self) -> None:
        self.save_current_keyword_set()
        if self.current_keyword_set < 0:
            return
        source = self.keyword_sets[self.current_keyword_set]
        self.keyword_sets.append({"name": source["name"] + " copy", "rules": list(source["rules"]), "selected": True})
        self.refresh_keyword_set_box(len(self.keyword_sets)-1)

    def delete_keyword_set(self) -> None:
        if self.current_keyword_set < 0:
            return
        if not messagebox.askyesno(APP_NAME, "Delete this keyword set from the project configuration?"):
            return
        del self.keyword_sets[self.current_keyword_set]
        self.refresh_keyword_set_box(max(0, self.current_keyword_set-1))

    def import_keyword_set(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("Text or JSON", "*.txt *.json"), ("All files", "*")])
        if not path:
            return
        p = Path(path)
        if p.suffix.casefold() == ".json":
            payload = json.loads(p.read_text(encoding="utf-8"))
            name = str(payload.get("name") or p.stem)
            rules = list(payload.get("rules") or payload.get("keywords") or [])
        else:
            name = p.stem
            rules = [line.strip() for line in p.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
        self.keyword_sets.append({"name": name, "rules": rules, "selected": True})
        self.refresh_keyword_set_box(len(self.keyword_sets)-1)

    def export_keyword_set(self) -> None:
        self.save_current_keyword_set()
        if self.current_keyword_set < 0:
            return
        item = self.keyword_sets[self.current_keyword_set]
        path = filedialog.asksaveasfilename(defaultextension=".txt", initialfile=item["name"] + ".txt")
        if path:
            Path(path).write_text("\n".join(item["rules"]) + "\n", encoding="utf-8")

    def apply_preset(self) -> None:
        preset = PRESETS[self.preset_var.get()]
        self.replace_text(self.targets_text, list(preset["targets"]))
        self.keyword_sets = [{"name": "Current keywords", "rules": list(preset["keywords"]), "selected": True}]
        self.refresh_keyword_set_box(0)
        self.from_date_var.set(str(preset.get("from_date", preset["from_year"])))
        self.to_date_var.set(str(preset.get("to_date", preset["to_year"])))
        self.replace_text(self.cdx_filters_text, list(preset.get("cdx_filters", ["statuscode:200"])))
        self.replace_text(self.cdx_extra_text, list(preset.get("cdx_extra_params", [])))
        collapses = set(preset.get("cdx_collapses", ["urlkey"]))
        self.collapse_urlkey_var.set("urlkey" in collapses)
        self.collapse_digest_var.set("digest" in collapses)
        self.cdx_match_type_var.set(preset.get("cdx_match_type") or "Automatic")
        self.replace_text(self.media_include_text, list(DEFAULT_IMAGE_EXTENSIONS) + list(DEFAULT_VIDEO_EXTENSIONS))

    def build_config(self, require_keywords: bool = True) -> ProjectConfig:
        self.save_current_keyword_set()
        selected_mode = MODE_LABELS[self.mode_var.get()]
        external_after_scan = selected_mode == "external_media_after_scan"
        try:
            media = MediaConfig(
                enabled=self.media_enabled_var.get() or external_after_scan,
                targets=self.lines_from(self.media_targets_text),
                include_images=self.media_images_var.get(),
                include_videos=self.media_videos_var.get(),
                include_extensions=self.lines_from(self.media_include_text),
                exclude_extensions=self.lines_from(self.media_exclude_text),
                discover_embedded=self.media_embedded_var.get() or external_after_scan,
                allow_external_embeds=self.media_external_var.get() or external_after_scan,
                snapshot_strategy=self.media_strategy_var.get(),
                max_file_mb=float(self.media_max_var.get()),
                preserve_paths=False,
            )
            analysis = AnalysisConfig(
                forum_profile=self.forum_profile_var.get(),
                reconstruct_threads=self.analysis_threads_var.get(),
                extract_legacy_embeds=self.analysis_embeds_var.get(),
                extractor_rules=self.lines_from(self.analysis_extractors_text),
                search_external_assets=self.analysis_external_var.get(),
                external_domains=self.lines_from(self.analysis_domains_text),
                external_asset_limit=int(self.analysis_external_limit_var.get()),
                duplicate_threshold=float(self.analysis_duplicate_var.get()),
                compare_snapshots=self.analysis_compare_var.get(),
                build_provenance=self.analysis_provenance_var.get(),
                merge_source=self.analysis_merge_source_var.get(),
            )
            ai = AIConfig(
                provider=self.ai_provider_var.get(),
                model=self.ai_model_var.get(),
                candidate_limit=int(self.ai_candidate_limit_var.get()),
                batch_size=int(self.ai_batch_size_var.get()),
                minimum_relevance=int(self.ai_min_relevance_var.get()),
                excerpt_chars=int(self.ai_excerpt_chars_var.get()),
            )
            research = ResearchConfig(
                enabled=True,
                auto_build=self.research_auto_var.get(),
                vector_backend=self.research_backend_var.get(),
                result_limit=int(self.research_limit_var.get()),
            )
            network = NetworkConfig(
                backend=self.network_backend_var.get(),
                trust_environment=self.network_trust_env_var.get(),
                endpoint_mode=self.network_endpoint_var.get(),
                index_strategy=self.network_strategy_var.get(),
                page_blocks=int(self.network_page_blocks_var.get()),
                cdx_workers=int(self.network_cdx_workers_var.get()),
                persistent_retries=self.network_persistent_var.get(),
                retry_base_seconds=float(self.network_retry_base_var.get()),
                retry_max_seconds=float(self.network_retry_max_var.get()),
                failure_pause_threshold=int(self.network_failure_limit_var.get()),
            )
            from_date = normalize_cdx_date(self.from_date_var.get(), end=False)
            to_date = normalize_cdx_date(self.to_date_var.get(), end=True)
            config = ProjectConfig(
                output_dir=Path(self.output_var.get()),
                targets=self.lines_from(self.targets_text),
                keywords=list(self.keyword_sets[0]["rules"]) if self.keyword_sets else [],
                keyword_set_name=self.keyword_sets[0]["name"] if self.keyword_sets else "Current keywords",
                keyword_sets=[KeywordSetConfig(item["name"], list(item["rules"]), bool(item.get("selected", True))) for item in self.keyword_sets],
                from_year=int(from_date[:4]),
                to_year=int(to_date[:4]),
                from_date=from_date,
                to_date=to_date,
                cdx_filters=self.lines_from(self.cdx_filters_text),
                cdx_collapses=[value for value, enabled in (("urlkey", self.collapse_urlkey_var.get()), ("digest", self.collapse_digest_var.get())) if enabled],
                cdx_match_type="" if self.cdx_match_type_var.get() == "Automatic" else self.cdx_match_type_var.get(),
                cdx_extra_params=self.lines_from(self.cdx_extra_text),
                page_size=int(self.page_size_var.get()),
                workers=int(self.workers_var.get()),
                scan_workers=int(self.scan_workers_var.get()),
                download_scope=SCOPE_LABELS[self.scope_var.get()],
                minimum_score=int(self.minimum_score_var.get()),
                report=ReportConfig(
                    retain_scan_details=self.report_retain_var.get(),
                    sort_order=self.report_sort_var.get(),
                    max_matches=int(self.report_max_var.get()),
                    snippet_limit=int(self.report_snippets_var.get()),
                    snippet_chars=int(self.report_chars_var.get()),
                    link_limit=int(self.report_links_var.get()),
                    outputs=[name for name, variable in self.report_output_vars.items() if variable.get()],
                    fields={
                        name: [field for field, variable in variables.items() if variable.get()]
                        for name, variables in self.report_field_vars.items()
                    },
                ),
                max_file_mb=float(self.max_file_var.get()),
                cdx_delay=float(self.cdx_delay_var.get()),
                download_delay=float(self.download_delay_var.get()),
                rate_limit_base_pause=float(self.rate_limit_base_var.get()),
                rate_limit_max_pause=float(self.rate_limit_max_var.get()),
                rate_limit_max_wait=float(self.rate_limit_wait_var.get()) * 60.0,
                media=media,
                analysis=analysis,
                ai=ai,
                research=research,
                network=network,
                target_settings=self.target_settings,
                auto_backup=self.auto_backup_var.get(),
                backup_keep=int(self.backup_keep_var.get()),
                backup_max_mb=float(self.backup_max_var.get()),
                hitlist_keywords=self.lines_from(self.hitlist_text),
                hitlist_file=self.hitlist_file_var.get(),
                import_source=self.import_source_var.get(),
            ).normalized()
        except (ValueError, KeyError) as exc:
            raise ValueError(f"Check the numeric settings, keyword rules, and target lines: {exc}") from exc
        mode = selected_mode
        if mode in {"all", "external_media_after_scan", "index", "download_only"} and not config.targets:
            raise ValueError("Add at least one site or path.")
        if require_keywords and mode in {"all", "external_media_after_scan", "download", "resume", "rescan", "retry_errors"} and not config.selected_keyword_sets():
            raise ValueError("Select at least one non-empty keyword set.")
        if mode == "merge_project" and not config.analysis.merge_source:
            raise ValueError("Choose a source project folder in Archive analysis.")
        if mode == "import_folder" and not config.import_source:
            raise ValueError("Choose an existing archive folder in Settings.")
        return config

    def preview_cdx(self) -> None:
        try:
            config = self.build_config(require_keywords=False)
            if not config.targets:
                raise ValueError("Add a target first.")
            window = cdx_year_window(config, config.from_year)
            if not window:
                raise ValueError("The selected date range does not contain an indexable year.")
            target = config.targets[0]
            config = config.for_target(target)
            window = cdx_year_window(config, config.from_year)
            if not window:
                raise ValueError("The target date range does not contain an indexable year.")
            if preferred_index_strategy(config, target) == "paged":
                network = config.network.normalized()
                blocks = 9 if network.index_strategy == "auto" else network.page_blocks
                params = build_num_pages_params(config, target, *window, blocks)
                endpoint = cdx_paged_endpoints(config)[0]
            else:
                params = build_cdx_params(config, target, *window)
                endpoint = cdx_endpoints(config)[0]
            url = cdx_request_url(endpoint, params)
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        dialog = tk.Toplevel(self)
        dialog.title("CDX request preview")
        dialog.geometry("860x540")
        ttk.Label(dialog, text="First request for this target/window. Copy URL sends the exact valid request; parameters below are shown decoded.", wraplength=820).pack(anchor="w", padx=10, pady=(10, 0))
        def copy_url():
            dialog.clipboard_clear()
            dialog.clipboard_append(url)
        ttk.Button(dialog, text="Copy request URL", command=copy_url).pack(anchor="w", padx=10, pady=6)
        text = tk.Text(dialog, wrap="word", font="TkFixedFont")
        text.pack(fill="both", expand=True, padx=10, pady=10)
        text.insert("1.0", url + "\n\nParameters (readable):\n" + "\n".join(f"{key} = {value}" for key, value in params))
        text.configure(state="disabled")

    def choose_output(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output_var.get() or str(Path.home()))
        if selected:
            self.output_var.set(selected)

    def open_output(self) -> None:
        open_path(Path(self.output_var.get()).expanduser())

    def open_reports(self) -> None:
        open_path(Path(self.output_var.get()).expanduser() / "reports")

    def start(self, override_config: ProjectConfig | None = None, override_mode: str | None = None) -> None:
        if self.worker_thread:
            if self.worker_thread.is_alive():
                self.status_var.set("The previous run is still shutting down…")
                self.log("Start ignored because the previous worker is still active.")
                messagebox.showinfo(APP_NAME, "The previous run is still active or shutting down. Wait for it to finish, then press Start again.")
                return
            self.worker_thread = None
        try:
            config = override_config or self.build_config()
        except (ValueError, FrozenBundleError) as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        mode = override_mode or MODE_LABELS[self.mode_var.get()]
        self.stop_event.clear()
        self.progress_var.set(0)
        self.status_var.set("Starting…")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.start_button.configure(state="disabled")
        if hasattr(self, "ai_start_button"):
            self.ai_start_button.configure(state="disabled")
        for name in ("research_search_button", "research_ai_button", "research_index_button"):
            if hasattr(self, name):
                getattr(self, name).configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.log(f"Starting {mode} in {config.output_dir}")
        self.worker_thread = threading.Thread(target=self.run_worker, args=(config, mode), daemon=True)
        try:
            self.worker_thread.start()
        except Exception as exc:
            self.worker_thread = None
            self.progress.stop()
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
            for name in ("ai_start_button", "research_search_button", "research_ai_button", "research_index_button"):
                if hasattr(self, name):
                    getattr(self, name).configure(state="normal")
            self.status_var.set("Could not start operation")
            self.log(f"Worker startup failed: {exc}")
            messagebox.showerror(APP_NAME, f"Could not start operation: {exc}")

    def run_worker(self, config: ProjectConfig, mode: str) -> None:
        try:
            self.on_engine_event(ProgressEvent("starting", "Opening project and preparing operation…"))
            # Bundle validation, migrations, backups, database setup, and every
            # other potentially slow initialization step belong on the worker
            # thread. The Start button must never leave Tk blocked at Starting…
            # before a worker exists to report progress or failure.
            ensure_frozen_bundle_available()
            self.events.put(("complete", run_project(config, mode, self.stop_event, self.on_engine_event)))
        except (RateLimitDeferred, ConnectivityPaused) as exc:
            self.events.put(("deferred", str(exc)))
        except Stopped:
            self.events.put(("stopped", None))
        except FrozenBundleError as exc:
            self.events.put(("error", str(exc)))
        except Exception:
            self.events.put(("error", traceback.format_exc()))

    def on_engine_event(self, event: ProgressEvent) -> None:
        self.events.put_progress(event)

    def stop(self) -> None:
        self.stop_event.set()
        self.status_var.set("Stopping after the current request…")
        self.stop_button.configure(state="disabled")

    def process_events(self) -> None:
        processed = 0
        try:
            while processed < 500:
                kind, payload = self.events.get_nowait()
                processed += 1
                if kind == "progress":
                    event = payload
                    self.status_var.set(event.message)
                    self.update_dashboard_from_progress(event)
                    self.log(event.message)
                    if event.current is not None and event.total:
                        self.progress.configure(mode="determinate")
                        self.progress_var.set(event.current / event.total * 100)
                    else:
                        self.progress.configure(mode="indeterminate")
                        self.progress.start(12)
                elif kind == "complete":
                    self.last_paths = payload
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.progress_var.set(100)
                    self.status_var.set("Complete")
                    self.log("Complete. Reports are ready.")
                    self.finish_run()
                    self.refresh_dashboard()
                    self.refresh_history()
                    self.refresh_results()
                    self.refresh_errors()
                    messagebox.showinfo(APP_NAME, "The run is complete.")
                elif kind == "ai_complete":
                    run_id = int(payload["run_id"])
                    self.last_paths.update(payload.get("paths") or {})
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.progress_var.set(100)
                    self.status_var.set("AI relevance review complete")
                    self.log(f"AI relevance review {run_id} complete. Reports are ready.")
                    self.finish_run()
                    self.refresh_ai_runs(select_run_id=run_id)
                    messagebox.showinfo(APP_NAME, "AI relevance review is complete. The most relevant pages are displayed in AI relevance.")
                elif kind == "research_results":
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.progress_var.set(100)
                    self.populate_research_results(payload)
                    self.status_var.set(f"Research search complete: {len(payload):,} results")
                    self.finish_run()
                elif kind == "research_ai_complete":
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.progress_var.set(100)
                    self.populate_research_results(payload.get("evidence") or [])
                    self.show_research_answer(payload)
                    self.status_var.set("Grounded AI review complete")
                    self.finish_run()
                elif kind == "stopped":
                    self.progress.stop()
                    self.status_var.set("Stopped. Progress was saved.")
                    self.finish_run()
                    self.refresh_dashboard()
                elif kind == "deferred":
                    self.progress.stop()
                    self.status_var.set("Paused safely because Wayback is unreachable. Progress was saved.")
                    self.log(str(payload))
                    self.finish_run()
                    self.refresh_dashboard()
                    messagebox.showinfo(
                        APP_NAME,
                        "Archive Scout could not obtain a stable Wayback connection after trying the available connection methods. "
                        "The exact queue was saved instead of marking the project failed. Use Resume after connectivity recovers.\n\n" + str(payload),
                    )
                elif kind == "error":
                    self.progress.stop()
                    self.status_var.set("Error")
                    self.log(str(payload))
                    self.finish_run()
                    self.refresh_dashboard()
                    messagebox.showerror(APP_NAME, str(payload))
        except queue.Empty:
            pass
        # A very large archive can emit progress faster than Tk can paint.
        # Drain bursts promptly while still yielding to the event loop so the
        # window remains responsive instead of accumulating an unbounded queue.
        self.after(10 if processed >= 500 else 100, self.process_events)

    def finish_run(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            self.after(50, self.finish_run)
            return
        self.worker_thread = None
        self.start_button.configure(state="normal")
        if hasattr(self, "ai_start_button"):
            self.ai_start_button.configure(state="normal")
        for name in ("research_search_button", "research_ai_button", "research_index_button"):
            if hasattr(self, name):
                getattr(self, name).configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.save_app_state()

    def log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{timestamp}] {message}\n")
        self.log_line_count += 1
        if self.log_line_count > 5000:
            self.log_text.delete("1.0", "501.0")
            self.log_line_count -= 500
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def project_database(self):
        return open_database(Path(self.output_var.get()).expanduser(), migrate=True)

    def refresh_scan_choices(self, database=None) -> None:
        own = database is None
        database = database or self.project_database()
        try:
            runs = list_scan_runs(database)
            self.scan_run_map = {f"{row['id']} — {row['keyword_set_name']} — {row['status']}": int(row["id"]) for row in runs}
            values = list(self.scan_run_map)
            self.result_scan_box.configure(values=values)
            if hasattr(self, "ai_scan_box"):
                self.ai_scan_box.configure(values=values)
            if values and self.result_scan_var.get() not in self.scan_run_map:
                self.result_scan_var.set(values[0])
            if values and self.ai_scan_var.get() not in self.scan_run_map:
                self.ai_scan_var.set(values[0])
        finally:
            if own:
                database.close()

    def refresh_results(self, reset_page: bool = False) -> None:
        if reset_page:
            self.result_page = 0
        root = Path(self.output_var.get()).expanduser()
        if not (root / "archive_scout.sqlite3").exists():
            return
        database = self.project_database()
        try:
            self.refresh_scan_choices(database)
            scan_id = self.scan_run_map.get(self.result_scan_var.get())
            if scan_id is None:
                return
            status = REVIEW_LABELS.get(self.result_review_filter_var.get(), "") if self.result_review_filter_var.get() != "All" else ""
            minimum = int(self.result_min_var.get() or 0)
            search_value = self.result_filter_var.get()
            total = result_count(database, scan_id, minimum, status, search_value)
            max_page = max(0, (total - 1) // self.result_page_size)
            self.result_page = min(self.result_page, max_page)
            rows = result_rows(
                database,
                scan_id,
                minimum,
                status,
                search_value,
                limit=self.result_page_size,
                offset=self.result_page * self.result_page_size,
            )
            self.result_page_var.set(f"Page {self.result_page + 1}/{max_page + 1} · {total:,}")
            self.results_tree.delete(*self.results_tree.get_children())
            self.result_row_map.clear()
            for row in rows:
                status_value = row["review_status"]
                item = self.results_tree.insert("", "end", values=(row["score"], status_value, row["timestamp"], row["title"] or "(untitled)", row["original_url"], len(json.loads(row["hits_json"] or "{}"))), tags=(status_value,))
                self.result_row_map[item] = dict(row)
        except Exception as exc:
            self.log(f"Could not load results: {exc}")
        finally:
            database.close()

    def previous_result_page(self) -> None:
        if self.result_page > 0:
            self.result_page -= 1
            self.refresh_results()

    def next_result_page(self) -> None:
        self.result_page += 1
        self.refresh_results()

    def run_fts_search(self) -> None:
        database = self.project_database()
        try:
            rows = search_documents(
                database,
                self.fts_query_var.get(),
                field=self.fts_field_var.get(),
                domain=self.fts_domain_var.get(),
                scan_run_id=self.current_scan_id(),
            )
            self.results_tree.delete(*self.results_tree.get_children())
            self.result_row_map.clear()
            for row in rows:
                data = dict(row)
                data.update({"id": 0, "score": round(-float(row["rank"]), 3), "review_status": "search", "hits_json": "{}", "snippets_json": json.dumps([row["snippet"] or ""]), "note": "", "tags": ""})
                item = self.results_tree.insert("", "end", values=(data["score"], "search", row["timestamp"], row["title"] or "(untitled)", row["original_url"], "FTS"))
                self.result_row_map[item] = data
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))
        finally:
            database.close()

    def sort_result_tree(self, column: str) -> None:
        if self.result_sort_column == column:
            self.result_sort_reverse = not self.result_sort_reverse
        else:
            self.result_sort_column = column
            self.result_sort_reverse = column in {"score", "timestamp", "hits"}
        items = list(self.results_tree.get_children())
        def key(item: str):
            value = self.results_tree.set(item, column)
            if column in {"score", "hits"}:
                try:
                    return float(value)
                except ValueError:
                    return float("-inf")
            return value.casefold()
        items.sort(key=key, reverse=self.result_sort_reverse)
        for index, item in enumerate(items):
            self.results_tree.move(item, "", index)

    def select_next_unreviewed(self) -> None:
        items = list(self.results_tree.get_children())
        if not items:
            return
        selected = self.results_tree.selection()
        start = items.index(selected[0]) + 1 if selected and selected[0] in items else 0
        for offset in range(len(items)):
            item = items[(start + offset) % len(items)]
            row = self.result_row_map.get(item, {})
            if row.get("review_status", "unreviewed") == "unreviewed":
                self.results_tree.selection_set(item)
                self.results_tree.focus(item)
                self.results_tree.see(item)
                self.load_selected_result()
                return
        messagebox.showinfo(APP_NAME, "There are no unreviewed results in the current view.")

    def selected_result(self) -> dict | None:
        selected = self.results_tree.selection()
        return self.result_row_map.get(selected[0]) if selected else None

    def load_selected_result(self, _event=None) -> None:
        row = self.selected_result()
        if not row:
            return
        reverse = {value: label for label, value in REVIEW_LABELS.items()}
        self.review_status_var.set(reverse.get(row.get("review_status"), "Unreviewed"))
        self.review_tags_var.set(row.get("tags") or "")
        snippets = json.loads(row.get("snippets_json") or "[]")
        detail = (row.get("note") or "") + ("\n\n" if row.get("note") and snippets else "") + "\n\n".join(snippets)
        self.result_detail_text.delete("1.0", "end")
        self.result_detail_text.insert("1.0", detail)

    def save_selected_review(self) -> None:
        row = self.selected_result()
        if not row or not row.get("id"):
            return
        database = self.project_database()
        try:
            with database:
                set_review(database, int(row["id"]), REVIEW_LABELS[self.review_status_var.get()])
                save_note(database, int(row["id"]), self.result_detail_text.get("1.0", "end").strip())
                set_match_tags(database, int(row["id"]), [value.strip() for value in self.review_tags_var.get().split(",")])
            self.refresh_results()
        finally:
            database.close()

    def open_selected_local(self) -> None:
        row = self.selected_result()
        if row and row.get("path"):
            path = Path(row["path"])
            if path.exists():
                if sys.platform == "darwin": subprocess.Popen(["open", str(path)])
                elif os.name == "nt": os.startfile(str(path))
                else: subprocess.Popen(["xdg-open", str(path)])

    def open_selected_wayback(self) -> None:
        row = self.selected_result()
        if row:
            webbrowser.open(replay_url(row["timestamp"], row["original_url"]))

    def copy_selected_url(self) -> None:
        row = self.selected_result()
        if row:
            self.clipboard_clear()
            self.clipboard_append(row["original_url"])

    def current_scan_id(self) -> int | None:
        return self.scan_run_map.get(self.result_scan_var.get())

    def export_results(self, format_name: str) -> None:
        scan_id = self.current_scan_id()
        if not scan_id:
            return
        extension = ".md" if format_name == "markdown" else "." + format_name
        path = filedialog.asksaveasfilename(defaultextension=extension, initialfile=f"scan-{scan_id}{extension}")
        if not path:
            return
        database = self.project_database()
        try:
            status = REVIEW_LABELS.get(self.result_review_filter_var.get(), "") if self.result_review_filter_var.get() != "All" else ""
            export_scan(
                database, scan_id, Path(path), format_name, status,
                int(self.result_min_var.get() or 0), self.result_filter_var.get(),
            )
        finally:
            database.close()

    def export_review_package_ui(self) -> None:
        scan_id = self.current_scan_id()
        if not scan_id:
            return
        path = filedialog.asksaveasfilename(defaultextension=".zip", initialfile=f"scan-{scan_id}-review.zip")
        if not path:
            return
        database = self.project_database()
        try:
            status = REVIEW_LABELS.get(self.result_review_filter_var.get(), "") if self.result_review_filter_var.get() != "All" else ""
            export_review_package(
                database, scan_id, Path(path), status,
                int(self.result_min_var.get() or 0), self.result_filter_var.get(),
            )
        finally:
            database.close()

    def on_ai_provider_changed(self) -> None:
        provider = self.ai_provider_var.get().strip().casefold()
        current = self.ai_model_var.get().strip()
        if provider == "openrouter" and (not current or current == "gpt-5-mini"):
            self.ai_model_var.set("anthropic/claude-sonnet-4.5")
        elif provider == "openai" and (not current or current.startswith("anthropic/")):
            self.ai_model_var.set("gpt-5-mini")

    def start_research_index_ui(self) -> None:
        try:
            config = self.build_config(require_keywords=False)
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self.start(config, "research_index")

    def _begin_research_worker(self, target, args: tuple, status: str) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo(APP_NAME, "Another operation is already running. Let it finish or stop it first.")
            return
        self.stop_event.clear()
        self.progress_var.set(0)
        self.status_var.set(status)
        self.start_button.configure(state="disabled")
        if hasattr(self, "ai_start_button"):
            self.ai_start_button.configure(state="disabled")
        for name in ("research_search_button", "research_ai_button", "research_index_button"):
            if hasattr(self, name):
                getattr(self, name).configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.worker_thread = threading.Thread(target=target, args=args, daemon=True)
        self.worker_thread.start()

    def start_research_search_ui(self) -> None:
        try:
            config = self.build_config(require_keywords=False)
            query = self.research_query_var.get().strip()
            if not query:
                raise ValueError("Describe what you are searching for.")
            limit = int(self.research_limit_var.get())
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self._begin_research_worker(self.run_research_search_worker, (config, query, limit), "Searching Research Intelligence…")

    def run_research_search_worker(self, config: ProjectConfig, query: str, limit: int) -> None:
        database = None
        try:
            database = open_database(config.output_dir, migrate=True)
            results = [item.to_dict() for item in search_research(config, database, query, limit)]
            self.events.put(("research_results", results))
        except Stopped:
            self.events.put(("stopped", None))
        except Exception:
            self.events.put(("error", traceback.format_exc()))
        finally:
            if database is not None:
                database.close()

    def start_research_ai_ui(self) -> None:
        try:
            config = self.build_config(require_keywords=False)
            query = self.research_query_var.get().strip()
            if not query:
                raise ValueError("Describe the research question before starting deep review.")
            api_key = self.ai_api_key_var.get().strip()
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self._begin_research_worker(self.run_research_ai_worker, (config, query, api_key), "Starting grounded AI review…")

    def run_research_ai_worker(self, config: ProjectConfig, query: str, api_key: str) -> None:
        database = None
        try:
            database = open_database(config.output_dir, migrate=True)
            answer = run_grounded_answer(
                config, database, query, api_key=api_key, stop_event=self.stop_event, callback=self.on_engine_event
            )
            self.events.put(("research_ai_complete", answer.to_dict()))
        except Stopped:
            self.events.put(("stopped", None))
        except Exception:
            self.events.put(("error", traceback.format_exc()))
        finally:
            if database is not None:
                database.close()

    def populate_research_results(self, rows: list[dict]) -> None:
        if not hasattr(self, "research_results_tree"):
            return
        self.research_results_tree.delete(*self.research_results_tree.get_children())
        self.research_result_row_map.clear()
        for row in rows:
            item = self.research_results_tree.insert(
                "", "end",
                values=(
                    f"{float(row.get('score') or 0) * 100:.1f}",
                    f"{float(row.get('vector_score') or 0) * 100:.1f}",
                    f"{float(row.get('archive_score') or 0) * 100:.1f}",
                    row.get("timestamp") or "",
                    row.get("title") or "(untitled)",
                    row.get("original_url") or "",
                ),
            )
            self.research_result_row_map[item] = dict(row)
        children = self.research_results_tree.get_children()
        if children:
            self.research_results_tree.selection_set(children[0])
            self.load_selected_research_result()

    def selected_research_result(self) -> dict | None:
        selected = self.research_results_tree.selection() if hasattr(self, "research_results_tree") else ()
        return self.research_result_row_map.get(selected[0]) if selected else None

    def load_selected_research_result(self, _event=None) -> None:
        row = self.selected_research_result()
        if not row:
            return
        entities = ", ".join(f"{item.get('kind')}:{item.get('value')}" for item in (row.get("entities") or [])[:12])
        related = ", ".join(str(value) for value in (row.get("related_document_ids") or [])[:12])
        relationship_lines = []
        for item in (row.get("relationships") or [])[:12]:
            relationship_lines.append(
                f"  {item.get('timestamp') or 'unknown time'} | {item.get('edge_type') or 'related'} | "
                f"doc {item.get('related_document_id')} | {item.get('title') or item.get('original_url') or ''}"
            )
        timeline = "\n".join(relationship_lines) or "  —"
        detail = (
            f"Research score: {float(row.get('score') or 0) * 100:.1f}/100\n"
            f"Vector similarity: {float(row.get('vector_score') or 0) * 100:.1f}/100\n"
            f"Full-text component: {float(row.get('text_score') or 0) * 100:.1f}/100\n"
            f"Entity component: {float(row.get('entity_score') or 0) * 100:.1f}/100\n"
            f"Archive Scout evidence component: {float(row.get('archive_score') or 0) * 100:.1f}/100\n"
            f"Document ID: {row.get('document_id')}\n"
            f"Duplicate group: {row.get('duplicate_group_id') or '—'}\n"
            f"Related documents: {related or '—'}\n"
            f"Entities: {entities or '—'}\n\n"
            f"Evidence graph / timeline\n{timeline}\n\n"
            f"Evidence excerpt\n{row.get('snippet') or ''}"
        )
        self.research_detail_text.configure(state="normal")
        self.research_detail_text.delete("1.0", "end")
        self.research_detail_text.insert("1.0", detail)
        self.research_detail_text.configure(state="disabled")

    def show_research_answer(self, payload: dict) -> None:
        claims = payload.get("claims") or []
        lines = ["AI interpretation (citation-grounded)", "", str(payload.get("answer") or ""), "", "Claims"]
        for claim in claims:
            ids = ", ".join(str(value) for value in claim.get("support_ids") or [])
            lines.append(f"• {claim.get('text') or ''} [documents {ids}] confidence={float(claim.get('confidence') or 0):.2f}")
            if claim.get("uncertainty"):
                lines.append(f"  Uncertainty: {claim['uncertainty']}")
        self.research_detail_text.configure(state="normal")
        self.research_detail_text.delete("1.0", "end")
        self.research_detail_text.insert("1.0", "\n".join(lines))
        self.research_detail_text.configure(state="disabled")

    def open_selected_research_wayback(self) -> None:
        row = self.selected_research_result()
        if row:
            webbrowser.open(replay_url(str(row.get("timestamp") or ""), str(row.get("original_url") or "")))

    def copy_selected_research_url(self) -> None:
        row = self.selected_research_result()
        if row:
            self.clipboard_clear()
            self.clipboard_append(str(row.get("original_url") or ""))

    def current_ai_scan_id(self) -> int | None:
        return self.scan_run_map.get(self.ai_scan_var.get())

    def current_ai_run_id(self) -> int | None:
        return self.ai_run_map.get(self.ai_run_var.get())

    def start_ai_review_ui(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo(APP_NAME, "Another operation is already running. Let it finish or stop it first.")
            return
        try:
            ensure_frozen_bundle_available()
            config = self.build_config(require_keywords=False)
            scan_id = self.current_ai_scan_id()
            if not scan_id:
                raise ValueError("Choose a completed scan to review.")
            prompt = self.ai_prompt_text.get("1.0", "end").strip()
            if not prompt:
                raise ValueError("Describe what you are searching for before starting AI relevance review.")
            api_key = self.ai_api_key_var.get().strip()
            # resolve_api_key is intentionally called inside the worker so an
            # OPENAI_API_KEY environment variable can be used without exposing it
            # to the Tk state or project configuration.
        except (ValueError, FrozenBundleError) as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self.stop_event.clear()
        self.progress_var.set(0)
        self.status_var.set("Starting AI relevance review…")
        self.start_button.configure(state="disabled")
        self.ai_start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.log(f"Starting AI relevance review for scan {scan_id}")
        self.worker_thread = threading.Thread(
            target=self.run_ai_worker,
            args=(config, int(scan_id), prompt, api_key),
            daemon=True,
        )
        self.worker_thread.start()

    def run_ai_worker(self, config: ProjectConfig, scan_id: int, prompt: str, api_key: str) -> None:
        database = None
        try:
            database = open_database(config.output_dir, migrate=True)
            ai_run_id = run_ai_review(
                config, database, scan_id, prompt, api_key, self.stop_event, self.on_engine_event
            )
            paths = generate_ai_reports(
                config.output_dir, database, ai_run_id, config.ai.normalized().minimum_relevance
            )
            self.events.put(("ai_complete", {"run_id": ai_run_id, "paths": paths}))
        except Stopped:
            self.events.put(("stopped", None))
        except (AIReviewError, FrozenBundleError) as exc:
            self.events.put(("error", str(exc)))
        except Exception:
            self.events.put(("error", traceback.format_exc()))
        finally:
            if database is not None:
                database.close()

    def refresh_ai_runs(self, database=None, select_run_id: int | None = None) -> None:
        root = Path(self.output_var.get()).expanduser()
        if not (root / "archive_scout.sqlite3").exists():
            return
        own = database is None
        database = database or self.project_database()
        try:
            scan_id = self.current_ai_scan_id()
            rows = list_ai_runs(database, scan_id) if scan_id else list_ai_runs(database)
            self.ai_run_map = {}
            selected_label = ""
            values: list[str] = []
            for row in rows:
                prompt = str(row["prompt"] or "").replace("\n", " ").strip()
                if len(prompt) > 54:
                    prompt = prompt[:51] + "…"
                label = f"{row['id']} — {row['status']} — {row['model']} — {prompt}"
                values.append(label)
                self.ai_run_map[label] = int(row["id"])
                if select_run_id is not None and int(row["id"]) == int(select_run_id):
                    selected_label = label
            self.ai_run_box.configure(values=values)
            if selected_label:
                self.ai_run_var.set(selected_label)
            elif values and self.ai_run_var.get() not in self.ai_run_map:
                self.ai_run_var.set(values[0])
            elif not values:
                self.ai_run_var.set("")
            self.refresh_ai_results(database)
        finally:
            if own:
                database.close()

    def refresh_ai_results(self, database=None) -> None:
        if not hasattr(self, "ai_results_tree"):
            return
        run_id = self.current_ai_run_id()
        self.ai_results_tree.delete(*self.ai_results_tree.get_children())
        self.ai_result_row_map.clear()
        if not run_id:
            return
        own = database is None
        database = database or self.project_database()
        try:
            minimum = int(self.ai_min_relevance_var.get() or 0)
            for row in ai_result_rows(database, run_id, minimum, limit=2000):
                values = (
                    int(row["relevance_score"]),
                    f"{float(row['confidence']):.2f}",
                    int(row["archive_score"]),
                    row["timestamp"],
                    row["title"] or "(untitled)",
                    row["original_url"],
                    row["category"] or "",
                )
                item = self.ai_results_tree.insert("", "end", values=values)
                self.ai_result_row_map[item] = dict(row)
        except Exception as exc:
            self.log(f"Could not load AI relevance results: {exc}")
        finally:
            if own:
                database.close()

    def selected_ai_result(self) -> dict | None:
        selected = self.ai_results_tree.selection() if hasattr(self, "ai_results_tree") else ()
        return self.ai_result_row_map.get(selected[0]) if selected else None

    def load_selected_ai_result(self, _event=None) -> None:
        row = self.selected_ai_result()
        if not row:
            return
        detail = (
            f"Relevance: {int(row.get('relevance_score') or 0)}/100\n"
            f"Confidence: {float(row.get('confidence') or 0):.2f}\n"
            f"Archive Scout score: {int(row.get('archive_score') or 0)}\n"
            f"Category: {row.get('category') or '—'}\n\n"
            f"Reason\n{row.get('reason') or ''}\n\n"
            f"Evidence summary\n{row.get('evidence') or ''}"
        )
        self.ai_detail_text.configure(state="normal")
        self.ai_detail_text.delete("1.0", "end")
        self.ai_detail_text.insert("1.0", detail)
        self.ai_detail_text.configure(state="disabled")

    def open_selected_ai_local(self) -> None:
        row = self.selected_ai_result()
        if row and row.get("path"):
            path = Path(row["path"])
            if path.exists():
                if sys.platform == "darwin":
                    subprocess.Popen(["open", str(path)])
                elif os.name == "nt":
                    os.startfile(str(path))
                else:
                    subprocess.Popen(["xdg-open", str(path)])

    def open_selected_ai_wayback(self) -> None:
        row = self.selected_ai_result()
        if row:
            webbrowser.open(replay_url(str(row["timestamp"]), str(row["original_url"])))

    def copy_selected_ai_url(self) -> None:
        row = self.selected_ai_result()
        if row:
            self.clipboard_clear()
            self.clipboard_append(str(row["original_url"]))

    def refresh_history(self) -> None:
        root = Path(self.output_var.get()).expanduser()
        if not (root / "archive_scout.sqlite3").exists():
            return
        database = self.project_database()
        try:
            self.history_tree.delete(*self.history_tree.get_children())
            for row in list_scan_runs(database):
                self.history_tree.insert("", "end", iid=str(row["id"]), values=(row["id"], row["keyword_set_name"], row["status"], row["started_at"], row["document_count"], row["match_count"], f"{row['duration_seconds']:.1f}", row["source_operation"]))
            self.refresh_scan_choices(database)
        finally:
            database.close()

    def selected_history_id(self) -> int | None:
        selected = self.history_tree.selection()
        return int(selected[0]) if selected else None

    def compare_selected_scans(self) -> None:
        selected = [int(value) for value in self.history_tree.selection()]
        if len(selected) != 2:
            messagebox.showinfo(APP_NAME, "Select exactly two scan runs to compare.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            initialfile=f"scan-{selected[0]}-vs-{selected[1]}.txt",
        )
        if not path:
            return
        database = self.project_database()
        try:
            generate_scan_comparison(database, selected[0], selected[1], Path(path))
        finally:
            database.close()

    def rename_selected_scan(self) -> None:
        scan_id = self.selected_history_id()
        if not scan_id:
            return
        name = simpledialog.askstring(APP_NAME, "New scan name:")
        if name:
            database = self.project_database()
            with database:
                rename_scan_run(database, scan_id, name)
            database.close()
            self.refresh_history()

    def regenerate_selected_scan(self) -> None:
        scan_id = self.selected_history_id()
        if not scan_id:
            return
        database = self.project_database()
        try:
            generate_reports(self.build_config(require_keywords=False), database, scan_id)
        finally:
            database.close()

    def delete_selected_scan(self) -> None:
        scan_id = self.selected_history_id()
        if not scan_id or not messagebox.askyesno(APP_NAME, "Delete this scan's matches, reviews, notes, and tags? Downloaded files will remain."):
            return
        database = self.project_database()
        with database:
            delete_scan_run(database, scan_id)
        database.close()
        self.refresh_history()

    def refresh_errors(self) -> None:
        root = Path(self.output_var.get()).expanduser()
        if not (root / "archive_scout.sqlite3").exists():
            return
        database = self.project_database()
        try:
            categories = list_error_categories(database)
            self.error_category_box.configure(values=("All", *categories))
            selected_category = self.error_category_var.get()
            rows = list_errors(
                database,
                category="" if selected_category == "All" else selected_category,
                limit=2000,
            )
            if len(rows) == 2000:
                self.log("The Activity error table is showing the newest 2,000 matching errors.")
            self.errors_tree.delete(*self.errors_tree.get_children())
            self.error_row_map.clear()
            for row in rows:
                url = row["original_url"] or row["media_url"] or row["path"] or row["media_path"] or ""
                item = self.errors_tree.insert("", "end", values=(row["operation"], row["category"], row["attempt_count"], bool(row["retryable"]), row["last_seen"], url, row["message"]))
                self.error_row_map[item] = dict(row)
            self.refresh_site_issues(database)
        finally:
            database.close()

    def refresh_site_issues(self, database=None) -> None:
        if not hasattr(self, "site_issues_tree"):
            return
        root = Path(self.output_var.get()).expanduser()
        if not (root / "archive_scout.sqlite3").exists():
            return
        own = database is None
        database = database or self.project_database()
        try:
            self.site_issues_tree.delete(*self.site_issues_tree.get_children())
            self.site_issue_row_map.clear()
            for row in list_site_issues(database, unresolved_only=True, limit=1000):
                status = int(row["http_status"] or 0)
                item = self.site_issues_tree.insert(
                    "", "end",
                    values=(
                        row["host"], row["stage"], row["category"], status or "",
                        int(row["occurrence_count"] or 0), row["last_seen"], row["message"],
                    ),
                )
                self.site_issue_row_map[item] = dict(row)
        finally:
            if own:
                database.close()

    def resolve_selected_site_issues(self) -> None:
        ids = [
            int(self.site_issue_row_map[item]["id"])
            for item in self.site_issues_tree.selection()
            if item in self.site_issue_row_map
        ]
        if not ids:
            return
        database = self.project_database()
        try:
            with database:
                for issue_id in ids:
                    resolve_site_issue(database, issue_id, True)
        finally:
            database.close()
        self.refresh_site_issues()

    def retry_selected_errors(self) -> None:
        selected = [self.error_row_map[item] for item in self.errors_tree.selection() if item in self.error_row_map]
        capture_ids = sorted({int(row["capture_id"]) for row in selected if row.get("capture_id")})
        media_ids = sorted({int(row["media_capture_id"]) for row in selected if row.get("media_capture_id")})
        if not capture_ids and not media_ids:
            messagebox.showinfo(APP_NAME, "Select one or more retryable text-page or media errors.")
            return
        config = self.build_config(require_keywords=bool(capture_ids))
        config.retry_capture_ids = capture_ids
        config.retry_media_capture_ids = media_ids
        if capture_ids:
            self.start(config.normalized(), "retry_errors")
        else:
            self.start(config.normalized(), "media_retry")

    def ignore_selected_errors(self) -> None:
        ids = [int(self.error_row_map[item]["id"]) for item in self.errors_tree.selection() if item in self.error_row_map]
        if not ids:
            return
        database = self.project_database()
        with database:
            ignore_errors(database, ids, True)
        database.close()
        self.refresh_errors()

    def save_project(self) -> None:
        try:
            path = save_project_config(self.build_config(require_keywords=False))
            messagebox.showinfo(APP_NAME, f"Project saved to:\n{path}")
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def load_project(self) -> None:
        selected = filedialog.askopenfilename(title="Load Archive Scout project", filetypes=[("Archive Scout project", "project.json"), ("JSON files", "*.json"), ("All files", "*")])
        if not selected:
            return
        try:
            self.apply_config(load_project_config(Path(selected)))
            self.refresh_history()
            self.refresh_results()
            self.refresh_ai_runs()
            self.refresh_errors()
            self.refresh_site_issues()
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not load project:\n{exc}")

    def apply_config(self, config: ProjectConfig) -> None:
        self.output_var.set(str(config.output_dir))
        self.replace_text(self.targets_text, config.targets)
        self.keyword_sets = [item.to_payload() for item in config.normalized_keyword_sets()]
        self.refresh_keyword_set_box(0)
        self.from_date_var.set(config.from_date)
        self.to_date_var.set(config.to_date)
        self.replace_text(self.cdx_filters_text, config.cdx_filters)
        self.replace_text(self.cdx_extra_text, config.cdx_extra_params)
        self.collapse_urlkey_var.set("urlkey" in config.cdx_collapses)
        self.collapse_digest_var.set("digest" in config.cdx_collapses)
        self.cdx_match_type_var.set(config.cdx_match_type or "Automatic")
        self.page_size_var.set(str(config.page_size))
        self.workers_var.set(str(config.workers))
        self.scan_workers_var.set(str(config.scan_workers))
        self.max_file_var.set(str(config.max_file_mb))
        self.minimum_score_var.set(str(config.minimum_score))
        report = config.report.normalized()
        self.report_retain_var.set(report.retain_scan_details)
        self.report_sort_var.set(report.sort_order)
        self.report_max_var.set(str(report.max_matches))
        self.report_snippets_var.set(str(report.snippet_limit))
        self.report_chars_var.set(str(report.snippet_chars))
        self.report_links_var.set(str(report.link_limit))
        for name, variable in self.report_output_vars.items():
            variable.set(report.output_enabled(name))
        for name, variables in self.report_field_vars.items():
            selected = set(report.fields_for(name))
            for field, variable in variables.items():
                variable.set(field in selected)
        self.cdx_delay_var.set(str(config.cdx_delay))
        self.download_delay_var.set(str(config.download_delay))
        self.rate_limit_base_var.set(str(config.rate_limit_base_pause))
        self.rate_limit_max_var.set(str(config.rate_limit_max_pause))
        self.rate_limit_wait_var.set(str(config.rate_limit_max_wait / 60.0))
        network = config.network.normalized()
        self.network_backend_var.set(network.backend)
        self.network_endpoint_var.set(network.endpoint_mode)
        self.network_strategy_var.set(network.index_strategy)
        self.network_page_blocks_var.set(str(network.page_blocks))
        self.network_cdx_workers_var.set(str(network.cdx_workers))
        self.network_trust_env_var.set(network.trust_environment)
        self.network_persistent_var.set(network.persistent_retries)
        self.network_retry_base_var.set(str(network.retry_base_seconds))
        self.network_retry_max_var.set(str(network.retry_max_seconds))
        self.network_failure_limit_var.set(str(network.failure_pause_threshold))
        self.target_settings = dict(config.target_settings)
        self.auto_backup_var.set(config.auto_backup)
        self.backup_keep_var.set(str(config.backup_keep))
        self.backup_max_var.set(str(config.backup_max_mb))
        if hasattr(self, "hitlist_text"):
            self.replace_text(self.hitlist_text, config.hitlist_keywords)
        self.hitlist_file_var.set(config.hitlist_file)
        self.import_source_var.set(config.import_source)
        for label, value in SCOPE_LABELS.items():
            if value == config.download_scope:
                self.scope_var.set(label)
                break
        media = config.media
        self.media_enabled_var.set(media.enabled)
        self.replace_text(self.media_targets_text, media.targets)
        self.media_images_var.set(media.include_images)
        self.media_videos_var.set(media.include_videos)
        self.replace_text(self.media_include_text, media.include_extensions)
        self.replace_text(self.media_exclude_text, media.exclude_extensions)
        self.media_embedded_var.set(media.discover_embedded)
        self.media_external_var.set(media.allow_external_embeds)
        self.media_strategy_var.set(media.snapshot_strategy)
        self.media_max_var.set(str(media.max_file_mb))
        self.media_preserve_var.set(False)
        ai = config.ai.normalized()
        self.ai_provider_var.set(ai.provider)
        self.ai_model_var.set(ai.model)
        self.ai_candidate_limit_var.set(str(ai.candidate_limit))
        self.ai_batch_size_var.set(str(ai.batch_size))
        self.ai_min_relevance_var.set(str(ai.minimum_relevance))
        self.ai_excerpt_chars_var.set(str(ai.excerpt_chars))
        research = config.research.normalized()
        self.research_backend_var.set(research.vector_backend)
        self.research_auto_var.set(research.auto_build)
        self.research_limit_var.set(str(research.result_limit))
        analysis = config.analysis
        self.forum_profile_var.set(analysis.forum_profile)
        self.analysis_threads_var.set(analysis.reconstruct_threads)
        self.analysis_embeds_var.set(analysis.extract_legacy_embeds)
        self.replace_text(self.analysis_extractors_text, analysis.extractor_rules)
        self.analysis_external_var.set(analysis.search_external_assets)
        self.replace_text(self.analysis_domains_text, analysis.external_domains)
        self.analysis_external_limit_var.set(str(analysis.external_asset_limit))
        self.analysis_duplicate_var.set(str(analysis.duplicate_threshold))
        self.analysis_compare_var.set(analysis.compare_snapshots)
        self.analysis_provenance_var.set(analysis.build_provenance)
        self.analysis_merge_source_var.set(analysis.merge_source)

    def state_path(self) -> Path:
        return app_support_dir() / "settings.json"

    def save_app_state(self) -> None:
        try:
            config = self.build_config(require_keywords=False)
            path = self.state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = config.to_payload()
            payload["mode"] = self.mode_var.get()
            payload["appearance"] = {
                "theme": self.theme_var.get(),
                "interface_mode": self.interface_mode_var.get(),
                "font_scale": self.font_scale_var.get(),
                "geometry": self.geometry(),
            }
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except Exception:
            pass

    def load_app_state(self) -> None:
        path = self.state_path()
        if not path.exists():
            return
        try:
            config = load_project_config(path)
            self.apply_config(config)
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("mode") in MODE_LABELS:
                self.mode_var.set(payload["mode"])
            appearance = payload.get("appearance") or {}
            if appearance.get("theme") in {"System", "Light", "Dark"}:
                self.theme_var.set(appearance["theme"])
            if appearance.get("interface_mode") in {"Simple", "Advanced"}:
                self.interface_mode_var.set(appearance["interface_mode"])
            if appearance.get("font_scale"):
                self.font_scale_var.set(str(appearance["font_scale"]))
            if appearance.get("geometry"):
                try:
                    self.geometry(str(appearance["geometry"]))
                except tk.TclError:
                    pass
            self.update_operation_help()
            self.apply_interface_theme()
            self.refresh_navigation()
        except Exception:
            pass

    def _finish_close(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            self.status_var.set("Pausing and flushing project state before close…")
            self.after(100, self._finish_close)
            return
        if self.dashboard_refresh_job is not None:
            try:
                self.after_cancel(self.dashboard_refresh_job)
            except tk.TclError:
                pass
            self.dashboard_refresh_job = None
        self.save_app_state()
        self.destroy()

    def on_close(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            if self.closing_after_stop:
                return
            if not messagebox.askyesno(APP_NAME, "A run is active. Pause it, save all durable progress, and close when the checkpoint finishes?"):
                return
            self.closing_after_stop = True
            self.stop_event.set()
            self.status_var.set("Pausing and saving before close…")
            self.after(100, self._finish_close)
            return
        self._finish_close()


def main() -> None:
    app = ArchiveScoutApp()
    if os.environ.get("ARCHIVE_SCOUT_STARTUP_PROBE") == "1":
        app.after(250, app.destroy)
    app.mainloop()


if __name__ == "__main__":
    main()
