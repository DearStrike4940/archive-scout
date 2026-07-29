# Create and publish the GitHub repository

This guide starts with the supplied `archive-scout-repository.zip` and ends with a public repository that automatically builds an Intel Mac DMG and attaches it to a GitHub Release.

## 1. Prepare the folder

1. Download and unzip the repository package.
2. Rename the extracted folder to `archive-scout`.
3. Open Terminal and enter the folder:

```bash
cd ~/Downloads/archive-scout
```

4. Replace the README download-link placeholder with your GitHub username:

```bash
python3 scripts/set_github_username.py YOUR_GITHUB_USERNAME
```

## 2. Create the empty repository on GitHub

1. Sign in to GitHub.
2. Select New repository.
3. Use the repository name `archive-scout`.
4. Suggested description:

```text
A resumable macOS interface for Wayback CDX searches, archived-page downloads, and custom keyword scanning.
```

5. Choose Public if you want anyone to download the DMG.
6. Do not add a README, `.gitignore`, or license because those files are already included.
7. Create the repository.

Suggested topics:

```text
wayback-machine
internet-archive
cdx-api
web-archiving
osint
research-tool
macos
python
tkinter
```

## 3. Push the supplied repository

Run these commands inside the extracted folder:

```bash
git init
git add .
git commit -m "Initial Archive Scout release"
git branch -M main
git remote add origin https://github.com/YOUR_GITHUB_USERNAME/archive-scout.git
git push -u origin main
```

Replace `YOUR_GITHUB_USERNAME` with your real username.

GitHub Desktop can also publish the folder: choose Add Existing Repository, select the folder, then Publish Repository.

## 4. Confirm automated tests

1. Open the Actions tab in the repository.
2. Open the Tests workflow.
3. Confirm the initial run passes.

The workflow compiles the source and runs the unit tests on every push to `main` and every pull request.

## 5. Allow release publishing

The build workflow requests `contents: write` so it can attach the DMG to a release. If repository or organization policy blocks that permission:

1. Open Settings.
2. Select Actions, then General.
3. Find Workflow permissions.
4. Select Read and write permissions.
5. Save.

Do not enable write tokens for untrusted fork pull requests.

## 6. Test the Mac build without publishing a release

1. Open Actions.
2. Select Build macOS Intel App.
3. Select Run workflow.
4. Run it from `main`.
5. When it finishes, download the `ArchiveScout-macOS-Intel` workflow artifact.

A manually dispatched run creates an artifact but does not create a public release.

## 7. Publish the first downloadable release

Create and push a version tag:

```bash
git tag v1.1.0
git push origin v1.1.0
```

The tag triggers the Mac workflow. It will:

1. use GitHub's Intel macOS runner,
2. install the pinned PyInstaller build dependency,
3. run the tests,
4. build `Archive Scout.app` in `--onedir` mode,
5. ad-hoc sign it,
6. place it in a drag-to-Applications DMG,
7. generate a SHA-256 checksum,
8. create a GitHub Release,
9. and upload both files.

The permanent download URL will be:

```text
https://github.com/YOUR_GITHUB_USERNAME/archive-scout/releases/latest/download/ArchiveScout-macOS-Intel.dmg
```

## 8. Update a release

Commit your changes, then create a new semantic version tag:

```bash
git add .
git commit -m "Describe the update"
git push
git tag v1.1.1
git push origin v1.1.1
```

Never reuse a published version tag. Create a new tag for every release.

## 9. First-launch expectations

The included workflow performs ad-hoc signing, not Apple Developer ID signing and notarization. Users may need to Control-click the app, choose Open, and confirm it once.

For a warning-free public distribution, obtain an Apple Developer account and add Developer ID signing and notarization. Keep Apple credentials only in encrypted GitHub Actions secrets; never commit them.

## Troubleshooting

### The release step says permission denied

Confirm the workflow has `permissions: contents: write` and that repository Actions settings permit write access.

### The build does not start after a tag

Confirm the tag was pushed:

```bash
git push origin --tags
```

### The direct download link returns 404

The asset name must remain exactly:

```text
ArchiveScout-macOS-Intel.dmg
```

Also confirm at least one tagged workflow has completed and published a release.

### The app opens on the build runner but not Monterey

Keep `MACOSX_DEPLOYMENT_TARGET` and `LSMinimumSystemVersion` at `12.0`. Do not build the Intel release on an Apple Silicon-only runner. For the strongest Monterey compatibility, run `scripts/build_macos.sh` directly on a Monterey Intel Mac and upload that DMG to the release, because deployment-target metadata alone cannot guarantee backward compatibility for every collected binary.
