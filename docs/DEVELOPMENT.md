# Development

## Run locally

```bash
python3 run_app.py
```

## Run tests

```bash
python3 -m unittest discover -s tests -v
```

## Compile check

```bash
python3 -m compileall -q archive_scout run_app.py
```

## Build the Intel Mac release

Run this on an Intel Mac with macOS 12 or newer:

```bash
python3 -m pip install -r requirements-build.txt
bash scripts/build_macos.sh
```

The files appear in `release/`.

## Add a preset

Edit `archive_scout/defaults.py`. Each preset can define:

```text
targets
keywords
from_year
to_year
from_date
to_date
cdx_filters
cdx_collapses
cdx_match_type
cdx_extra_params
```

## Compatibility

The source targets Python 3.11 or newer. The DMG includes its own Python runtime and targets Intel macOS Monterey 12 or newer.
