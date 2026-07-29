from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import traceback
import urllib.parse
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .defaults import PRESETS
from .engine import (
    CDX_URL,
    ProjectConfig,
    ProgressEvent,
    RateLimited,
    Stopped,
    VERSION,
    build_cdx_params,
    cdx_year_window,
    load_project_config,
    run_project,
    save_project_config,
)

APP_NAME = "Archive Scout"
MODE_LABELS = {
    "Index, download, scan, and report": "all",
    "Index URLs only": "index",
    "Download and scan existing index": "download",
    "Rescan saved pages with current keywords": "rescan",
    "Regenerate reports only": "report",
}
SCOPE_LABELS = {
    "All archived text pages (thorough)": "all_text",
    "Only URLs containing a keyword (fast)": "keyword_urls",
    "Index only; download nothing": "index_only",
}


def app_support_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path.home() / f".{APP_NAME.lower().replace(' ', '_')}"


def open_path(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif os.name == "nt":
        os.startfile(path)
    else:
        subprocess.Popen(["xdg-open", str(path)])


class ArchiveScoutApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} {VERSION}")
        self.geometry("980x760")
        self.minsize(820, 650)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self.last_paths: dict[str, Path] = {}
        self.create_variables()
        self.create_ui()
        self.load_app_state()
        self.after(100, self.process_events)

    def create_variables(self) -> None:
        default_output = Path.home() / "Downloads" / "ArchiveScout"
        cpu_count = os.cpu_count() or 4
        self.output_var = tk.StringVar(value=str(default_output))
        self.preset_var = tk.StringVar(value="Ogrish 9/11 research")
        self.mode_var = tk.StringVar(value="Index, download, scan, and report")
        self.scope_var = tk.StringVar(value="All archived text pages (thorough)")
        self.from_date_var = tk.StringVar(value="2001")
        self.to_date_var = tk.StringVar(value="2010")
        self.cdx_match_type_var = tk.StringVar(value="Automatic")
        self.collapse_urlkey_var = tk.BooleanVar(value=True)
        self.collapse_digest_var = tk.BooleanVar(value=False)
        self.page_size_var = tk.StringVar(value="5000")
        self.workers_var = tk.StringVar(value=str(min(6, max(2, cpu_count))))
        self.max_file_var = tk.StringVar(value="25")
        self.minimum_score_var = tk.StringVar(value="1")
        self.cdx_delay_var = tk.StringVar(value="0.8")
        self.download_delay_var = tk.StringVar(value="0.25")
        self.status_var = tk.StringVar(value="Ready")
        self.progress_var = tk.DoubleVar(value=0)

    def create_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        header = ttk.Frame(self, padding=(14, 12, 14, 6))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text=APP_NAME, font=("TkDefaultFont", 20, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="Download public Wayback Machine captures and scan them for your own keywords.",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 0))
        ttk.Label(header, text="Preset:").grid(row=0, column=1, sticky="e", padx=(20, 6))
        preset = ttk.Combobox(header, textvariable=self.preset_var, values=list(PRESETS), state="readonly", width=24)
        preset.grid(row=0, column=2, sticky="e")
        preset.bind("<<ComboboxSelected>>", lambda event: self.apply_preset())

        project = ttk.LabelFrame(self, text="Project", padding=10)
        project.grid(row=1, column=0, sticky="ew", padx=14, pady=6)
        project.columnconfigure(1, weight=1)
        ttk.Label(project, text="Output folder:").grid(row=0, column=0, sticky="w")
        ttk.Entry(project, textvariable=self.output_var).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(project, text="Browse…", command=self.choose_output).grid(row=0, column=2)
        ttk.Button(project, text="Open", command=self.open_output).grid(row=0, column=3, padx=(6, 0))
        ttk.Label(project, text="Operation:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(project, textvariable=self.mode_var, values=list(MODE_LABELS), state="readonly").grid(
            row=1, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(8, 0)
        )

        notebook = ttk.Notebook(self)
        notebook.grid(row=2, column=0, sticky="nsew", padx=14, pady=6)

        targets_tab = ttk.Frame(notebook, padding=10)
        targets_tab.columnconfigure(0, weight=1)
        targets_tab.rowconfigure(1, weight=1)
        notebook.add(targets_tab, text="Sites and paths")
        ttk.Label(
            targets_tab,
            text="One Wayback target per line. Examples: example.com/*, forum.example.com/*, example.com/path/*",
        ).grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.targets_text = tk.Text(targets_tab, wrap="none", undo=True, font=("Menlo", 12))
        self.targets_text.grid(row=1, column=0, sticky="nsew")
        targets_scroll = ttk.Scrollbar(targets_tab, orient="vertical", command=self.targets_text.yview)
        targets_scroll.grid(row=1, column=1, sticky="ns")
        self.targets_text.configure(yscrollcommand=targets_scroll.set)

        keywords_tab = ttk.Frame(notebook, padding=10)
        keywords_tab.columnconfigure(0, weight=1)
        keywords_tab.rowconfigure(1, weight=1)
        notebook.add(keywords_tab, text="Keywords")
        ttk.Label(
            keywords_tab,
            text="One case-insensitive phrase per line. Prefix a line with re: to use a regular expression.",
        ).grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.keywords_text = tk.Text(keywords_tab, wrap="none", undo=True, font=("Menlo", 12))
        self.keywords_text.grid(row=1, column=0, sticky="nsew")
        keywords_scroll = ttk.Scrollbar(keywords_tab, orient="vertical", command=self.keywords_text.yview)
        keywords_scroll.grid(row=1, column=1, sticky="ns")
        self.keywords_text.configure(yscrollcommand=keywords_scroll.set)

        cdx_tab = ttk.Frame(notebook, padding=12)
        cdx_tab.columnconfigure(1, weight=1)
        cdx_tab.rowconfigure(6, weight=1)
        notebook.add(cdx_tab, text="CDX options")
        ttk.Label(cdx_tab, text="Start date:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(cdx_tab, textvariable=self.from_date_var, width=22).grid(row=0, column=1, sticky="w", padx=(10, 0), pady=4)
        ttk.Label(cdx_tab, text="End date:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(cdx_tab, textvariable=self.to_date_var, width=22).grid(row=1, column=1, sticky="w", padx=(10, 0), pady=4)
        ttk.Label(
            cdx_tab,
            text="Accepted formats: YYYY, YYYYMM, YYYYMMDD, or YYYYMMDDhhmmss.",
        ).grid(row=2, column=1, sticky="w", padx=(10, 0))
        ttk.Label(cdx_tab, text="matchType:").grid(row=3, column=0, sticky="w", pady=(10, 4))
        ttk.Combobox(
            cdx_tab,
            textvariable=self.cdx_match_type_var,
            values=("Automatic", "exact", "prefix", "host", "domain"),
            state="readonly",
            width=19,
        ).grid(row=3, column=1, sticky="w", padx=(10, 0), pady=(10, 4))
        collapse_frame = ttk.Frame(cdx_tab)
        collapse_frame.grid(row=4, column=1, sticky="w", padx=(10, 0), pady=4)
        ttk.Checkbutton(collapse_frame, text="collapse=urlkey", variable=self.collapse_urlkey_var).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(collapse_frame, text="collapse=digest", variable=self.collapse_digest_var).grid(row=0, column=1, sticky="w", padx=(18, 0))
        ttk.Label(cdx_tab, text="Results per CDX page:").grid(row=5, column=0, sticky="w", pady=4)
        ttk.Entry(cdx_tab, textvariable=self.page_size_var, width=22).grid(row=5, column=1, sticky="w", padx=(10, 0), pady=4)

        options_frame = ttk.Frame(cdx_tab)
        options_frame.grid(row=6, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
        options_frame.columnconfigure(0, weight=1)
        options_frame.columnconfigure(1, weight=1)
        options_frame.rowconfigure(1, weight=1)
        ttk.Label(options_frame, text="Filters, one value per line").grid(row=0, column=0, sticky="w")
        ttk.Label(options_frame, text="Additional parameters, one key=value per line").grid(row=0, column=1, sticky="w", padx=(12, 0))
        self.cdx_filters_text = tk.Text(options_frame, height=8, wrap="none", undo=True, font=("Menlo", 11))
        self.cdx_filters_text.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        self.cdx_extra_text = tk.Text(options_frame, height=8, wrap="none", undo=True, font=("Menlo", 11))
        self.cdx_extra_text.grid(row=1, column=1, sticky="nsew", padx=(12, 0), pady=(4, 0))
        ttk.Label(
            cdx_tab,
            text="Examples: statuscode:200, mimetype:text/html, resolveRevisits=true, fastLatest=true. Values are entered unencoded.",
            wraplength=760,
        ).grid(row=7, column=0, columnspan=2, sticky="nw", pady=(10, 0))
        ttk.Button(cdx_tab, text="Preview CDX request", command=self.preview_cdx).grid(row=8, column=0, columnspan=2, sticky="w", pady=(10, 0))

        settings_tab = ttk.Frame(notebook, padding=14)
        settings_tab.columnconfigure(1, weight=1)
        notebook.add(settings_tab, text="Settings")
        rows = [
            ("Download workers", self.workers_var),
            ("Maximum page size (MB)", self.max_file_var),
            ("Minimum report score", self.minimum_score_var),
            ("CDX request delay (seconds)", self.cdx_delay_var),
            ("Download request delay (seconds)", self.download_delay_var),
        ]
        for row, (label, variable) in enumerate(rows):
            ttk.Label(settings_tab, text=label + ":").grid(row=row, column=0, sticky="w", pady=5)
            ttk.Entry(settings_tab, textvariable=variable, width=18).grid(row=row, column=1, sticky="w", padx=(12, 0), pady=5)
        ttk.Label(settings_tab, text="Download scope:").grid(row=len(rows), column=0, sticky="w", pady=5)
        ttk.Combobox(settings_tab, textvariable=self.scope_var, values=list(SCOPE_LABELS), state="readonly", width=42).grid(
            row=len(rows), column=1, sticky="w", padx=(12, 0), pady=5
        )
        ttk.Label(
            settings_tab,
            text="For a 2015 Intel Mac, 4–6 workers is usually a good balance. Higher values can trigger archive rate limits.",
            wraplength=700,
        ).grid(row=len(rows) + 1, column=0, columnspan=2, sticky="w", pady=(14, 0))

        log_tab = ttk.Frame(notebook, padding=10)
        log_tab.columnconfigure(0, weight=1)
        log_tab.rowconfigure(0, weight=1)
        notebook.add(log_tab, text="Activity")
        self.log_text = tk.Text(log_tab, wrap="word", state="disabled", font=("Menlo", 11))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_tab, orient="vertical", command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)

        footer = ttk.Frame(self, padding=(14, 8, 14, 14))
        footer.grid(row=3, column=0, sticky="ew")
        footer.columnconfigure(1, weight=1)
        self.start_button = ttk.Button(footer, text="Start", command=self.start)
        self.start_button.grid(row=0, column=0)
        self.stop_button = ttk.Button(footer, text="Stop", command=self.stop, state="disabled")
        self.stop_button.grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Button(footer, text="Save project", command=self.save_project).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(footer, text="Load project…", command=self.load_project).grid(row=0, column=3, padx=(8, 0))
        ttk.Button(footer, text="Open reports", command=self.open_reports).grid(row=0, column=4, padx=(8, 0))
        self.progress = ttk.Progressbar(footer, variable=self.progress_var, maximum=100)
        self.progress.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(10, 4))
        ttk.Label(footer, textvariable=self.status_var).grid(row=2, column=0, columnspan=5, sticky="w")

        self.apply_preset()

    def lines_from(self, widget: tk.Text) -> list[str]:
        return [line.strip() for line in widget.get("1.0", "end").splitlines() if line.strip()]

    def replace_text(self, widget: tk.Text, values: list[str]) -> None:
        widget.delete("1.0", "end")
        widget.insert("1.0", "\n".join(values))

    def apply_preset(self) -> None:
        preset = PRESETS[self.preset_var.get()]
        self.replace_text(self.targets_text, list(preset["targets"]))
        self.replace_text(self.keywords_text, list(preset["keywords"]))
        self.from_date_var.set(str(preset.get("from_date", preset["from_year"])))
        self.to_date_var.set(str(preset.get("to_date", preset["to_year"])))
        self.replace_text(self.cdx_filters_text, list(preset.get("cdx_filters", ["statuscode:200"])))
        self.replace_text(self.cdx_extra_text, list(preset.get("cdx_extra_params", [])))
        collapses = set(preset.get("cdx_collapses", ["urlkey"]))
        self.collapse_urlkey_var.set("urlkey" in collapses)
        self.collapse_digest_var.set("digest" in collapses)
        self.cdx_match_type_var.set(preset.get("cdx_match_type") or "Automatic")

    def preview_cdx(self) -> None:
        try:
            config = self.build_config(require_keywords=False)
            window = cdx_year_window(config, config.from_year)
            if window is None:
                raise ValueError("The selected date range does not contain an indexable year.")
            params = build_cdx_params(config, config.targets[0], window[0], window[1])
            url = CDX_URL + "?" + urllib.parse.urlencode(params, doseq=True)
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        dialog = tk.Toplevel(self)
        dialog.title("CDX request preview")
        dialog.geometry("860x420")
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(1, weight=1)
        ttk.Label(dialog, text="Preview for the first target and first year window:", padding=(10, 10, 10, 4)).grid(row=0, column=0, sticky="w")
        text = tk.Text(dialog, wrap="word", font=("Menlo", 11))
        text.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        text.insert("1.0", url)
        text.configure(state="disabled")

    def choose_output(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output_var.get() or str(Path.home()))
        if selected:
            self.output_var.set(selected)

    def open_output(self) -> None:
        open_path(Path(self.output_var.get()).expanduser())

    def open_reports(self) -> None:
        open_path(Path(self.output_var.get()).expanduser() / "reports")

    def build_config(self, require_keywords: bool = True) -> ProjectConfig:
        try:
            config = ProjectConfig(
                output_dir=Path(self.output_var.get()),
                targets=self.lines_from(self.targets_text),
                keywords=self.lines_from(self.keywords_text),
                from_year=int(str(self.from_date_var.get()).strip()[:4]),
                to_year=int(str(self.to_date_var.get()).strip()[:4]),
                from_date=self.from_date_var.get(),
                to_date=self.to_date_var.get(),
                cdx_filters=self.lines_from(self.cdx_filters_text),
                cdx_collapses=[
                    value
                    for value, enabled in (
                        ("urlkey", self.collapse_urlkey_var.get()),
                        ("digest", self.collapse_digest_var.get()),
                    )
                    if enabled
                ],
                cdx_match_type="" if self.cdx_match_type_var.get() == "Automatic" else self.cdx_match_type_var.get(),
                cdx_extra_params=self.lines_from(self.cdx_extra_text),
                page_size=int(self.page_size_var.get()),
                workers=int(self.workers_var.get()),
                download_scope=SCOPE_LABELS[self.scope_var.get()],
                minimum_score=int(self.minimum_score_var.get()),
                max_file_mb=float(self.max_file_var.get()),
                cdx_delay=float(self.cdx_delay_var.get()),
                download_delay=float(self.download_delay_var.get()),
            ).normalized()
        except (ValueError, KeyError) as exc:
            raise ValueError(f"Check the numeric settings and target lines: {exc}") from exc
        if config.from_date > config.to_date:
            raise ValueError("The start date cannot be later than the end date.")
        if not config.targets:
            raise ValueError("Add at least one site or path.")
        mode = MODE_LABELS[self.mode_var.get()]
        if require_keywords and not config.keywords and mode not in {"index", "report"}:
            raise ValueError("Add at least one keyword.")
        return config

    def start(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            return
        try:
            config = self.build_config()
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        mode = MODE_LABELS[self.mode_var.get()]
        self.stop_event.clear()
        self.progress_var.set(0)
        self.status_var.set("Starting…")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.log(f"Starting {mode} in {config.output_dir}")
        self.worker_thread = threading.Thread(target=self.run_worker, args=(config, mode), daemon=True)
        self.worker_thread.start()

    def run_worker(self, config: ProjectConfig, mode: str) -> None:
        try:
            paths = run_project(config, mode, self.stop_event, self.on_engine_event)
            self.events.put(("complete", paths))
        except Stopped:
            self.events.put(("stopped", None))
        except RateLimited as exc:
            self.events.put(("error", f"{exc}\n\nProgress is saved. Resume later with the same project."))
        except Exception:
            self.events.put(("error", traceback.format_exc()))

    def on_engine_event(self, event: ProgressEvent) -> None:
        self.events.put(("progress", event))

    def stop(self) -> None:
        self.stop_event.set()
        self.status_var.set("Stopping after the current request…")
        self.stop_button.configure(state="disabled")

    def process_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "progress":
                    event = payload
                    self.status_var.set(event.message)
                    self.log(event.message)
                    if event.current is not None and event.total:
                        self.progress.configure(mode="determinate")
                        self.progress_var.set(event.current / event.total * 100)
                    else:
                        self.progress.configure(mode="indeterminate")
                        if not self.progress.instate(["!disabled"]):
                            self.progress.start(12)
                elif kind == "complete":
                    self.last_paths = payload
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.progress_var.set(100)
                    self.status_var.set("Complete")
                    self.log("Complete. Reports are ready.")
                    self.finish_run()
                    messagebox.showinfo(APP_NAME, "The run is complete. Reports were written to the project folder.")
                elif kind == "stopped":
                    self.progress.stop()
                    self.status_var.set("Stopped. Progress was saved.")
                    self.log("Stopped. Run the same operation again to resume.")
                    self.finish_run()
                elif kind == "error":
                    self.progress.stop()
                    self.status_var.set("Error")
                    self.log(str(payload))
                    self.finish_run()
                    messagebox.showerror(APP_NAME, str(payload))
        except queue.Empty:
            pass
        self.after(100, self.process_events)

    def finish_run(self) -> None:
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.save_app_state()

    def log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{timestamp}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def save_project(self) -> None:
        try:
            path = save_project_config(self.build_config())
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self.log(f"Saved project: {path}")
        messagebox.showinfo(APP_NAME, f"Project saved to:\n{path}")

    def load_project(self) -> None:
        selected = filedialog.askopenfilename(
            title="Load Archive Scout project",
            filetypes=[("Archive Scout project", "project.json"), ("JSON files", "*.json"), ("All files", "*")],
        )
        if not selected:
            return
        try:
            config = load_project_config(Path(selected))
            self.apply_config(config)
            self.log(f"Loaded project: {selected}")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not load project:\n{exc}")

    def apply_config(self, config: ProjectConfig) -> None:
        self.output_var.set(str(config.output_dir))
        self.replace_text(self.targets_text, config.targets)
        self.replace_text(self.keywords_text, config.keywords)
        self.from_date_var.set(config.from_date or str(config.from_year))
        self.to_date_var.set(config.to_date or str(config.to_year))
        self.replace_text(self.cdx_filters_text, config.cdx_filters)
        self.replace_text(self.cdx_extra_text, config.cdx_extra_params)
        self.collapse_urlkey_var.set("urlkey" in config.cdx_collapses)
        self.collapse_digest_var.set("digest" in config.cdx_collapses)
        self.cdx_match_type_var.set(config.cdx_match_type or "Automatic")
        self.page_size_var.set(str(config.page_size))
        self.workers_var.set(str(config.workers))
        self.max_file_var.set(str(config.max_file_mb))
        self.minimum_score_var.set(str(config.minimum_score))
        self.cdx_delay_var.set(str(config.cdx_delay))
        self.download_delay_var.set(str(config.download_delay))
        for label, value in SCOPE_LABELS.items():
            if value == config.download_scope:
                self.scope_var.set(label)
                break

    def state_path(self) -> Path:
        return app_support_dir() / "settings.json"

    def save_app_state(self) -> None:
        try:
            config = self.build_config()
            path = self.state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "output_dir": str(config.output_dir),
                "targets": config.targets,
                "keywords": config.keywords,
                "from_year": config.from_year,
                "to_year": config.to_year,
                "from_date": config.from_date,
                "to_date": config.to_date,
                "cdx_filters": config.cdx_filters,
                "cdx_collapses": config.cdx_collapses,
                "cdx_match_type": config.cdx_match_type,
                "cdx_extra_params": config.cdx_extra_params,
                "page_size": config.page_size,
                "workers": config.workers,
                "download_scope": config.download_scope,
                "minimum_score": config.minimum_score,
                "max_file_mb": config.max_file_mb,
                "cdx_delay": config.cdx_delay,
                "download_delay": config.download_delay,
                "mode": self.mode_var.get(),
            }
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except Exception:
            pass

    def load_app_state(self) -> None:
        path = self.state_path()
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            config = ProjectConfig(
                output_dir=Path(payload.get("output_dir") or Path.home() / "Downloads" / "ArchiveScout"),
                targets=list(payload.get("targets") or []),
                keywords=list(payload.get("keywords") or []),
                from_year=int(payload.get("from_year", 2000)),
                to_year=int(payload.get("to_year", 2010)),
                from_date=str(payload.get("from_date") or payload.get("from_year", 2000)),
                to_date=str(payload.get("to_date") or payload.get("to_year", 2010)),
                cdx_filters=list(payload["cdx_filters"]) if "cdx_filters" in payload else ["statuscode:200"],
                cdx_collapses=list(payload["cdx_collapses"]) if "cdx_collapses" in payload else ["urlkey"],
                cdx_match_type=str(payload.get("cdx_match_type", "")),
                cdx_extra_params=list(payload.get("cdx_extra_params") or []),
                page_size=int(payload.get("page_size", 5000)),
                workers=int(payload.get("workers", 4)),
                download_scope=str(payload.get("download_scope", "all_text")),
                minimum_score=int(payload.get("minimum_score", 1)),
                max_file_mb=float(payload.get("max_file_mb", 25)),
                cdx_delay=float(payload.get("cdx_delay", 0.8)),
                download_delay=float(payload.get("download_delay", 0.25)),
            ).normalized()
            self.apply_config(config)
            if payload.get("mode") in MODE_LABELS:
                self.mode_var.set(payload["mode"])
        except Exception:
            pass

    def on_close(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            if not messagebox.askyesno(APP_NAME, "A run is active. Stop it and close the application?"):
                return
            self.stop_event.set()
        self.save_app_state()
        self.destroy()


def main() -> None:
    app = ArchiveScoutApp()
    app.mainloop()


if __name__ == "__main__":
    main()
