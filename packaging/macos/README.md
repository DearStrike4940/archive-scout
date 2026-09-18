The universal macOS application is built by `scripts/build_macos.sh` on an Intel GitHub-hosted macOS runner using a Universal2 Python installation.

The release is a symlink-preserving ZIP named:

```text
ArchiveScout-macOS-Universal.zip
```

The script uses `ditto` to preserve bundle metadata and symbolic links. It extracts the completed ZIP into a clean temporary directory and verifies `base_library.zip`, all bundle links, and the code signature before publishing the package.

Archive Scout 1.0.7.1 release ZIPs also contain `ArchiveScoutCLI`, a universal console executable for bot/automation workflows. Run `./ArchiveScoutCLI --help` from Terminal or another process runner; the GUI remains `Archive Scout.app`.
