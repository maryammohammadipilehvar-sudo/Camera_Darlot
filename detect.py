"""
Production AI Security Pipeline v2 — Jetson Orin
Stack: YOLOv9-C (TensorRT) → ByteTrack → BehaviorAnalyzer (YOLOv8n-pose TRT)
       → SCRFD face detect → AdaFace embed → Faiss 1:N search
       → YAMNet audio → PatchCore anomaly → MQTT alerts

Changes from v1:
  - MQTT worker with auto-reconnect loop
  - /health endpoint on port 8081 (JSON status)
  - Behavior/pose analysis via behavior.py (graceful degradation)
  - Thermal monitor thread (logs + alerts on >70°C)
  - Structured log fields (machine-parseable)
  - Alert deduplication per-track with per-kind cooldowns
  - Graceful SIGTERM / SIGINT shutdown with final stats log
  - face_every_n / anomaly_every_n controlled by config (was hardcoded 999999)
  - Separate MJPEG annotate queue to reduce main-loop latency
"""

import os, time, signal, logging, threading, json, queue, collections, http.server, itertools

import re
from pathlib import Path

def _clean_behavior_label(v):
    try:
        if v is None:
            return "unknown"
        if not isinstance(v, str):
            v = str(v)
        raw = v.strip()
        if not raw:
            return "unknown"

        if "???" in raw:
            parts = [x.strip() for x in raw.split("???")]
            good = [x for x in parts if x and x.lower() not in ("unknown", "none", "n/a")]
            if good:
                raw = good[0]
            elif parts:
                raw = parts[0].strip() or "unknown"

        raw = re.sub(r'\bunknown\b$', '', raw, flags=re.I).strip()
        raw = re.sub(r'\s+', ' ', raw).strip(" -_:,")
        return raw or "unknown"
    except Exception:
        return "unknown"

import socketserver
import numpy as np
import cv2
import torch

DEVICE = "cuda:0" if torch.cuda.is_available() else "cpu"

os.environ.setdefault("YOLO_CONFIG_DIR", "/tmp/Ultralytics")
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;5000000"

# ─────────────────────────── CONFIG ───────────────────────────────────────────
CFG = {
    "rtsp_url":     "rtsp://admin:QuareroRobotics_2025@192.168.2.148/Preview_01_sub",
    "camera_id":    "cam_01",

    # Models
    "yolo_engine":     "models/yolov9c.pt",      # swap to .engine after trtexec
    "pose_engine":     "models/yolov8n-pose.pt",
    "scrfd_model":     "models/scrfd_10g.onnx",
    "adaface_model":   "models/adaface_ir50.onnx",
    "patchcore_model": "models/patchcore_memory.pt",
    "yamnet_model":    "models/yamnet.tflite",

    # Thresholds
    "yolo_conf":       0.55,
    "face_conf":       0.60,
    "face_sim_alert":  0.85,
    "face_sim_review": 0.60,
    "anomaly_thresh":  0.90,
    "audio_conf":      0.75,

    # Inference rates
    "mjpeg_port":       8080,
    "health_port":      8081,
    "mjpeg_quality":    60,
    "resize_w":         640,
    "resize_h":         360,
    "inference_fps":    10.0,
    "face_every_n":     999999,    # set to e.g. 3 when face pipeline is stable
    "behavior_every_n": 3,         # run behavior every Nth detection frame
    "anomaly_every_n":  999999,    # set to e.g. 30 when patchcore is built

    # Alert deduplication
    "alert_cooldown_sec": 30.0,    # per object, per kind
    "object_ttl_sec":     20.0,
    "summary_min_gap":     5.0,    # min seconds between scene-summary events

    # Zone rules
    "zones": [
        {"name": "entrance", "polygon": [[0,0],[320,0],[320,360],[0,360]], "dwell_sec": 120},
    ],

    # MQTT
    "mqtt_host":  "localhost",
    "mqtt_port":  1883,
    "mqtt_topic": "security/alerts",

    # Misc
    "reconnect_delay":  2.0,
    "watchlist_db":     "watchlist.index",
    "watchlist_meta":   "watchlist_meta.json",
    "thermal_warn_c":   70.0,
    "thermal_crit_c":   80.0,

    # Snapshot capture (event-time frame retention for dashboard/Telegram)
    "snapshot_enabled": True,
    "snapshot_dir":     "~/.local/share/darlot/snapshots",
    "snapshot_quality": 85,       # JPEG quality; evidence setting, not MJPEG
    "snapshot_queue":   16,       # drop-newest on overflow
    "snapshot_keep":    1000,     # prune oldest by mtime when over cap

    # Site identity (Operator must edit per deployment.)
    "site_name":        "darlot-default",

    # Telegram notifier
    "notifier_enabled": True,     # set False to disable push alerts at startup
    "notifier_queue":   100,      # detect.py-side queue for notify jobs
}

ALLOWED_CLASSES = {0, 2, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23}

# ─────────────────────────── LOGGING ──────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)-12s] %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("pipeline")


# ─────────────────────────── NOTIFIER MODULE ──────────────────────────────────
# Import here (after log is configured) so a credentials misconfiguration
# degrades gracefully: log.error, continue without Telegram. MQTT, dashboard,
# snapshots, and detection keep running. /health surfaces the failure reason
# via telegram_last_error for operators to diagnose.
try:
    import telegram_notifier
    _notifier_module_error: "str | None" = None
except Exception as e:
    _notifier_module_error = f"{type(e).__name__}: {e}"
    log.error(
        "NOTIFIER module unavailable (%s) — Telegram disabled, pipeline continues",
        e,
    )
    telegram_notifier = None  # type: ignore[assignment]


# ─────────────────────────── SHARED STATE ─────────────────────────────────────
_health = {
    "status":         "starting",
    "mqtt":           False,
    "camera":         False,
    "fps":            0.0,
    "frames_total":   0,
    "alerts_total":   0,
    "thermal_c":      0.0,
    "behavior_ready": False,
    "uptime_s":       0,
    "start_ts":       time.time(),
    # Snapshot capture observability (also updated live in /health handler)
    "snapshot_count":            0,
    "snapshot_errors":           0,
    "snapshot_queue_depth":      0,
    "snapshot_last_write_s_ago": None,
    # Telegram notifier observability (also updated live in /health handler)
    "telegram_last_success_s_ago": None,
    "telegram_last_error":         _notifier_module_error or "not yet started",
    "telegram_sends":              0,
    "telegram_drops":              0,
    "notifier_queue_depth":        0,
    "notifier_queue_drops":        0,
}
_health_lock = threading.Lock()

