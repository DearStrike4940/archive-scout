# Contributing

Bug reports, test cases, documentation improvements, and focused pull requests are welcome.

Before opening a pull request:

```bash
python3 -m compileall -q archive_scout run_app.py
python3 -m unittest discover -s tests -v
```

Keep network behavior resumable and rate-limited. New CDX parameters must not override the fixed fields required for parsing and resume support. Do not commit downloaded archive content, SQLite databases, DMGs, build folders, credentials, or private research data.

Use a separate branch, explain the behavior change, and include tests for engine changes.
