from __future__ import annotations

import argparse
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("username")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    changed = []
    for relative in ("README.md", "docs/GITHUB_SETUP.md", "REPOSITORY_DETAILS.md"):
        path = root / relative
        text = path.read_text(encoding="utf-8")
        updated = text.replace("YOUR_GITHUB_USERNAME", args.username)
        if updated != text:
            path.write_text(updated, encoding="utf-8")
            changed.append(relative)
    print("Updated: " + ", ".join(changed) if changed else "No placeholders remained.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