def _hset(**kw):
    with _health_lock:
        _health.update(kw)
        _health["uptime_s"] = int(time.time() - _health["start_ts"])


# ─────────────────────────── SNAPSHOT CAPTURE ─────────────────────────────────
# Monotonic per-alert id. Unique within a single pipeline run; filenames are
# prefixed with _snapshot_start_epoch so restarts never overwrite evidence.
_event_counter      = itertools.count(1)
_event_counter_lock = threading.Lock()

# Queue entries: (target_path: Path, frame: np.ndarray, enqueue_ts: float).
_snapshot_q: "queue.Queue" = queue.Queue(maxsize=CFG["snapshot_queue"])

# Latest annotated frame, published by the main loop after draw_hud each
# iteration. Read by _snapshot_enqueue. Single-assignment of a numpy array
# reference is atomic under the CPython GIL; consumers .copy() defensively.
_snapshot_frame = None

_snapshot_dir: Path       = Path(os.path.expanduser(str(CFG["snapshot_dir"])))
_snapshot_start_epoch     = int(time.time())
_snapshot_count           = 0
_snapshot_errors          = 0
_snapshot_last_write_ts   = 0.0
_snapshot_nonmain_seen: set = set()


def _next_event_id() -> int:
    """Return the next monotonic event id. Thread-safe."""
    with _event_counter_lock:
        return next(_event_counter)


def _setup_snapshot_dir(cfg: dict) -> bool:
    """Create the snapshot directory if missing. Degrades on failure.

    Args:
        cfg: pipeline config dict; mutated to set ``snapshot_enabled=False``
            if the directory cannot be created.

    Returns:
        True if the directory is ready; False if snapshots are disabled.
    """
    global _snapshot_dir
    path = Path(os.path.expanduser(str(cfg["snapshot_dir"])))
    try:
        path.mkdir(parents=True, exist_ok=True)
        _snapshot_dir = path
        log.info(
            "SNAPSHOT dir ready: %s (start_epoch=%d, keep=%d, q=%d)",
            path, _snapshot_start_epoch,
            cfg["snapshot_keep"], cfg["snapshot_queue"],
        )
        return True
    except Exception as e:
        log.error("SNAPSHOT dir create failed (%s): snapshots disabled", e)
        cfg["snapshot_enabled"] = False
        return False


def _snapshot_enqueue(event_id: int) -> "str | None":
    """Publish the current annotated frame to the snapshot writer queue.

    Non-blocking. Returns the expected file path (the writer may not have
    written it yet) or None when snapshots are disabled, no frame is
    available, or the queue is full.

    Args:
        event_id: monotonic id assigned by ``_next_event_id``.

    Returns:
        The expected JPEG path as a string, or None on any degraded path.
    """
    if not CFG.get("snapshot_enabled", True):
        return None
    frame = _snapshot_frame
    if frame is None:
        return None  # main loop hasn't produced its first frame yet

    name = threading.current_thread().name
    if name != "MainThread" and name not in _snapshot_nonmain_seen:
        _snapshot_nonmain_seen.add(name)
        log.info(
            "SNAPSHOT NOTE: first enqueue from non-main thread=%s "
            "(will use most recent main-loop frame — may be slightly stale)",
            name,
        )

    try:
        frame_copy = frame.copy()
    except Exception as e:
        log.warning("SNAPSHOT frame copy failed for event=%d: %s", event_id, e)
        return None

    target = _snapshot_dir / f"{_snapshot_start_epoch}_{event_id}.jpg"
    try:
        _snapshot_q.put_nowait((target, frame_copy, time.time()))
    except queue.Full:
        log.warning(
            "SNAPSHOT queue full — dropped event=%d depth=%d",
            event_id, _snapshot_q.qsize(),
        )
        return None
    return str(target)


def _snapshot_prune(directory: Path, keep: int) -> None:
    """Delete oldest files by mtime until at most ``keep`` remain."""
    try:
        files = [
            (f.stat().st_mtime, f)
            for f in directory.iterdir()
            if f.is_file() and f.suffix == ".jpg"
        ]
        if len(files) <= keep:
            return
        files.sort(key=lambda x: x[0])
        excess = files[: len(files) - keep]
        for _mtime, path in excess:
            try:
                path.unlink()
            except Exception as e:
                log.warning("SNAPSHOT prune failed for %s: %s", path, e)
    except Exception as e:
        log.warning("SNAPSHOT prune listing failed: %s", e)


def _snapshot_writer(cfg: dict) -> None:
    """Background thread — write queued snapshots to disk, prune over cap."""
    global _snapshot_count, _snapshot_errors, _snapshot_last_write_ts
    log.info("SNAPSHOT writer thread started")
    keep = cfg["snapshot_keep"]
    quality = cfg["snapshot_quality"]
    while True:
        try:
            item = _snapshot_q.get(timeout=1.0)
        except queue.Empty:
            continue
        if item is None:  # shutdown sentinel — not currently used, reserved
            break
        target, frame, _enq_ts = item
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            ok = cv2.imwrite(str(target), frame,
                             [cv2.IMWRITE_JPEG_QUALITY, quality])
            if not ok:
                raise IOError(f"cv2.imwrite returned False for {target}")
            _snapshot_count += 1
            _snapshot_last_write_ts = time.time()
            _hset(snapshot_count=_snapshot_count)
            _snapshot_prune(_snapshot_dir, keep)
        except Exception as e:
            _snapshot_errors += 1
            _hset(snapshot_errors=_snapshot_errors)
            log.error("SNAPSHOT write failed: %s err=%s", target, e)


def start_snapshot_writer(cfg: dict) -> None:
    """Spawn the snapshot writer thread if snapshots are enabled."""
    if not cfg.get("snapshot_enabled", True):
        log.info("SNAPSHOT disabled — writer thread not started")
        return
    threading.Thread(
        target=_snapshot_writer, args=(cfg,), daemon=True, name="snapshot",
    ).start()


