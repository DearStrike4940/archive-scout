from __future__ import annotations

import argparse
import threading
from pathlib import Path

from .engine import ProgressEvent, load_project_config, run_project


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an Archive Scout project without the desktop interface.")
    parser.add_argument("project", type=Path, help="Path to project.json")
    parser.add_argument("--mode", choices=("all", "index", "download", "rescan", "report"), default="all")
    args = parser.parse_args()
    config = load_project_config(args.project)

    def show(event: ProgressEvent) -> None:
        print(event.message, flush=True)

    run_project(config, args.mode, threading.Event(), show)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
