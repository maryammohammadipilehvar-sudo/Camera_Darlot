"""Restore the recovered forbidden-zone polygon into a fresh DB.

Run this AFTER the broken DB has been moved aside and the services
restarted (so init_db has created the schema). Idempotent: skips
insertion if a row with the same name already exists.

Usage (from the repo root, with .venv active):
    python3 tools/restore_polygon.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
DB_PATH = REPO / "sentinel_events.db"
POLY_PATH = REPO / "RECOVERED_polygon.json"


def main() -> int:
    if not POLY_PATH.exists():
        print(f"ERROR: {POLY_PATH} not found — nothing to restore")
        return 1
    if not DB_PATH.exists():
        print(
            f"ERROR: {DB_PATH} not found — start security_pipeline + "
            "sentinel_dashboard first to recreate the schema"
        )
        return 1

    payload = json.loads(POLY_PATH.read_text())
    name = payload["name"]
    poly = payload["polygon"]
    poly_json = json.dumps(poly)
    now = int(time.time())

    conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
    try:
        existing = conn.execute(
            "SELECT id, polygon FROM forbidden_zones WHERE name = ?", (name,),
        ).fetchone()
        if existing is not None:
            print(f"already present (id={existing[0]}): {name} — skipping")
            return 0
        cur = conn.execute(
            "INSERT INTO forbidden_zones (name, polygon, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (name, poly_json, now, now),
        )
        conn.commit()
        print(f"inserted forbidden_zone id={cur.lastrowid}: {name} ({len(poly)} pts)")
        return 0
    except sqlite3.OperationalError as e:
        print(f"ERROR: schema not ready ({e}). Start services first.")
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