# ─────────────────────────── FRAME BUS ────────────────────────────────────────
class _FrameBus:
    def __init__(self):
        self._jpg   = None
        self._lock  = threading.Lock()
        self._event = threading.Event()

    def put(self, jpg: bytes):
        with self._lock:
            self._jpg = jpg
        self._event.set()

    def get(self, timeout=2.0):
        self._event.wait(timeout)
        with self._lock:
            return self._jpg

_frame_bus = _FrameBus()


# ─────────────────────────── MJPEG SERVER ─────────────────────────────────────
class _MJPEGHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        if self.path not in ("/", "/stream"):
            self.send_error(404); return
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            while True:
                jpg = _frame_bus.get()
                if jpg is None: continue
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

class _ThreadedHTTP(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

def start_mjpeg(port: int):
    srv = _ThreadedHTTP(("0.0.0.0", port), _MJPEGHandler)
    threading.Thread(target=srv.serve_forever, daemon=True, name="mjpeg").start()
    log.info(f"MJPEG  → http://0.0.0.0:{port}/stream")
    return srv


# ─────────────────────────── HEALTH SERVER ────────────────────────────────────
class _HealthHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        if self.path == "/health":
            with _health_lock:
                # Live fields computed on read so the values don't go stale.
                _health["snapshot_queue_depth"] = _snapshot_q.qsize()
                _health["snapshot_last_write_s_ago"] = (
                    int(time.time() - _snapshot_last_write_ts)
                    if _snapshot_last_write_ts > 0 else None
                )
                # Notifier / Telegram observability.
                _health["notifier_queue_depth"] = _notify_q.qsize()
                if telegram_notifier is not None:
                    _health["telegram_last_success_s_ago"] = (
                        int(time.time() - telegram_notifier.last_success_ts)
                        if telegram_notifier.last_success_ts > 0 else None
                    )
                    _health["telegram_last_error"] = telegram_notifier.last_error
                    _health["telegram_sends"]      = telegram_notifier.send_count
                    _health["telegram_drops"]      = telegram_notifier.drop_count
                body = json.dumps(_health, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

def start_health(port: int):
    srv = _ThreadedHTTP(("0.0.0.0", port), _HealthHandler)
    threading.Thread(target=srv.serve_forever, daemon=True, name="health").start()
    log.info(f"Health → http://0.0.0.0:{port}/health")
    return srv


# ─────────────────────────── THERMAL MONITOR ──────────────────────────────────
def _read_thermal_zone(zone: int = 0) -> float:
    """Read Jetson thermal zone in Celsius."""
    try:
        with open(f"/sys/class/thermal/thermal_zone{zone}/temp") as f:
            return int(f.read().strip()) / 1000.0
    except Exception:
        return 0.0

def start_thermal_monitor(cfg: dict):
    def _run():
        while True:
            try:
                # Jetson Orin: thermal_zone0 is CPU cluster, zone7 is GPU
                # We read both and take max
                temps = [_read_thermal_zone(i) for i in range(10)]
                t = max(t for t in temps if t > 0) if any(t > 0 for t in temps) else 0.0
                _hset(thermal_c=round(t, 1))
                if t >= cfg["thermal_crit_c"]:
                    log.error(f"THERMAL CRITICAL: {t:.1f}°C — consider throttling")
                    emit_alert(cfg["camera_id"], "thermal", {
                        "temp_c": t, "severity": "high",
                        "label": f"Thermal critical: {t:.1f}°C"
                    })
                elif t >= cfg["thermal_warn_c"]:
                    log.warning(f"Thermal warning: {t:.1f}°C")
            except Exception:
                log.exception("THERMAL MONITOR error — continuing")
            time.sleep(15)

    threading.Thread(target=_run, daemon=True, name="thermal").start()
    log.info("Thermal monitor started")


# ─────────────────────────── ALERT BUS (MQTT) ─────────────────────────────────
_alert_q: queue.Queue = queue.Queue(maxsize=500)
_alert_total = 0

def _mqtt_worker(cfg: dict):
    """MQTT publisher thread with auto-reconnect."""
    global _alert_total
    import paho.mqtt.client as mqtt

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="sentinel_pipeline", protocol=mqtt.MQTTv311)
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    def _on_connect(c, ud, flags, rc):
        if rc == 0:
            _hset(mqtt=True)
            log.info("MQTT connected")
        else:
            _hset(mqtt=False)
            log.warning(f"MQTT connect rc={rc}")

    def _on_disconnect(c, ud, rc):
        _hset(mqtt=False)
        log.warning(f"MQTT disconnected rc={rc}, reconnecting…")

    client.on_connect    = _on_connect
    client.on_disconnect = _on_disconnect

    while True:
        try:
            client.connect(cfg["mqtt_host"], cfg["mqtt_port"], keepalive=60)
            client.loop_start()
            log.info(f"MQTT connecting → {cfg['mqtt_host']}:{cfg['mqtt_port']}")
            break
        except Exception as e:
            log.warning(f"MQTT initial connect failed ({e}) — retry in 5s")
            time.sleep(5)

    while True:
        try:
            payload = _alert_q.get(timeout=2.0)
        except queue.Empty:
            continue

        try:
            msg = json.dumps(payload, default=str)
            result = client.publish(cfg["mqtt_topic"], msg, qos=1)
            _alert_total += 1
            _hset(alerts_total=_alert_total)
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                log.warning(f"MQTT publish error rc={result.rc}")
        except Exception as e:
            log.warning(f"MQTT publish error: {e}")
            # Re-queue if we can
            try:
                _alert_q.put_nowait(payload)
            except queue.Full:
                pass


# ─────────────────────────── NOTIFIER BUS (TELEGRAM) ──────────────────────────
# Dedicated worker thread + bounded queue, mirroring the _mqtt_worker pattern.
# emit_alert dispatches here non-blocking; the worker owns the 150ms snapshot-
# file wait and the blocking HTTP retry path so the main loop is never held.
_notify_q: queue.Queue = queue.Queue(maxsize=CFG["notifier_queue"])
_notifier_queue_drops = 0

# Audio class substrings that map to CRITICAL severity. Keep in lock-step with
# dashboard_static/index.html:878 CRITICAL_AUDIO_CLASSES.
_CRITICAL_AUDIO_CLASSES = ("gunshot", "scream", "glass", "alarm", "siren")


def _severity_of(kind: str, detail: dict) -> str:
    """Map an event to its canonical severity bucket.

    Ported from dashboard_static/index.html:895 severityOf(). Returns the
    uppercase canonical names that telegram_notifier expects
    (CRITICAL/HIGH/MEDIUM/LOW/INFO). The "high→critical" and
    "medium→high" upgrades from detail.severity are preserved verbatim so
    Telegram and dashboard agree on the severity badge for any event; see
    FOLLOWUPS entry on severity-upgrade audit for whether that's right.

    Args:
        kind: event kind string (thermal/audio/detection/loitering/etc.).
        detail: event detail dict.

    Returns:
        One of CRITICAL, HIGH, MEDIUM, LOW, INFO.
    """
    raw = str(detail.get("severity") or "").lower()
    if raw in ("critical", "high"):
        return "CRITICAL"
    if raw == "medium":
        return "HIGH"
    if raw == "low":
        return "LOW"

    k = str(kind or "").lower()
    if k in ("anomaly", "thermal"):
        return "CRITICAL"
    if k == "audio":
        c = str(detail.get("class") or "").lower()
        if any(x in c for x in _CRITICAL_AUDIO_CLASSES):
            return "CRITICAL"
        return "HIGH"
    if k == "loitering":
        return "HIGH"
    if k == "detection":
        return "MEDIUM"
    if k in ("detection_summary", "face_match"):
        return "LOW"
    return "INFO"


def _summary_of(kind: str, detail: dict) -> str:
    """Build a short human-readable summary for a Telegram notification.

    Parallels dashboard_server.py:173 format_event_for_ui() summary rules,
    with explicit thermal and face_match cases that the dashboard currently
    falls through to "Unknown event" (see FOLLOWUPS entry).

    Args:
        kind: event kind string.
        detail: event detail dict.

    Returns:
        One-line summary suitable for a notification body.
    """
    k = str(kind or "").lower()
    if k == "detection_summary":
        return str(detail.get("label") or "Detection summary")
    if k == "detection":
        class_name = str(detail.get("class_name") or "object").replace("_", " ")
        return f"1 {class_name.lower()} detected"
    if k == "loitering":
        zone = detail.get("zone", "unknown")
        return f"Loitering detected in {zone}"
    if k == "audio":
        sound = detail.get("class", "unknown")
        return f"Audio alert: {sound}"
    if k == "anomaly":
        return "Anomaly detected"
    if k == "thermal":
        c = detail.get("celsius") or detail.get("temperature_c")
        try:
            return f"Thermal alert: {float(c):.1f}°C" if c is not None else "Thermal alert"
        except (TypeError, ValueError):
            return "Thermal alert"
    if k == "face_match":
        name = detail.get("name") or detail.get("identity") or "unknown"
        return f"Face match: {name}"
    return f"{k.replace('_', ' ').capitalize()} event"


def _dispatch_notify(
    event_id: int, camera_id: str, kind: str, detail: dict,
    snapshot_path: "str | None",
) -> None:
    """Severity-filter then enqueue a notify job. Non-blocking.

    First-layer severity filter — avoids enqueuing events the notifier would
    drop anyway, keeping the queue depth meaningful. The notifier module
    re-applies the same filter at the module boundary (belt-and-suspenders —
    future callers that bypass emit_alert still get filtered).
    """
    global _notifier_queue_drops
    if telegram_notifier is None or not CFG.get("notifier_enabled", True):
        return
    severity = _severity_of(kind, detail)
    if severity not in telegram_notifier.NOTIFY_LEVELS:
        return
    summary = _summary_of(kind, detail)
    site = str(CFG.get("site_name") or "unknown")
    try:
        _notify_q.put_nowait(
            (severity, site, camera_id, summary, event_id, snapshot_path)
        )
    except queue.Full:
        _notifier_queue_drops += 1
        _hset(notifier_queue_drops=_notifier_queue_drops)
        log.warning(
            "NOTIFIER queue full — dropped event=%d severity=%s",
            event_id, severity,
        )


def _notify_worker(cfg: dict) -> None:
    """Background thread — drain _notify_q into telegram_notifier.notify().

    Owns the 150ms snapshot-file wait so emit_alert stays non-blocking. Each
    enqueued job is (severity, site, camera, summary, event_id, snapshot_path).
    """
    log.info("NOTIFIER worker thread started")
    while True:
        try:
            item = _notify_q.get(timeout=1.0)
        except queue.Empty:
            continue
        try:
            severity, site, camera, summary, event_id, snapshot_path = item

            # Bounded wait for snapshot file to land on disk (Option B from
            # Phase 4 design §5c). Typical readiness is ~3ms; we poll at 10ms
            # for up to 150ms. On timeout, notify text-only so the alert
            # still ships.
            if snapshot_path:
                deadline = time.time() + 0.150
                ready = False
                while time.time() < deadline:
                    if Path(snapshot_path).is_file():
                        ready = True
                        break
                    time.sleep(0.010)
                if not ready:
                    log.info(
                        "NOTIFIER snapshot not ready after 150ms — "
                        "sending text-only event=%d", event_id,
                    )
                    snapshot_path = None

            telegram_notifier.notify(
                severity, site, camera, summary, event_id, snapshot_path,
            )
        except Exception as e:
            log.exception(
                "NOTIFIER worker: unexpected error (continuing): %s", e,
            )


def start_notify_worker(cfg: dict) -> None:
    """Spawn the notifier worker thread if the module and toggle allow it."""
    if telegram_notifier is None:
        log.warning(
            "NOTIFIER worker not started: module unavailable "
            "(see earlier NOTIFIER module log)"
        )
        return
    if not cfg.get("notifier_enabled", True):
        log.info(
            "NOTIFIER disabled via CFG[notifier_enabled]=False — "
            "Telegram alerts off"
        )
        return
    threading.Thread(
        target=_notify_worker, args=(cfg,), daemon=True, name="notifier",
    ).start()


def emit_alert(camera_id: str, kind: str, detail: dict):
    event_id = _next_event_id()
    # Phase 4: best-effort snapshot capture. Never blocks; degrades silently.
    # snapshot_path is the intended destination; the file may not exist yet —
    # the notifier worker polls for it (150ms max) before sending.
    snapshot_path = _snapshot_enqueue(event_id)
    payload = {
        "camera":   camera_id,
        "kind":     kind,
        "ts":       time.time(),
        "label":    detail.get("label", kind),
        "severity": detail.get("severity", "low"),
        **detail,
        "event_id": event_id,  # placed after **detail so callers can't clobber
    }
    try:
        _alert_q.put_nowait(payload)
    except queue.Full:
        log.warning("Alert queue full — dropped")

    # Phase 5: dispatch Telegram notify (non-blocking, severity-filtered).
    _dispatch_notify(event_id, camera_id, kind, detail, snapshot_path)


# ─────────────────────────── YOLO ─────────────────────────────────────────────
def load_yolo(path: str):
    from ultralytics import YOLO
    model = YOLO(path, task="detect")
    dummy = np.zeros((CFG["resize_h"], CFG["resize_w"], 3), dtype=np.uint8)
    for _ in range(3):
        model(dummy, device=DEVICE, verbose=False)
    log.info(f"YOLO loaded: {path}")
    return model


# ─────────────────────────── BYTETRACK ────────────────────────────────────────
def _init_bytetrack():
    try:
        from boxmot.trackers.bytetrack.bytetrack import ByteTrack
    except ImportError as e:
        log.error(f"TRACKING DISABLED — ByteTrack import failed: {e!r}")
        return None
    try:
        tracker = ByteTrack(
            track_thresh=0.45,
            match_thresh=0.8,
            track_buffer=30,
            frame_rate=15,
        )
        log.info("ByteTrack loaded")
        return tracker
    except Exception:
        log.exception("TRACKING DISABLED — ByteTrack construction failed")
        return None


# ─────────────────────────── SCRFD ────────────────────────────────────────────
class SCRFDDetector:
    def __init__(self, model_path: str):
        self._ready = False
        try:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            # Force CPU provider to avoid ORT crash on this Jetson
            self.sess = ort.InferenceSession(
                model_path, opts,
                providers=["CPUExecutionProvider"],
            )
            self.input_name = self.sess.get_inputs()[0].name
            self._ready = True
            log.info("SCRFD loaded (CPU provider)")
        except Exception as e:
            log.warning(f"SCRFD unavailable: {e}")

    def detect(self, img_bgr: np.ndarray):
        if not self._ready:
            return []
        h, w = img_bgr.shape[:2]
        inp  = cv2.resize(img_bgr, (640, 640))
        inp  = (inp.astype(np.float32) - 127.5) / 128.0
        inp  = inp.transpose(2, 0, 1)[None]
        try:
            outs   = self.sess.run(None, {self.input_name: inp})
            scores = outs[0][0]
            bboxes = outs[1][0]
            mask   = scores > CFG["face_conf"]
            sx, sy = w / 640, h / 640
            return [
                (int(b[0]*sx), int(b[1]*sy), int(b[2]*sx), int(b[3]*sy), float(s))
                for s, b in zip(scores[mask], bboxes[mask])
            ]
        except Exception as e:
            log.debug(f"SCRFD run error: {e}")
            return []


# ─────────────────────────── ADAFACE ──────────────────────────────────────────
class AdaFaceEmbedder:
    def __init__(self, model_path: str):
        self._ready = False
        try:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            self.sess = ort.InferenceSession(
                model_path, opts,
                providers=["CPUExecutionProvider"],
            )
            self.input_name = self.sess.get_inputs()[0].name
            self._ready = True
            log.info("AdaFace loaded (CPU provider)")
        except Exception as e:
            log.warning(f"AdaFace unavailable: {e}")

    def embed(self, face_crop_bgr: np.ndarray):
        if not self._ready:
            return None
        face = cv2.resize(face_crop_bgr, (112, 112))
        face = cv2.cvtColor(face, cv2.COLOR_BGR2RGB).astype(np.float32)
        face = (face - 127.5) / 128.0
        face = face.transpose(2, 0, 1)[None]
        try:
            emb  = self.sess.run(None, {self.input_name: face})[0][0]
            emb  = emb / (np.linalg.norm(emb) + 1e-6)
            return emb.astype(np.float32)
        except Exception as e:
            log.debug(f"AdaFace error: {e}")
            return None


# ─────────────────────────── FAISS WATCHLIST ──────────────────────────────────
class WatchlistDB:
    def __init__(self, index_path: str, meta_path: str):
        self._ready = False
        try:
            import faiss
            if os.path.exists(index_path):
                self.index = faiss.read_index(index_path)
                with open(meta_path) as f:
                    self.meta = json.load(f)
                self._ready = True
                log.info(f"Watchlist: {self.index.ntotal} identities")
            else:
                self.index = faiss.IndexFlatIP(512)
                self.meta  = []
                log.info("Watchlist: empty")
        except ImportError:
            log.warning("faiss-cpu not installed — face search disabled")

    def search(self, emb: np.ndarray):
        if not self._ready or self.index.ntotal == 0:
            return None, 0.0
        import faiss
        D, I = self.index.search(emb[None], 1)
        sim, idx = float(D[0][0]), int(I[0][0])
        if idx < 0 or idx >= len(self.meta):
            return None, 0.0
        return self.meta[idx].get("name", f"id_{idx}"), sim


# ─────────────────────────── PATCHCORE ────────────────────────────────────────
class PatchCoreAnomaly:
    def __init__(self, model_path: str):
        self._ready = False
        try:
            if os.path.exists(model_path):
                data = torch.load(model_path, map_location="cpu", weights_only=False)
                self.memory = data["memory_bank"]
                self.mean   = data.get("mean", np.zeros(3))
                self.std    = data.get("std",  np.ones(3))
                self._ready = True
                log.info(f"PatchCore loaded: {self.memory.shape[0]} patches")
            else:
                log.info("PatchCore memory bank not found — run build_patchcore.py first")
        except Exception as e:
            log.warning(f"PatchCore unavailable: {e}")

    def score(self, frame_bgr: np.ndarray) -> float:
        if not self._ready:
            return 0.0
        try:
            import torchvision.transforms.functional as TF
            img = cv2.cvtColor(cv2.resize(frame_bgr, (224, 224)), cv2.COLOR_BGR2RGB)
            t   = torch.from_numpy(img).float().permute(2, 0, 1) / 255.0
            t   = TF.normalize(t, self.mean, self.std).unsqueeze(0)
            with torch.no_grad():
                patches = t.unfold(2, 16, 8).unfold(3, 16, 8)
                patches = patches.contiguous().view(-1, 3*16*16).numpy()
                patches = patches / (np.linalg.norm(patches, axis=1, keepdims=True) + 1e-6)
                mem     = self.memory.numpy()
                dists   = 1 - patches @ mem.T
                return float(min(dists.min(axis=1).max(), 1.0))
        except Exception as e:
            log.debug(f"PatchCore score error: {e}")
            return 0.0


# ─────────────────────────── YAMNET ───────────────────────────────────────────
AUDIO_DANGER_CLASSES = {"Gunshot, gunfire", "Glass break", "Screaming", "Alarm", "Siren"}

class YAMNetAudio:
    def __init__(self, model_path: str):
        self._ready = False
        try:
            import tflite_runtime.interpreter as tflite
            self.interp = tflite.Interpreter(model_path=model_path)
            self.interp.allocate_tensors()
            self._in  = self.interp.get_input_details()[0]
            self._out = self.interp.get_output_details()
            import csv
            self.classes = []
            names_path = os.path.splitext(model_path)[0] + "_classes.csv"
            if os.path.exists(names_path):
                with open(names_path) as f:
                    for row in csv.reader(f):
                        if row: self.classes.append(row[-1].strip())
            self._ready = True
            log.info("YAMNet loaded")
        except Exception as e:
            log.warning(f"YAMNet unavailable: {e}")

    def classify(self, waveform: np.ndarray):
        if not self._ready:
            return None, 0.0
        try:
            self.interp.set_tensor(self._in["index"], waveform.reshape(self._in["shape"]))
            self.interp.invoke()
            scores     = self.interp.get_tensor(self._out[0]["index"])
            mean_scores = scores.mean(axis=0)
            idx        = int(mean_scores.argmax())
            name       = self.classes[idx] if idx < len(self.classes) else f"class_{idx}"
            return name, float(mean_scores[idx])
        except Exception as e:
            log.debug(f"YAMNet error: {e}")
            return None, 0.0

def start_audio_thread(yamnet: YAMNetAudio, camera_id: str, cfg: dict):
    def _run():
        try:
            import sounddevice as sd
            sr     = cfg["audio_sample_rate"]
            window = int(sr * cfg["audio_window_ms"] / 1000)

            def _cb(indata, frames, ts, status):
                wav  = indata[:, 0].astype(np.float32)
                name, conf = yamnet.classify(wav)
                if name in AUDIO_DANGER_CLASSES and conf > cfg["audio_conf"]:
                    emit_alert(camera_id, "audio", {
                        "class": name, "confidence": round(conf, 3),
                        "label": f"Audio: {name}",
                        "severity": "high",
                    })
                    log.warning(f"Audio alert: {name} ({conf:.2f})")

            with sd.InputStream(samplerate=sr, channels=1, blocksize=window,
                                callback=_cb, dtype="float32"):
                log.info("Audio monitoring started")
                while True:
                    time.sleep(1)
        except Exception as e:
            log.warning(f"Audio thread error: {e}")

    threading.Thread(target=_run, daemon=True, name="audio").start()


# ─────────────────────────── ZONE ENGINE ──────────────────────────────────────
class ZoneEngine:
    def __init__(self, zones: list):
        self.zones = []
        for z in zones:
            poly = np.array(z["polygon"], dtype=np.int32)
            self.zones.append({
                "name": z["name"], "poly": poly,
                "dwell_sec": z["dwell_sec"], "dwell": {},
            })

    def update(self, track_id: int, cx: int, cy: int, camera_id: str):
        now = time.time()
        for z in self.zones:
            inside = cv2.pointPolygonTest(z["poly"], (cx, cy), False) >= 0
            if inside:
                entered = z["dwell"].setdefault(track_id, now)
                if now - entered >= z["dwell_sec"]:
                    emit_alert(camera_id, "loitering", {
                        "zone":      z["name"],
                        "track_id":  track_id,
                        "dwell_sec": round(now - entered, 1),
                        "label":     f"Loitering in {z['name']}",
                        "severity":  "medium",
                    })
                    z["dwell"][track_id] = now
            else:
                z["dwell"].pop(track_id, None)


# ─────────────────────────── CAMERA HELPERS ───────────────────────────────────
def open_camera(url: str) -> cv2.VideoCapture:
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    cap.set(cv2.CAP_PROP_FPS, 30)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open: {url}")
    _hset(camera=True)
    return cap

def reopen_camera(url: str, delay: float) -> cv2.VideoCapture:
    _hset(camera=False)
    while True:
        try:
            return open_camera(url)
        except Exception as e:
            log.warning(f"Reconnect failed ({e}) — retry in {delay}s")
            time.sleep(delay)


# ─────────────────────────── DRAW HELPERS ─────────────────────────────────────
_COLORS = [(0,0,255),(0,255,0),(255,128,0),(0,255,255),(255,0,255),(200,0,200)]

def _color(track_id: int):
    return _COLORS[track_id % len(_COLORS)]

COCO_NAMES = [
    "person","bicycle","car","motorcycle","airplane","bus","train","truck","boat",
    "traffic light","fire hydrant","stop sign","parking meter","bench","bird","cat",
    "dog","horse","sheep","cow","elephant","bear","zebra","giraffe","backpack",
    "umbrella","handbag","tie","suitcase","frisbee","skis","snowboard","sports ball",
    "kite","baseball bat","baseball glove","skateboard","surfboard","tennis racket",
    "bottle","wine glass","cup","fork","knife","spoon","bowl","banana","apple",
    "sandwich","orange","broccoli","carrot","hot dog","pizza","donut","cake","chair",
    "couch","potted plant","bed","dining table","toilet","tv","laptop","mouse",
    "remote","keyboard","cell phone","microwave","oven","toaster","sink",
    "refrigerator","book","clock","vase","scissors","teddy bear","hair drier",
    "toothbrush",
]

def iou_xyxy(a, b):
    ax1,ay1,ax2,ay2 = a; bx1,by1,bx2,by2 = b
    ix1 = max(ax1,bx1); iy1 = max(ay1,by1)
    ix2 = min(ax2,bx2); iy2 = min(ay2,by2)
    inter = max(0,ix2-ix1)*max(0,iy2-iy1)
    ua = max(0,ax2-ax1)*max(0,ay2-ay1)
    ub = max(0,bx2-bx1)*max(0,by2-by1)
    return inter / (ua+ub-inter+1e-6)

def draw_tracks(frame, tracks, behavior_labels=None):
    if tracks is None or len(tracks) == 0:
        return
    for t in tracks:
        x1,y1,x2,y2 = int(t[0]),int(t[1]),int(t[2]),int(t[3])
        tid  = int(t[4])
        cls  = int(t[6]) if len(t) > 6 else -1
        conf = float(t[5]) if len(t) > 5 else 0.0
        c    = _color(tid)
        name = COCO_NAMES[cls] if 0 <= cls < len(COCO_NAMES) else "?"

        behavior = None
        if isinstance(behavior_labels, dict):
            behavior = behavior_labels.get(tid)

        if behavior:
            lbl = _clean_behavior_label(f"{behavior}")
        else:
            lbl = _clean_behavior_label(f"{name} #{tid} {conf:.0%}")

        cv2.rectangle(frame, (x1,y1), (x2,y2), c, 2)
        cv2.putText(frame, lbl, (x1, y1-6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, c, 1, cv2.LINE_AA)

def draw_faces(frame, faces, labels):
    for (x1,y1,x2,y2,_), label in zip(faces, labels):
        cv2.rectangle(frame, (x1,y1), (x2,y2), (255,200,0), 2)
        # AUTO_CLEAN_LABEL
        try:
            label = _clean_behavior_label(_clean_behavior_label(label))
        except Exception:
            pass
        cv2.putText(frame, label, (x1, y1-6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255,200,0), 1, cv2.LINE_AA)

def draw_anomaly(frame, score: float):
    if score > CFG["anomaly_thresh"]:
        cv2.putText(frame, f"ANOMALY {score:.2f}", (10,30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0,0,255), 2, cv2.LINE_AA)

def draw_hud(frame, fps: float, n_tracks: int, thermal: float):
    ts  = time.strftime("%Y-%m-%d %H:%M:%S")
    txt = f"{ts}  |  {fps:.1f} fps  |  {n_tracks} tracks  |  {thermal:.0f}°C"
    cv2.putText(frame, txt, (4, frame.shape[0]-8),
                cv2.FONT_HERSHEY_SIMPLEX, 0.38, (180,180,180), 1, cv2.LINE_AA)


# ─────────────────────────── MAIN LOOP ────────────────────────────────────────
def run(cfg: dict):
    # ── Start support services ─────────────────────────────────────────────────
    threading.Thread(target=_mqtt_worker, args=(cfg,), daemon=True, name="mqtt").start()
    start_mjpeg(cfg["mjpeg_port"])
    start_health(cfg["health_port"])
    start_thermal_monitor(cfg)
    _setup_snapshot_dir(cfg)
    start_snapshot_writer(cfg)
    start_notify_worker(cfg)

    # ── Load models ────────────────────────────────────────────────────────────
    yolo      = load_yolo(cfg["yolo_engine"])
    tracker   = _init_bytetrack()

    # Behavior analyzer (graceful degradation if pose model not present)
    beh_analyzer = None
    try:
        from behavior import BehaviorAnalyzer, draw_behavior
        beh_analyzer = BehaviorAnalyzer(cfg["pose_engine"])
        beh_analyzer.start_thread(emit_alert, cfg["camera_id"])
        _hset(behavior_ready=True)
        log.info("Behavior analyzer ready")
    except Exception as e:
        log.warning(f"Behavior analyzer unavailable: {e}")

    face_det  = None    # SCRFDDetector(cfg["scrfd_model"])  — re-enable when stable
    face_emb  = None    # AdaFaceEmbedder(cfg["adaface_model"])
    watchlist = None
    patchcore = None
    yamnet    = None
    zones     = ZoneEngine(cfg["zones"])

    cam = open_camera(cfg["rtsp_url"])

    running        = True
    n_frame        = 0
    last_infer     = 0.0
    infer_gap      = 1.0 / max(cfg["inference_fps"], 1.0)
    last_tracks    = np.empty((0, 6))
    last_faces:    list = []
    last_labels:   list = []
    last_anomaly   = 0.0
    last_summary_counts: dict = {}
    last_summary_ts = 0.0

    # FPS rolling window
    fps_window: collections.deque = collections.deque(maxlen=30)
    last_fps_ts = time.time()

    # Alert dedup
    alert_cooldown_sec = cfg["alert_cooldown_sec"]
    object_ttl_sec     = cfg["object_ttl_sec"]
    next_object_id     = 1
    active_objects:    dict = {}

    def _stop(sig, _):
        nonlocal running
        running = False
        log.info(f"Shutdown signal {sig} — draining…")

    signal.signal(signal.SIGINT,  _stop)
    signal.signal(signal.SIGTERM, _stop)

    _hset(status="running")
    log.info("═══ Pipeline running ═══")

    try:
        while running:
            # Drop stale frames for low latency
            for _ in range(3):
                cam.grab()

            ret, raw = cam.read()
            if not ret or raw is None:
                if str(cfg["rtsp_url"]).endswith((".mp4",".webm",".avi",".mkv")):
                    log.info("End of file → looping")
                    cam.release()
                    cam = open_camera(cfg["rtsp_url"])
                    continue
                log.warning("Camera read failed — reconnecting")
                cam.release()
                cam = reopen_camera(cfg["rtsp_url"], cfg["reconnect_delay"])
                continue

            frame  = cv2.resize(raw, (cfg["resize_w"], cfg["resize_h"]))
            n_frame += 1
            now = time.time()

            # ── FPS calculation ────────────────────────────────────────────────
            fps_window.append(now)
            if len(fps_window) >= 2:
                fps = (len(fps_window) - 1) / (fps_window[-1] - fps_window[0] + 1e-6)
            else:
                fps = 0.0
            if now - last_fps_ts > 5.0:
                _hset(fps=round(fps, 1), frames_total=n_frame)
                last_fps_ts = now

            # ── DETECTION + TRACKING ───────────────────────────────────────────
            if now - last_infer >= infer_gap:
                results = yolo(
                    frame,
                    conf=cfg["yolo_conf"],
                    device=DEVICE,
                    verbose=False,
                )
                last_infer = now

                dets = []
                for r in results:
                    if r.boxes is None:
                        continue
                    for b in r.boxes:
                        x1,y1,x2,y2 = b.xyxy[0].tolist()
                        cls  = int(b.cls[0]) if b.cls is not None else 0
                        if cls not in ALLOWED_CLASSES:
                            continue
                        dets.append([x1,y1,x2,y2, float(b.conf[0]), cls])

                dets = np.array(dets, dtype=np.float32) if dets else np.empty((0,6))

                if tracker is not None:
                    last_tracks = tracker.update(dets, frame)
                else:
                    last_tracks = dets

                # ── Behavior submit (every Nth frame) ──────────────────────────
                if beh_analyzer and n_frame % cfg["behavior_every_n"] == 0 and len(last_tracks):
                    beh_analyzer.submit(frame, last_tracks, now)

                # ── Scene summary (human-readable) ────────────────────────────
                class_counts: dict = {}
                for t in last_tracks:
                    cls = int(t[6]) if (tracker and len(t)>6) else int(t[5]) if len(t)>5 else -1
                    if 0 <= cls < len(COCO_NAMES):
                        name = COCO_NAMES[cls]
                        class_counts[name] = class_counts.get(name, 0) + 1

                if class_counts != last_summary_counts and \
                   now - last_summary_ts >= cfg["summary_min_gap"]:
                    parts = []
                    for name, count in class_counts.items():
                        parts.append(f"{count} {'people' if (name=='person' and count>1) else name}{'s' if (name!='person' and count>1) else ''}")
                    emit_alert(cfg["camera_id"], "detection_summary", {
                        "counts": class_counts,
                        "label":  ", ".join(parts) + " detected",
                        "severity": "medium",
                    })
                    last_summary_counts = dict(class_counts)
                    last_summary_ts = now

                # ── Prune stale object state ───────────────────────────────────
                stale = [k for k,v in active_objects.items()
                         if now - v["last_seen"] > object_ttl_sec]
                for k in stale:
                    active_objects.pop(k, None)

                # ── Per-object events with dedup ───────────────────────────────
                for t in last_tracks:
                    x1,y1,x2,y2 = int(t[0]),int(t[1]),int(t[2]),int(t[3])
                    bbox = [x1,y1,x2,y2]

                    if tracker is not None and len(t) > 6:
                        track_id = int(t[4]); conf = float(t[5]); cls = int(t[6])
                        object_key = f"trk:{track_id}:{cls}"
                    else:
                        conf = float(t[4]) if len(t)>4 else 0.0
                        cls  = int(t[5])   if len(t)>5 else -1
                        track_id = -1
                        matched_key = None; best_iou = 0.0
                        for k, v in active_objects.items():
                            if v["cls"] != cls: continue
                            s = iou_xyxy(bbox, v["bbox"])
                            if s > 0.50 and s > best_iou:
                                matched_key = k; best_iou = s
                        object_key = matched_key or f"obj:{next_object_id}:{cls}"
                        if not matched_key:
                            next_object_id += 1

                    cx = int((x1+x2)/2); cy = int((y1+y2)/2)
                    zones.update(track_id if track_id >= 0 else 0, cx, cy, cfg["camera_id"])

                    if not (0 <= cls < len(COCO_NAMES)):
                        continue
                    class_name = COCO_NAMES[cls]

                    info   = active_objects.get(object_key)
                    is_new = info is None
                    if is_new:
                        info = {"cls":cls,"bbox":bbox,"last_seen":now,"last_alert":0.0}
                        active_objects[object_key] = info

                    info["bbox"] = bbox; info["last_seen"] = now

                    if is_new or (now - info["last_alert"] >= alert_cooldown_sec):
                        emit_alert(cfg["camera_id"], "detection", {
                            "class_name": class_name,
                            "confidence": round(conf, 3),
                            "bbox":       bbox,
                            "track_id":   track_id if track_id >= 0 else None,
                            "label":      f"{class_name} detected",
                            "severity":   "medium" if class_name == "person" else "low",
                        })
                        info["last_alert"] = now

            # ── FACE (disabled until ORT is stable on this Jetson) ─────────────
            # if face_det and n_frame % cfg["face_every_n"] == 0: ...

            # ── ANOMALY (disabled until patchcore is built) ────────────────────
            # if patchcore and n_frame % cfg["anomaly_every_n"] == 0: ...

            # ── ANNOTATE + STREAM ──────────────────────────────────────────────
            vis = frame.copy()
            beh_labels = beh_analyzer.get_labels() if beh_analyzer else {}
            draw_tracks(vis, last_tracks, beh_labels)
            if beh_analyzer:
                try:
                    from behavior import draw_behavior
                    draw_behavior(vis, last_tracks, beh_labels)
                except Exception:
                    pass
            draw_faces(vis, last_faces, last_labels)
            draw_anomaly(vis, last_anomaly)
            draw_hud(vis, fps, len(last_tracks), _health.get("thermal_c", 0.0))

            # Publish the fully-annotated frame for snapshot capture.
            # Atomic reference rebind under the GIL; consumers .copy() before use.
            global _snapshot_frame
            _snapshot_frame = vis

            ok, jpg = cv2.imencode(
                ".jpg", vis,
                [cv2.IMWRITE_JPEG_QUALITY, cfg["mjpeg_quality"]],
            )
            if ok:
                _frame_bus.put(jpg.tobytes())

            time.sleep(0.001)

    finally:
        cam.release()
        if beh_analyzer:
            beh_analyzer.stop()
        _hset(status="stopped")
        log.info(
            f"Pipeline stopped — frames={n_frame} alerts={_health['alerts_total']}"
            f" uptime={_health['uptime_s']}s"
        )


def main():
    run(CFG)

if __name__ == "__main__":
    main()
