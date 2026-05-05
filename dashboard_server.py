#!/usr/bin/env python3
"""
Sentinel Dashboard Server v1.0
Bridges: MQTT security/alerts → SQLite → WebSocket → Browser

Port: 8888 (dashboard)
Proxies MJPEG stream from: 8080 → /proxy/stream

Usage:
    cd /home/quarero/Desktop/security_pipeline
    source .venv/bin/activate
    python dashboard_server.py
"""

import asyncio
import datetime
import json
import logging
import os
import socket
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import paho.mqtt.client as mqtt
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

# ── Config ────────────────────────────────────────────────────────────────────
MQTT_HOST   = os.getenv("MQTT_HOST",   "localhost")
MQTT_PORT   = int(os.getenv("MQTT_PORT",   "1883"))
MQTT_TOPIC  = os.getenv("MQTT_TOPIC",  "security/alerts")
STREAM_HOST = os.getenv("STREAM_HOST", "127.0.0.1")
STREAM_PORT = int(os.getenv("STREAM_PORT", "8080"))
DASH_PORT   = int(os.getenv("DASH_PORT",   "8888"))
DB_PATH     = os.getenv("DB_PATH", str(
    Path(__file__).parent / "sentinel_events.db"
))
CLIP_DIR = Path(os.path.expanduser(
    os.getenv("CLIP_DIR", "~/.local/share/darlot/clips")
))
SNAPSHOT_DIR = Path(os.path.expanduser(
    os.getenv("SNAPSHOT_DIR", "~/.local/share/darlot/snapshots")
))
STATIC_DIR  = Path(__file__).parent / "dashboard_static"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [sentinel] %(levelname)s %(message)s",
)
log = logging.getLogger("sentinel")

# ── Database ──────────────────────────────────────────────────────────────────
_db_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


_FZ_PHASE1_COLUMNS = (
    # (name, sql-fragment) — additive only; defaults preserve current behavior.
    ("dwell_s",          "REAL    NOT NULL DEFAULT 0.0"),
    ("severity_tier",    "TEXT    NOT NULL DEFAULT 'CRITICAL'"),
    ("time_window",      "TEXT    NOT NULL DEFAULT '*'"),
    ("authorized_roles", "TEXT    NOT NULL DEFAULT '[]'"),
    ("template_kind",    "TEXT    NOT NULL DEFAULT 'custom'"),
    # Phase 3: shadow mode. Epoch seconds; 0 = off, otherwise the zone
    # is in shadow mode until this timestamp (events fire to dashboard
    # + audit, never to Telegram, regardless of severity).
    ("shadow_until",     "INTEGER NOT NULL DEFAULT 0"),
)


def _migrate_forbidden_zones(c: sqlite3.Connection) -> None:
    """Idempotent additive migration for the Restricted Areas Phase 1 fields.

    Reads PRAGMA table_info before each ALTER so re-running on an already
    migrated DB is a no-op. Defaults are picked so existing rows behave
    exactly as they did pre-migration (dwell=0, severity=CRITICAL, always-on).
    """
    existing = {row["name"] for row in c.execute("PRAGMA table_info(forbidden_zones)")}
    for col, ddl in _FZ_PHASE1_COLUMNS:
        if col not in existing:
            c.execute(f"ALTER TABLE forbidden_zones ADD COLUMN {col} {ddl}")
            log.info(f"forbidden_zones: added column {col}")


