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
        """)
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

        c.close()

    return {
        "total":     total,
        "today":     today,
        "last_hour": last_hour,
        "by_kind":   by_kind,
        "cameras":   cameras,
        "hourly":    hourly,
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

class ZoneCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    polygon: list  # validated below — list of [x,y] pairs in 0..1


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


def _row_to_zone(row) -> dict:
    return {
        "id":         row["id"],
        "name":       row["name"],
        "polygon":    json.loads(row["polygon"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@app.get("/api/zones")
def api_zones_list():
    with _db_lock:
        c = _conn()
        rows = c.execute(
            "SELECT id, name, polygon, created_at, updated_at "
            "FROM forbidden_zones ORDER BY id"
        ).fetchall()
        c.close()
    return {"zones": [_row_to_zone(r) for r in rows]}


@app.post("/api/zones", status_code=201)
def api_zones_create(payload: ZoneCreate):
    poly_json = _validate_polygon(payload.polygon)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name must not be empty")
    now = int(time.time())
    with _db_lock:
        c = _conn()
        cur = c.execute(
            "INSERT INTO forbidden_zones (name, polygon, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (name, poly_json, now, now),
        )
        row_id = cur.lastrowid
        c.commit()
        row = c.execute(
            "SELECT id, name, polygon, created_at, updated_at "
            "FROM forbidden_zones WHERE id=?", (row_id,),
        ).fetchone()
        c.close()
    log.info(f"forbidden_zone created id={row_id} name={name!r}")
    return _row_to_zone(row)


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


@app.get("/api/stats")
def api_stats():
    stats = db_stats()
    stats["mqtt_connected"] = _mqtt_ok
    stats["uptime_s"]       = int(time.time() - _start_ts)
    stats["stream_port"]    = STREAM_PORT
    return stats


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