# Install Archive Scout on an Intel Mac

## Requirements

- Intel-based Mac
- macOS Monterey 12 or newer target; release maintainers should test the DMG on Monterey
- Internet connection
- Enough free disk space for the selected archive scope

The downloadable application includes Python and its interface libraries. Users do not need Homebrew, pip, or a separate Python installation.

## Install

1. Download `ArchiveScout-macOS-Intel.dmg` from the latest GitHub Release.
2. Open the DMG.
3. Drag Archive Scout into the Applications shortcut.
4. Eject the Archive Scout disk image.
5. Open Applications.
6. Control-click Archive Scout and select Open.
7. Confirm Open in the security dialog.

The Control-click step is generally needed only for the first launch of the community build because it is not Apple-notarized.

## Remove

1. Quit Archive Scout.
2. Move Archive Scout from Applications to Trash.
3. Optionally delete application settings:

```text
~/Library/Application Support/Archive Scout/settings.json
```

Project folders are stored wherever the user selected and are not removed automatically.

## Verify a download

The release includes:

```text
ArchiveScout-macOS-Intel.dmg.sha256
```

In Terminal, change to the download folder and run:

```bash
shasum -a 256 ArchiveScout-macOS-Intel.dmg
```

Compare the result with the checksum file attached to the same GitHub Release.