def init_db():
    with _db_lock:
        c = _conn()
        c.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      REAL    NOT NULL,
                camera  TEXT    NOT NULL DEFAULT 'unknown',
                kind    TEXT    NOT NULL DEFAULT 'unknown',
                detail  TEXT    NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_ts   ON events(ts);
            CREATE INDEX IF NOT EXISTS idx_kind ON events(kind);
            CREATE INDEX IF NOT EXISTS idx_cam  ON events(camera);
            CREATE TABLE IF NOT EXISTS forbidden_zones (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL,
                polygon    TEXT    NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_fz_updated ON forbidden_zones(updated_at);
            CREATE TABLE IF NOT EXISTS event_actions (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id  INTEGER NOT NULL,
                action    TEXT    NOT NULL,
                actor     TEXT    NOT NULL DEFAULT 'operator',
                ts        INTEGER NOT NULL,
                note      TEXT    NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_ea_event   ON event_actions(event_id);
            CREATE INDEX IF NOT EXISTS idx_ea_actts   ON event_actions(action, ts);
            CREATE TABLE IF NOT EXISTS zone_rejections (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                zone_id         INTEGER NOT NULL,
                cx              INTEGER NOT NULL,
                cy              INTEGER NOT NULL,
                hour            INTEGER NOT NULL,
                weekday         INTEGER NOT NULL,
                source_event_id INTEGER NOT NULL,
                created_at      INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_zr_zone_hour ON zone_rejections(zone_id, hour);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_zr_source ON zone_rejections(source_event_id);
        """)
        _migrate_forbidden_zones(c)
        c.commit()
        c.close()
    log.info(f"DB ready: {DB_PATH}")


def db_insert(ts: float, camera: str, kind: str, detail: str) -> int:
    with _db_lock:
        c = _conn()
        cur = c.execute(
            "INSERT INTO events (ts,camera,kind,detail) VALUES(?,?,?,?)",
            (ts, camera, kind, detail),
        )
        row_id = cur.lastrowid
        c.commit()
        c.close()
    return row_id


def db_query(
    limit=100, offset=0,
    kind=None, camera=None,
    start=None, end=None,
):
    with _db_lock:
        c = _conn()
        q = "SELECT * FROM events WHERE 1=1"
        p: list = []
        tq = "SELECT COUNT(*) FROM events WHERE 1=1"
        tp: list = []

        if kind:
            q  += " AND kind=?";   p.append(kind)
            tq += " AND kind=?";   tp.append(kind)
        if camera:
            q  += " AND camera=?"; p.append(camera)
            tq += " AND camera=?"; tp.append(camera)
        if start is not None:
            q  += " AND ts>=?";    p.append(start)
            tq += " AND ts>=?";    tp.append(start)
        if end is not None:
            q  += " AND ts<=?";    p.append(end)
            tq += " AND ts<=?";    tp.append(end)

        q += " ORDER BY ts DESC LIMIT ? OFFSET ?"
        p += [limit, offset]

        rows  = [dict(r) for r in c.execute(q, p).fetchall()]
        total = c.execute(tq, tp).fetchone()[0]
        c.close()
    return rows, total


def db_stats() -> dict:
    now = time.time()
    today_ts = datetime.datetime.utcnow().replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()

    with _db_lock:
        c = _conn()
        total     = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        today     = c.execute("SELECT COUNT(*) FROM events WHERE ts>=?",
                               (today_ts,)).fetchone()[0]
        last_hour = c.execute("SELECT COUNT(*) FROM events WHERE ts>=?",
                               (now - 3600,)).fetchone()[0]

        by_kind: dict = {}
        for r in c.execute(
            "SELECT kind, COUNT(*) c FROM events GROUP BY kind ORDER BY c DESC"
        ):
            by_kind[r["kind"]] = r["c"]

        cameras = [r[0] for r in c.execute(
            "SELECT DISTINCT camera FROM events"
        )]

        hourly = []
        for i in range(24):
            t0 = now - (24 - i) * 3600
            t1 = t0 + 3600
            cnt = c.execute(
                "SELECT COUNT(*) FROM events WHERE ts>=? AND ts<?", (t0, t1)
            ).fetchone()[0]
            hourly.append(cnt)

        # False-alarm rate (Phase 2a). Joins event_actions on event_id
        # to attribute each false_alarm back to the camera that produced
        # the underlying event. 7-day window — matches the operator
        # spec target of "<2 false alarms per camera per week".
        seven_days_ago = now - 7 * 86400
        try:
            fp_rows = c.execute(
                "SELECT e.camera AS camera, COUNT(*) AS c "
                "FROM event_actions ea "
                "JOIN events e ON e.id = ea.event_id "
                "WHERE ea.action='false_alarm' AND ea.ts>=? "
                "GROUP BY e.camera",
                (seven_days_ago,),
            ).fetchall()
            fp_last_7d = {r["camera"]: r["c"] for r in fp_rows}
        except sqlite3.OperationalError:
            # event_actions table absent (pre-Phase-2a DB).
            fp_last_7d = {}

        c.close()

    fp_budget = {cam: (fp_last_7d.get(cam, 0) > 2) for cam in cameras}
    return {
        "total":      total,
        "today":      today,
        "last_hour":  last_hour,
        "by_kind":    by_kind,
        "cameras":    cameras,
        "hourly":     hourly,
        "fp_last_7d": fp_last_7d,
        "fp_over_budget": fp_budget,
    }

def format_event_for_ui(event: dict) -> dict:
    ts = float(event.get("ts", 0))
    dt = datetime.datetime.fromtimestamp(ts)
    display_time = dt.strftime("%d/%m/%y %H:%M:%S")
    short_time = dt.strftime("%H:%M:%S")

    detail = event.get("detail", {})
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except Exception:
            detail = {"raw": detail}

    kind = str(event.get("kind", "unknown")).lower()
    camera = str(event.get("camera", "unknown"))

    summary = "Unknown event"
    ui_kind = kind
    show_in_live_feed = True

    if kind == "detection_summary":
        summary = str(detail.get("label", "Detection summary"))
        ui_kind = "summary"

    elif kind == "detection":
        class_name = str(detail.get("class_name", "object")).replace("_", " ").title()
        summary = f"1 {class_name.lower()} detected"
        ui_kind = "detection"
        show_in_live_feed = False

    elif kind == "loitering":
        zone = detail.get("zone", "unknown")
        summary = f"Loitering detected in {zone}"

    elif kind == "audio":
        sound = detail.get("class", "unknown")
        summary = f"Audio alert: {sound}"

    elif kind == "anomaly":
        summary = "Anomaly detected"

    elif kind == "behavior":
        summary = str(
            detail.get("label")
            or f"Behavior: {detail.get('action', 'unknown')}"
        )

    elif kind == "forbidden_zone":
        zone = detail.get("zone") or detail.get("zone_name") or "zone"
        summary = f"Intrusion: {zone}"

    elif kind == "phone_use":
        tid = detail.get("track_id")
        if isinstance(tid, int) and tid >= 0:
            summary = f"Phone use — Person #{tid}"
        else:
            summary = "Phone use detected"

    elif kind == "dog":
        tid = detail.get("track_id")
        if isinstance(tid, int) and tid >= 0:
            summary = f"Dog detected — #{tid}"
        else:
            summary = "Dog detected"

    event["detail"] = detail
    event["display_time"] = display_time
    event["display_short_time"] = short_time
    event["display_summary"] = summary
    event["display_camera"] = camera
    event["display_kind"] = ui_kind
    event["show_in_live_feed"] = show_in_live_feed

    return event


# ── WebSocket manager ─────────────────────────────────────────────────────────
class _WSManager:
    def __init__(self):
        self._clients: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._clients.append(ws)
        log.info(f"WS +1 ({len(self._clients)} connected)")

    def disconnect(self, ws: WebSocket):
        if ws in self._clients:
            self._clients.remove(ws)
        log.info(f"WS -1 ({len(self._clients)} connected)")

    async def broadcast(self, data: dict):
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


_mgr       = _WSManager()
_loop: asyncio.AbstractEventLoop | None = None
_mqtt_ok   = False
_start_ts  = time.time()


# ── MQTT bridge ───────────────────────────────────────────────────────────────
def _on_connect(client, userdata, flags, rc):
    global _mqtt_ok
    _mqtt_ok = rc == 0
    if rc == 0:
        client.subscribe(MQTT_TOPIC)
        log.info(f"MQTT ✓  subscribed → {MQTT_TOPIC}")
    else:
        log.warning(f"MQTT connect failed rc={rc}")


def _on_disconnect(client, userdata, rc):
    global _mqtt_ok
    _mqtt_ok = False
    log.warning(f"MQTT disconnected rc={rc}")


def _on_message(client, userdata, msg):
    global _loop
    try:
        payload = json.loads(msg.payload.decode())
        ts     = float(payload.get("ts",     time.time()))
        camera = str(payload.get("camera",   "unknown"))
        kind   = str(payload.get("kind",     "unknown"))
        detail = json.dumps({
            k: v for k, v in payload.items()
            if k not in ("ts", "camera", "kind")
        })

        row_id = db_insert(ts, camera, kind, detail)

        event = {
            "type":   "event",
            "id":     row_id,
            "ts":     ts,
            "camera": camera,
            "kind":   kind,
            "detail": json.loads(detail),
        }

        event = format_event_for_ui(event)

        if _loop and not _loop.is_closed():
            asyncio.run_coroutine_threadsafe(_mgr.broadcast(event), _loop)

    except Exception as e:
        log.warning(f"MQTT parse error: {e}")


def _mqtt_thread():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
    client.on_connect    = _on_connect
    client.on_disconnect = _on_disconnect
    client.on_message    = _on_message
    while True:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, 60)
            client.loop_forever()
        except Exception as e:
            log.warning(f"MQTT error: {e} — retry in 5 s")
            time.sleep(5)


# ── FastAPI ───────────────────────────────────────────────────────────────────
@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _loop
    _loop = asyncio.get_event_loop()
    init_db()
    threading.Thread(target=_mqtt_thread, daemon=True, name="mqtt").start()
    log.info(f"Sentinel dashboard  →  http://0.0.0.0:{DASH_PORT}")
    yield


app = FastAPI(title="Sentinel Dashboard", lifespan=_lifespan)


# ── MJPEG proxy ───────────────────────────────────────────────────────────────
@app.get("/proxy/stream")
def proxy_stream():
    """
    Proxies the detect.py MJPEG stream so the browser only needs one port.
    Uses raw TCP (no extra deps) — fast enough for 480×270 @ 8 fps.
    """
    CHUNK = 65536

    def _gen():
        try:
            s = socket.create_connection((STREAM_HOST, STREAM_PORT), timeout=4)
            s.settimeout(10)
            req = (
                b"GET /stream HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Connection: close\r\n\r\n"
            )
            s.sendall(req)
            # drain HTTP response headers
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = s.recv(1024)
                if not chunk:
                    break
                buf += chunk
            _, _, rest = buf.partition(b"\r\n\r\n")
            if rest:
                yield rest
            while True:
                chunk = s.recv(CHUNK)
                if not chunk:
                    break
                yield chunk
        except Exception as e:
            log.debug(f"Stream proxy: {e}")

    return StreamingResponse(
        _gen(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control":               "no-cache",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ── REST API ──────────────────────────────────────────────────────────────────
@app.get("/api/events")
def api_events(
    limit:  int             = Query(100, ge=1, le=1000),
    offset: int             = Query(0,   ge=0),
    kind:   Optional[str]   = None,
    camera: Optional[str]   = None,
    start:  Optional[float] = None,
    end:    Optional[float] = None,
    live_only: bool         = Query(False),
):
    rows, total = db_query(limit, offset, kind, camera, start, end)

    formatted = []
    for row in rows:
        detail = row.get("detail", "{}")
        if isinstance(detail, str):
            try:
                row["detail"] = json.loads(detail)
            except Exception:
                row["detail"] = {"raw": detail}

        row = format_event_for_ui(row)

        if live_only and not row.get("show_in_live_feed", True):
            continue

        formatted.append(row)

    return {
        "events": formatted,
        "total": total,
        "limit": limit,
        "offset": offset
    }

# ── Forbidden zones (operator-drawn polygons) ─────────────────────────────────

_VALID_SEVERITY_TIERS = {"CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"}
_VALID_TEMPLATE_KINDS = {"custom", "chemical", "electrical", "mezzanine", "dock", "fall"}


class ZoneCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    polygon: list  # validated below — list of [x,y] pairs in 0..1
    dwell_s: float = Field(default=0.0, ge=0.0, le=600.0)
    severity_tier: str = "CRITICAL"
    time_window: str = "*"
    authorized_roles: list = Field(default_factory=list)
    template_kind: str = "custom"
    shadow_until: int = Field(default=0, ge=0)


class ZoneUpdate(BaseModel):
    """All fields optional — only provided keys are updated."""
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    polygon: Optional[list] = None
    dwell_s: Optional[float] = Field(default=None, ge=0.0, le=600.0)
    severity_tier: Optional[str] = None
    time_window: Optional[str] = None
    authorized_roles: Optional[list] = None
    template_kind: Optional[str] = None
    shadow_until: Optional[int] = Field(default=None, ge=0)


def _validate_polygon(polygon) -> str:
    """Return JSON string for storage if valid; raise HTTPException otherwise."""
    if not isinstance(polygon, list) or len(polygon) < 3:
        raise HTTPException(status_code=400, detail="polygon must have >= 3 points")
    cleaned = []
    for p in polygon:
        if not (isinstance(p, (list, tuple)) and len(p) == 2):
            raise HTTPException(status_code=400, detail="each point must be [x,y]")
        try:
            x, y = float(p[0]), float(p[1])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="point coords must be numeric")
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise HTTPException(status_code=400, detail="coords must be in 0..1")
        cleaned.append([x, y])
    return json.dumps(cleaned)


def _validate_severity_tier(tier: str) -> str:
    t = str(tier).upper()
    if t not in _VALID_SEVERITY_TIERS:
        raise HTTPException(
            status_code=400,
            detail=f"severity_tier must be one of {sorted(_VALID_SEVERITY_TIERS)}",
        )
    return t


def _validate_time_window(tw: str) -> str:
    """Accept '*' (always-on) or 'HH:MM-HH:MM' (24h, wraps midnight if start > end)."""
    s = str(tw).strip()
    if s == "*" or s == "":
        return "*"
    parts = s.split("-")
    if len(parts) != 2:
        raise HTTPException(
            status_code=400,
            detail="time_window must be '*' or 'HH:MM-HH:MM'",
        )
    for chunk in parts:
        hm = chunk.split(":")
        if len(hm) != 2:
            raise HTTPException(status_code=400, detail="time_window: bad HH:MM")
        try:
            h, m = int(hm[0]), int(hm[1])
        except ValueError:
            raise HTTPException(status_code=400, detail="time_window: HH:MM must be numeric")
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise HTTPException(status_code=400, detail="time_window: out of range")
    return s


def _validate_template_kind(kind: str) -> str:
    k = str(kind).lower()
    if k not in _VALID_TEMPLATE_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"template_kind must be one of {sorted(_VALID_TEMPLATE_KINDS)}",
        )
    return k


def _validate_roles(roles) -> str:
    if not isinstance(roles, list):
        raise HTTPException(status_code=400, detail="authorized_roles must be a list")
    cleaned = []
    for r in roles:
        if not isinstance(r, str) or not r.strip():
            raise HTTPException(status_code=400, detail="each role must be a non-empty string")
        cleaned.append(r.strip()[:64])
    return json.dumps(cleaned)


def _row_to_zone(row) -> dict:
    keys = row.keys() if hasattr(row, "keys") else []
    def _g(k, default=None):
        return row[k] if k in keys else default
    try:
        roles = json.loads(_g("authorized_roles") or "[]")
    except Exception:
        roles = []
    return {
        "id":               row["id"],
        "name":             row["name"],
        "polygon":          json.loads(row["polygon"]),
        "created_at":       row["created_at"],
        "updated_at":       row["updated_at"],
        "dwell_s":          float(_g("dwell_s", 0.0) or 0.0),
        "severity_tier":    str(_g("severity_tier", "CRITICAL") or "CRITICAL"),
        "time_window":      str(_g("time_window", "*") or "*"),
        "authorized_roles": roles,
        "template_kind":    str(_g("template_kind", "custom") or "custom"),
        "shadow_until":     int(_g("shadow_until", 0) or 0),
    }


_ZONE_COLS = (
    "id, name, polygon, created_at, updated_at, "
    "dwell_s, severity_tier, time_window, authorized_roles, template_kind, "
    "shadow_until"
)


@app.get("/api/zones")
def api_zones_list():
    with _db_lock:
        c = _conn()
        rows = c.execute(
            f"SELECT {_ZONE_COLS} FROM forbidden_zones ORDER BY id"
        ).fetchall()
        c.close()
    return {"zones": [_row_to_zone(r) for r in rows]}


@app.post("/api/zones", status_code=201)
def api_zones_create(payload: ZoneCreate):
    poly_json = _validate_polygon(payload.polygon)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name must not be empty")
    severity = _validate_severity_tier(payload.severity_tier)
    time_window = _validate_time_window(payload.time_window)
    template = _validate_template_kind(payload.template_kind)
    roles = _validate_roles(payload.authorized_roles)
    now = int(time.time())
    with _db_lock:
        c = _conn()
        cur = c.execute(
            "INSERT INTO forbidden_zones "
            "(name, polygon, created_at, updated_at, "
            " dwell_s, severity_tier, time_window, authorized_roles, template_kind, "
            " shadow_until) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, poly_json, now, now,
             float(payload.dwell_s), severity, time_window, roles, template,
             int(payload.shadow_until)),
        )
        row_id = cur.lastrowid
        c.commit()
        row = c.execute(
            f"SELECT {_ZONE_COLS} FROM forbidden_zones WHERE id=?", (row_id,),
        ).fetchone()
        c.close()
    log.info(
        f"forbidden_zone created id={row_id} name={name!r} "
        f"template={template} severity={severity} dwell={payload.dwell_s} "
        f"window={time_window}"
    )
    return _row_to_zone(row)


@app.patch("/api/zones/{zone_id}")
def api_zones_update(zone_id: int, payload: ZoneUpdate):
    """Partial update — only fields present in the body are written."""
    sets: list = []
    args: list = []
    if payload.name is not None:
        n = payload.name.strip()
        if not n:
            raise HTTPException(status_code=400, detail="name must not be empty")
        sets.append("name=?"); args.append(n)
    if payload.polygon is not None:
        sets.append("polygon=?"); args.append(_validate_polygon(payload.polygon))
    if payload.dwell_s is not None:
        sets.append("dwell_s=?"); args.append(float(payload.dwell_s))
    if payload.severity_tier is not None:
        sets.append("severity_tier=?"); args.append(_validate_severity_tier(payload.severity_tier))
    if payload.time_window is not None:
        sets.append("time_window=?"); args.append(_validate_time_window(payload.time_window))
    if payload.authorized_roles is not None:
        sets.append("authorized_roles=?"); args.append(_validate_roles(payload.authorized_roles))
    if payload.template_kind is not None:
        sets.append("template_kind=?"); args.append(_validate_template_kind(payload.template_kind))
    if payload.shadow_until is not None:
        sets.append("shadow_until=?"); args.append(int(payload.shadow_until))
    if not sets:
        raise HTTPException(status_code=400, detail="no fields to update")
    sets.append("updated_at=?"); args.append(int(time.time()))
    args.append(zone_id)
    with _db_lock:
        c = _conn()
        cur = c.execute(
            f"UPDATE forbidden_zones SET {', '.join(sets)} WHERE id=?", args,
        )
        if cur.rowcount == 0:
            c.close()
            raise HTTPException(status_code=404, detail="zone not found")
        c.commit()
        row = c.execute(
            f"SELECT {_ZONE_COLS} FROM forbidden_zones WHERE id=?", (zone_id,),
        ).fetchone()
        c.close()
    log.info(f"forbidden_zone updated id={zone_id} fields={[s.split('=')[0] for s in sets]}")
    return _row_to_zone(row)


@app.get("/api/zones/{zone_id}/rejections")
def api_zone_rejections_list(zone_id: int):
    """List learned rejections (False-alarm-derived suppressions) for a zone."""
    with _db_lock:
        c = _conn()
        try:
            rows = c.execute(
                "SELECT id, zone_id, cx, cy, hour, weekday, source_event_id, created_at "
                "FROM zone_rejections WHERE zone_id=? ORDER BY created_at DESC",
                (zone_id,),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        c.close()
    return {
        "rejections": [
            {
                "id":               r["id"],
                "zone_id":          r["zone_id"],
                "cx":               r["cx"],
                "cy":               r["cy"],
                "hour":             r["hour"],
                "weekday":          r["weekday"],
                "source_event_id":  r["source_event_id"],
                "created_at":       r["created_at"],
            } for r in rows
        ],
    }


@app.delete("/api/rejections/{rejection_id}", status_code=204)
def api_rejection_delete(rejection_id: int):
    """Forget a learned rejection (operator clicked False-alarm by mistake)."""
    with _db_lock:
        c = _conn()
        # Read zone_id so we can bump forbidden_zones.updated_at and
        # trigger a hot reload in the pipeline.
        row = c.execute(
            "SELECT zone_id FROM zone_rejections WHERE id=?", (rejection_id,)
        ).fetchone()
        if not row:
            c.close()
            raise HTTPException(status_code=404, detail="rejection not found")
        c.execute("DELETE FROM zone_rejections WHERE id=?", (rejection_id,))
        c.execute(
            "UPDATE forbidden_zones SET updated_at=? WHERE id=?",
            (int(time.time()), row["zone_id"]),
        )
        c.commit()
        c.close()
    log.info(f"rejection forgotten id={rejection_id}")
    return Response(status_code=204)


@app.delete("/api/zones/{zone_id}", status_code=204)
def api_zones_delete(zone_id: int):
    with _db_lock:
        c = _conn()
        cur = c.execute("DELETE FROM forbidden_zones WHERE id=?", (zone_id,))
        deleted = cur.rowcount
        c.commit()
        c.close()
    if not deleted:
        raise HTTPException(status_code=404, detail="zone not found")
    log.info(f"forbidden_zone deleted id={zone_id}")
    return Response(status_code=204)


_VALID_ACTIONS = {"acknowledge", "dispatch", "false_alarm"}


class ActionCreate(BaseModel):
    action: str
    actor: str = "operator"
    note: str = ""


def _row_to_action(row) -> dict:
    return {
        "id":       row["id"],
        "event_id": row["event_id"],
        "action":   row["action"],
        "actor":    row["actor"],
        "ts":       row["ts"],
        "note":     row["note"],
    }


@app.post("/api/events/{event_id}/actions", status_code=201)
def api_event_action_create(event_id: int, payload: ActionCreate):
    """Record an operator action on an event (Acknowledge / Dispatch / False alarm).

    Multiple actions may exist per event (an event can be acknowledged,
    then escalated via dispatch, then later marked false_alarm). We
    keep the full timeline so the audit / insurance export is complete.
    """
    action = str(payload.action).strip().lower()
    if action not in _VALID_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"action must be one of {sorted(_VALID_ACTIONS)}",
        )
    actor = (payload.actor or "operator").strip()[:64] or "operator"
    note = (payload.note or "").strip()[:500]
    now = int(time.time())
    rejection_inserted = False
    with _db_lock:
        c = _conn()
        ev = c.execute(
            "SELECT id, ts, kind, detail FROM events WHERE id=?", (event_id,)
        ).fetchone()
        if not ev:
            c.close()
            raise HTTPException(status_code=404, detail="event not found")
        cur = c.execute(
            "INSERT INTO event_actions (event_id, action, actor, ts, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (event_id, action, actor, now, note),
        )
        row_id = cur.lastrowid

        # Phase 4a: a False-alarm click on a forbidden_zone event seeds a
        # rejection so the rule stops firing on similar future hits.
        # Idempotent — UNIQUE INDEX on source_event_id makes a duplicate
        # click a no-op rather than a doubled rejection.
        if action == "false_alarm" and str(ev["kind"]) == "forbidden_zone":
            try:
                detail = json.loads(ev["detail"]) if isinstance(ev["detail"], str) else (ev["detail"] or {})
            except Exception:
                detail = {}
            zone_id = detail.get("zone_id")
            bbox = detail.get("bbox") or []
            if isinstance(zone_id, int) and len(bbox) == 4:
                cx = int((int(bbox[0]) + int(bbox[2])) / 2)
                cy = int((int(bbox[1]) + int(bbox[3])) / 2)
                try:
                    ts_local = datetime.datetime.fromtimestamp(float(ev["ts"]))
                    hour = ts_local.hour
                    weekday = ts_local.weekday()
                except Exception:
                    hour, weekday = 0, 0
                try:
                    c.execute(
                        "INSERT INTO zone_rejections "
                        "(zone_id, cx, cy, hour, weekday, source_event_id, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (zone_id, cx, cy, hour, weekday, event_id, now),
                    )
                    rejection_inserted = True
                except sqlite3.IntegrityError:
                    # Duplicate False-alarm on same event — ignore.
                    pass
                # Bump forbidden_zones.updated_at so the engine picks up
                # the new rejection on its next 5s reload tick.
                c.execute(
                    "UPDATE forbidden_zones SET updated_at=? WHERE id=?",
                    (now, zone_id),
                )

        c.commit()
        row = c.execute(
            "SELECT id, event_id, action, actor, ts, note "
            "FROM event_actions WHERE id=?", (row_id,),
        ).fetchone()
        c.close()
    log.info(
        f"event_action recorded event_id={event_id} action={action} "
        f"actor={actor!r} rejection_seeded={rejection_inserted}"
    )
    return _row_to_action(row)


@app.get("/api/events/{event_id}/actions")
def api_event_actions_list(event_id: int):
    """Return the action timeline (oldest first) for one event."""
    with _db_lock:
        c = _conn()
        rows = c.execute(
            "SELECT id, event_id, action, actor, ts, note "
            "FROM event_actions WHERE event_id=? ORDER BY ts ASC",
            (event_id,),
        ).fetchall()
        c.close()
    return {"actions": [_row_to_action(r) for r in rows]}


@app.get("/api/events/actions/latest")
def api_event_actions_latest(ids: str = Query(default="")):
    """Batch lookup: latest action per event id, keyed by id.

    Frontend calls this once per events feed render so cards can show
    "Acked by X" without an N+1. Returns ``{}`` for unknown ids.
    """
    raw = [s for s in (ids or "").split(",") if s.strip()]
    parsed: list = []
    for s in raw:
        try:
            parsed.append(int(s))
        except ValueError:
            continue
    if not parsed:
        return {}
    placeholders = ",".join("?" * len(parsed))
    with _db_lock:
        c = _conn()
        # Latest action per event_id (max(ts) wins). SQLite-friendly
        # subquery: select rows where (event_id, ts) matches the max ts
        # for that event_id.
        rows = c.execute(
            f"SELECT id, event_id, action, actor, ts, note FROM event_actions "
            f"WHERE event_id IN ({placeholders}) "
            f"AND (event_id, ts) IN ("
            f"  SELECT event_id, MAX(ts) FROM event_actions "
            f"  WHERE event_id IN ({placeholders}) GROUP BY event_id"
            f")",
            parsed + parsed,
        ).fetchall()
        c.close()
    return {str(r["event_id"]): _row_to_action(r) for r in rows}


@app.get("/api/events/{event_id}/snapshot")
def api_event_snapshot(event_id: int):
    """Serve the JPEG snapshot captured when this event fired.

    Looks up the events row, reads the monotonic event id from
    ``detail.event_id`` (stamped by emit_alert in detect.py), and
    returns the matching JPEG from SNAPSHOT_DIR. Snapshot filenames
    are ``<start_epoch>_<monotonic_id>.jpg``; the start_epoch prefix
    differs per pipeline restart, so we glob by suffix.

    404 if the events row, the monotonic id, or the file is missing.
    """
    try:
        row = _conn().execute(
            "SELECT detail FROM events WHERE id=?", (event_id,)
        ).fetchone()
    except Exception as e:
        log.warning(f"snapshot db lookup failed event_id={event_id}: {e}")
        return Response(status_code=404)
    if not row:
        return Response(status_code=404)
    try:
        detail = json.loads(row["detail"]) if isinstance(row["detail"], str) else (row["detail"] or {})
    except Exception:
        return Response(status_code=404)
    monotonic_id = detail.get("event_id")
    if not isinstance(monotonic_id, int):
        return Response(status_code=404)
    if not SNAPSHOT_DIR.is_dir():
        return Response(status_code=404)
    matches = sorted(SNAPSHOT_DIR.glob(f"*_{monotonic_id}.jpg"))
    if not matches:
        return Response(status_code=404)
    return FileResponse(
        matches[-1],  # most recent if multiple epochs collide
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/events/{event_id}/clip")
def api_event_clip(event_id: int):
    """Serve the 5-second MP4 clip captured around this event (Phase 2b).

    Same monotonic-id glob trick as snapshots — clips live in CLIP_DIR
    named ``<start_epoch>_<monotonic_id>.mp4``. The clip writer thread
    in detect.py finalises the file ~clip_post_s after the event
    fires, so this endpoint can 404 briefly right after an alert
    before turning into a 200 once the encoder lands the file.
    """
    try:
        row = _conn().execute(
            "SELECT detail FROM events WHERE id=?", (event_id,)
        ).fetchone()
    except Exception as e:
        log.warning(f"clip db lookup failed event_id={event_id}: {e}")
        return Response(status_code=404)
    if not row:
        return Response(status_code=404)
    try:
        detail = json.loads(row["detail"]) if isinstance(row["detail"], str) else (row["detail"] or {})
    except Exception:
        return Response(status_code=404)
    monotonic_id = detail.get("event_id")
    if not isinstance(monotonic_id, int):
        return Response(status_code=404)
    if not CLIP_DIR.is_dir():
        return Response(status_code=404)
    matches = sorted(CLIP_DIR.glob(f"*_{monotonic_id}.mp4"))
    if not matches:
        return Response(status_code=404)
    # FileResponse handles HTTP Range requests automatically — the
    # browser's <video> element scrubs by issuing partial-content GETs.
    return FileResponse(
        matches[-1],
        media_type="video/mp4",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/stats")
def api_stats():
    stats = db_stats()
    stats["mqtt_connected"] = _mqtt_ok
    stats["uptime_s"]       = int(time.time() - _start_ts)
    stats["stream_port"]    = STREAM_PORT
    return stats


def _detail_dict(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            d = json.loads(raw)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}
    return {}


@app.get("/api/reports/weekly")
def api_reports_weekly(
    days: int = Query(default=7, ge=1, le=90),
    camera: Optional[str] = None,
    sample_limit: int = Query(default=20, ge=0, le=200),
):
    """Aggregated rollup for the operator's weekly review (Phase 3).

    Defaults to a 7-day window. Returns:
      * window: {since, until, days}
      * totals: events, by_kind, by_camera
      * shadow: how many events were shadow-only (no Telegram)
      * after_hours: how many forbidden_zone events fired during a
                     time_window match outside business hours (best-effort
                     since we don't store mode per event — we use the
                     stamped ``detail.severity`` ladder that's already
                     mode-aware as a coarse proxy)
      * by_zone: forbidden_zone breakdown by zone name
      * fp: {by_camera, total, over_budget_per_camera_per_week}
      * sample_event_ids: most-recent ids the operator can spot-check
                          via the modal/clip player
    """
    until = int(time.time())
    since = until - days * 86400
    args_e: list = [since, until]
    cam_clause = ""
    if camera:
        cam_clause = " AND camera=?"
        args_e.append(camera)

    with _db_lock:
        c = _conn()

        total = c.execute(
            f"SELECT COUNT(*) FROM events WHERE ts>=? AND ts<=?{cam_clause}",
            args_e,
        ).fetchone()[0]

        by_kind = {
            r["kind"]: r["c"]
            for r in c.execute(
                f"SELECT kind, COUNT(*) c FROM events "
                f"WHERE ts>=? AND ts<=?{cam_clause} GROUP BY kind ORDER BY c DESC",
                args_e,
            )
        }
        by_camera = {
            r["camera"]: r["c"]
            for r in c.execute(
                f"SELECT camera, COUNT(*) c FROM events "
                f"WHERE ts>=? AND ts<=?{cam_clause} GROUP BY camera ORDER BY c DESC",
                args_e,
            )
        }

        # Currently-configured zones: id → name. Used to split historical
        # forbidden_zone events into "active zone" rows (the operator
        # still has this zone drawn) vs "deleted zone" rows (events from
        # zones that have since been removed). Without this split the
        # report misleads when zones are deleted and the historical
        # events linger in the table.
        try:
            current_zones = {
                int(r["id"]): str(r["name"])
                for r in c.execute("SELECT id, name FROM forbidden_zones")
            }
        except sqlite3.OperationalError:
            current_zones = {}

        # Walk forbidden_zone rows once to build zone + shadow + after-hours
        # counters. detail.shadow is a per-event flag; detail.zone is the
        # human zone name; ts is the event-fire time for the after-hours
        # bucket (00:00–06:00 or 18:00–24:00 local — coarse but useful).
        zone_rows = c.execute(
            f"SELECT ts, detail FROM events WHERE kind='forbidden_zone' "
            f"AND ts>=? AND ts<=?{cam_clause}",
            args_e,
        ).fetchall()
        by_zone_active: dict = {}
        by_zone_deleted: dict = {}
        shadow_count = 0
        after_hours = 0
        for r in zone_rows:
            d = _detail_dict(r["detail"])
            zid = d.get("zone_id")
            zname = str(d.get("zone") or d.get("zone_name") or "—")
            # Resolve to the live name when the id still exists, so renames
            # are reflected in the active breakdown.
            if isinstance(zid, int) and zid in current_zones:
                live = current_zones[zid]
                by_zone_active[live] = by_zone_active.get(live, 0) + 1
            else:
                by_zone_deleted[zname] = by_zone_deleted.get(zname, 0) + 1
            if d.get("shadow"):
                shadow_count += 1
            try:
                hr = datetime.datetime.fromtimestamp(float(r["ts"])).hour
                if hr < 6 or hr >= 18:
                    after_hours += 1
            except Exception:
                pass

        # FP rate by camera (false_alarm actions joined back to events).
        fp_rows = c.execute(
            f"SELECT e.camera AS camera, COUNT(*) AS c "
            f"FROM event_actions ea JOIN events e ON e.id=ea.event_id "
            f"WHERE ea.action='false_alarm' AND ea.ts>=? AND ea.ts<=?"
            + (f" AND e.camera=?" if camera else "") +
            f" GROUP BY e.camera",
            args_e,
        ).fetchall() if True else []
        try:
            fp_by_camera = {r["camera"]: r["c"] for r in fp_rows}
        except sqlite3.OperationalError:
            fp_by_camera = {}

        # Sample event ids — most recent, weighted toward forbidden_zone
        # so the operator's spot-check covers what the spec actually
        # cares about. Falls back to "any recent event" when there are
        # too few zone events.
        sample_ids: list = []
        if sample_limit > 0:
            zone_ids = [
                r["id"] for r in c.execute(
                    f"SELECT id FROM events WHERE kind='forbidden_zone' "
                    f"AND ts>=? AND ts<=?{cam_clause} ORDER BY ts DESC LIMIT ?",
                    args_e + [sample_limit],
                )
            ]
            sample_ids.extend(zone_ids)
            if len(sample_ids) < sample_limit:
                pad = sample_limit - len(sample_ids)
                rest_ids = [
                    r["id"] for r in c.execute(
                        f"SELECT id FROM events "
                        f"WHERE ts>=? AND ts<=?{cam_clause} "
                        f"AND kind!='forbidden_zone' ORDER BY ts DESC LIMIT ?",
                        args_e + [pad],
                    )
                ]
                sample_ids.extend(rest_ids)

        c.close()

    weeks = max(days / 7.0, 1.0 / 7.0)  # avoid /0
    fp_per_camera_per_week = {
        cam: round(n / weeks, 2) for cam, n in fp_by_camera.items()
    }
    fp_over_budget = {
        cam: rate > 2.0 for cam, rate in fp_per_camera_per_week.items()
    }

    return {
        "window": {"since": since, "until": until, "days": days},
        "totals": {
            "events":     total,
            "by_kind":    by_kind,
            "by_camera":  by_camera,
        },
        "zones_configured": len(current_zones),
        "shadow":      {"count": shadow_count},
        "after_hours": {"count": after_hours},
        # by_zone kept as a merged view for backwards compatibility, but
        # the UI should prefer by_zone_active vs by_zone_deleted so the
        # operator can tell apart "still-drawn zone" vs "zone you deleted".
        "by_zone":          {**by_zone_active, **by_zone_deleted},
        "by_zone_active":   by_zone_active,
        "by_zone_deleted":  by_zone_deleted,
        "fp": {
            "by_camera":                  fp_by_camera,
            "per_camera_per_week":        fp_per_camera_per_week,
            "over_budget_per_camera":     fp_over_budget,
            "budget":                     2.0,
        },
        "sample_event_ids": sample_ids,
    }


@app.get("/api/status")
def api_status():
    return {
        "ok":       True,
        "mqtt":     _mqtt_ok,
        "uptime_s": int(time.time() - _start_ts),
        "ts":       time.time(),
    }


# ── WebSocket ─────────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await _mgr.connect(ws)
    try:
        await ws.send_json({"type": "connected", "ts": time.time()})
        while True:
            await asyncio.sleep(20)
            await ws.send_json({"type": "ping", "ts": time.time()})
    except WebSocketDisconnect:
        _mgr.disconnect(ws)
    except Exception:
        _mgr.disconnect(ws)


# ── Serve dashboard ───────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def root():
    html = STATIC_DIR / "index.html"
    if html.exists():
        return html.read_text(encoding="utf-8")
    return HTMLResponse("<h2>dashboard_static/index.html not found</h2>", 500)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(
        "dashboard_server:app",
        host="0.0.0.0",
        port=DASH_PORT,
        reload=False,
        log_level="info",
        workers=1,
    )