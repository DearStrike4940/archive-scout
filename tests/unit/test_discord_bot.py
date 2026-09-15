import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from archive_scout.discord_bot import (
    JobManager,
    build_help_text,
    live_matches_text,
    parse_snowflakes,
    parse_targets,
    project_name_choices,
    report_name_choices,
    safe_project_dir,
    uploadable_report_path,
)


def test_parse_snowflakes() -> None:
    assert parse_snowflakes("123, 456") == frozenset({123, 456})
    assert parse_snowflakes("") == frozenset()
    with pytest.raises(ValueError):
        parse_snowflakes("123,nope")


def test_safe_project_dir_stays_inside_root(tmp_path: Path) -> None:
    assert safe_project_dir(tmp_path, "case-01") == tmp_path.resolve() / "case-01"
    for unsafe in ("../outside", "two/levels", "", "."):
        with pytest.raises(ValueError):
            safe_project_dir(tmp_path, unsafe)


def test_parse_multiple_targets() -> None:
    assert parse_targets("example.com/*", "www.example.com/*; forum.example.com/*\nexample.com/*") == [
        "example.com/*",
        "www.example.com/*",
        "forum.example.com/*",
    ]


def test_discord_autocomplete_lists_projects_and_reports(tmp_path: Path) -> None:
    for name in ("Beta", "alpha"):
        project = tmp_path / name
        reports = project / "reports"
        reports.mkdir(parents=True)
        (project / "project.json").write_text("{}", encoding="utf-8")
    (tmp_path / "alpha" / "reports" / "all_matches_ranked.md").write_text("markdown", encoding="utf-8")
    (tmp_path / "alpha" / "reports" / "all_matches_ranked.csv").write_text("csv", encoding="utf-8")
    (tmp_path / "alpha" / "reports" / "all_matches_ranked.txt").write_text("matches", encoding="utf-8")
    (tmp_path / "alpha" / "reports" / "summary.txt").write_text("summary", encoding="utf-8")

    assert project_name_choices(tmp_path) == ["alpha", "Beta"]
    assert project_name_choices(tmp_path, "BET") == ["Beta"]
    assert report_name_choices(tmp_path, "alpha")[:4] == [
        "all_matches_ranked.md",
        "all_matches_ranked.csv",
        "all_matches_ranked.txt",
        "summary.txt",
    ]
    assert report_name_choices(tmp_path, "../outside") == []


def test_oversized_text_report_is_zipped_for_discord(tmp_path: Path) -> None:
    report = tmp_path / "all_matches_ranked.txt"
    report.write_text("repeated result\n" * 1000, encoding="utf-8")

    upload = uploadable_report_path(report, 1000)

    assert upload.suffix == ".zip"
    assert upload.name == "all_matches_ranked.txt.zip"
    assert upload.stat().st_size <= 1000
    with __import__("zipfile").ZipFile(upload) as bundle:
        assert bundle.namelist() == [report.name]

    spreadsheet = tmp_path / "all_matches_ranked.csv"
    spreadsheet.write_text("value\n" * 1000, encoding="utf-8")
    spreadsheet_upload = uploadable_report_path(spreadsheet, 1000)
    assert spreadsheet_upload.name == "all_matches_ranked.csv.zip"
    assert spreadsheet_upload != upload
    with __import__("zipfile").ZipFile(spreadsheet_upload) as bundle:
        assert bundle.namelist() == [spreadsheet.name]


def test_help_text_explains_configured_concurrency() -> None:
    text = build_help_text(3)
    assert "3 separate projects at once" in text
    assert "mode:resume" in text
    assert "does not automatically repeat" in text
    assert len(text) <= 2000


def test_live_matches_reads_running_scan_without_writing(tmp_path: Path) -> None:
    project = tmp_path / "case-01"
    project.mkdir()
    database_path = project / "archive_scout.sqlite3"
    database = sqlite3.connect(database_path)
    database.executescript(
        """
        CREATE TABLE scan_runs(id INTEGER PRIMARY KEY,status TEXT,minimum_score INTEGER);
        CREATE TABLE captures(id INTEGER PRIMARY KEY,original_url TEXT,timestamp TEXT);
        CREATE TABLE documents(id INTEGER PRIMARY KEY,capture_id INTEGER,title TEXT);
        CREATE TABLE document_matches(
            id INTEGER PRIMARY KEY,scan_run_id INTEGER,document_id INTEGER,score INTEGER,
            hits_json TEXT,snippets_json TEXT,excluded INTEGER,required_missing INTEGER
        );
        INSERT INTO scan_runs VALUES(1,'running',1);
        INSERT INTO captures VALUES(1,'https://example.com/archive','20010911084600');
        INSERT INTO documents VALUES(1,1,'Example match');
        INSERT INTO document_matches VALUES(1,1,1,3,'{"needle":2}','[]',0,0);
        """
    )
    database.commit()
    before = database.total_changes

    text = live_matches_text(tmp_path, "case-01", 5)

    assert "1 qualifying match" in text
    assert "Example match" in text
    assert "needle x2" in text
    assert "https://example.com/archive" in text
    assert database.total_changes == before
    database.close()


def test_job_manager_allows_distinct_projects_up_to_limit(tmp_path: Path) -> None:
    async def scenario() -> None:
        for name in ("alpha", "beta", "gamma"):
            folder = tmp_path / name
            folder.mkdir()
            (folder / "project.json").write_text("{}", encoding="utf-8")

        bot = SimpleNamespace(
            settings=SimpleNamespace(projects_root=tmp_path, max_concurrent_jobs=2)
        )
        manager = JobManager(bot)
        release = asyncio.Event()

        async def hold_job(job, project_file) -> None:
            await release.wait()

        manager._run = hold_job
        first, _ = await manager.start("alpha", "all", 1, 10)
        second, _ = await manager.start("beta", "all", 1, 10)
        duplicate, duplicate_message = await manager.start("alpha", "resume", 1, 10)
        over_limit, limit_message = await manager.start("gamma", "all", 1, 10)

        assert first and second
        assert not duplicate and "already has" in duplicate_message
        assert not over_limit and "concurrency limit" in limit_message

        stopped, _ = await manager.stop("alpha")
        assert stopped
        assert manager.active["alpha"].stop_event.is_set()

        release.set()
        await asyncio.gather(*(job.task for job in manager.active.values() if job.task))

    asyncio.run(scenario())
