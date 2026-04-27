"""Shared pytest fixtures for replay-mode tests.

Tests subprocess-launch detect.py with --replay so each test gets a clean
process with no shared global state. Each test gets its own temp DB and
JSON output path via pytest's tmp_path.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
DETECT_PY = REPO_ROOT / "detect.py"
CORPUS_DIR = Path(__file__).parent / "corpus"


def _bootstrap_test_db(
    db_path: Path,
    forbidden_polygon: Optional[list] = None,
) -> None:
    """Create the SQLite schema detect.py expects and (optionally) insert a
    forbidden-zone polygon.

    Mirrors the schema in dashboard_server.init_db() and detect._audit_init().
    Kept independent so the tests don't depend on the dashboard process.
    """
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS events (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            ts     REAL    NOT NULL,
            camera TEXT    NOT NULL DEFAULT 'unknown',
            kind   TEXT    NOT NULL DEFAULT 'unknown',
            detail TEXT    NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS forbidden_zones (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT    NOT NULL,
            polygon    TEXT    NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        """
    )
    if forbidden_polygon is not None:
        conn.execute(
            "INSERT INTO forbidden_zones (name, polygon, created_at, updated_at) "
            "VALUES (?, ?, 0, 0)",
            ("Test Zone", json.dumps(forbidden_polygon)),
        )
    conn.commit()
    conn.close()


def _run_replay(
    clip_path: Path,
    db_path: Path,
    out_json: Path,
    mode: str = "OCCUPIED",
    timeout_s: int = 180,
) -> subprocess.CompletedProcess:
    """Subprocess-launch detect.py in replay mode.

    Uses DB_PATH env var to scope the audit DB to this test only.
    """
    env = {**os.environ, "DB_PATH": str(db_path)}
    return subprocess.run(
        [
            sys.executable, str(DETECT_PY),
            "--replay", str(clip_path),
            "--emit-json", str(out_json),
            "--mode", mode,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        cwd=str(REPO_ROOT),
    )


@pytest.fixture
def replay_runner(tmp_path):
    """Returns a callable: (clip, polygon=None, mode='OCCUPIED') -> decisions list.

    polygon: if provided, inserted into the test forbidden_zones table.
    Returns the parsed JSON list of decisions emitted during replay.
    """

    def run(
        clip: Path,
        polygon: Optional[list] = None,
        mode: str = "OCCUPIED",
    ) -> list:
        db = tmp_path / "test.db"
        out = tmp_path / "decisions.json"
        _bootstrap_test_db(db, forbidden_polygon=polygon)
        result = _run_replay(clip, db, out, mode=mode)
        if result.returncode != 0:
            pytest.fail(
                f"detect.py replay exited {result.returncode}\n"
                f"--- stdout ---\n{result.stdout[-2000:]}\n"
                f"--- stderr ---\n{result.stderr[-2000:]}"
            )
        if not out.exists():
            pytest.fail(
                f"emit-json output missing: {out}\n"
                f"--- stderr ---\n{result.stderr[-2000:]}"
            )
        return json.loads(out.read_text())

    return run
