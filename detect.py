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
import sqlite3
import sys
from datetime import datetime
from typing import Optional

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

from rules import RuleContext, RulesEngine, TrackedObject
from rules.forbidden_zone import ForbiddenZoneRule
from rules.loitering import LoiteringRule
from rules.predicted_intrusion import PredictedIntrusionRule

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
    # yolo_conf lowered 0.55 → 0.40 in Session 9 to catch small objects
    # (cell phones at 640×360 inference are typically 20–40px wide and
    # below YOLO's natural confidence at 0.55). Watch for false-positive
    # noise on detection / detection_summary kinds; revert if it spikes.
    "yolo_conf":       0.40,
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

    # Scene-summary cadence (per-object cooldown removed in Session 5;
    # dedup now lives in the central decision pipeline — see "dedup" key).
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

    # ── Operating-mode schedule (Session 5) ───────────────────────
    # ISO weekday: 1=Mon ... 7=Sun. Hours are local time (system tz).
    # Weekdays absent from occupied_hours → CLOSED all day.
    # holidays: list of "YYYY-MM-DD" strings; CLOSED override.
    # maintenance: True forces MAINTENANCE mode globally. Operator-set
    #   today (CFG edit + restart). TODO admin-UI toggle in a later session.
    "schedule": {
        "occupied_hours": {
            1: ("06:00", "20:00"),
            2: ("06:00", "20:00"),
            3: ("06:00", "20:00"),
            4: ("06:00", "20:00"),
            5: ("06:00", "20:00"),
        },
        "holidays": [],
        "maintenance": False,
    },

    # ── Severity table (Session 5) ────────────────────────────────
    # Maps "<kind>" or "<kind>:<subtype>" → {mode: severity}.
    # PUBLIC zone only this session — every event treated as zone="main".
    "severity_table": {
        "detection":          {"OCCUPIED": "MEDIUM",   "CLOSED": "HIGH",     "MAINTENANCE": "INFO"},
        "detection_summary":  {"OCCUPIED": "INFO",     "CLOSED": "LOW",      "MAINTENANCE": "INFO"},
        "loitering":          {"OCCUPIED": "LOW",      "CLOSED": "HIGH",     "MAINTENANCE": "LOW"},
        "face_match:known":   {"OCCUPIED": "INFO",     "CLOSED": "LOW",      "MAINTENANCE": "INFO"},
        "face_match:unknown": {"OCCUPIED": "LOW",      "CLOSED": "HIGH",     "MAINTENANCE": "LOW"},
        "face_match:banned":  {"OCCUPIED": "CRITICAL", "CLOSED": "CRITICAL", "MAINTENANCE": "CRITICAL"},
        "audio:critical":     {"OCCUPIED": "CRITICAL", "CLOSED": "CRITICAL", "MAINTENANCE": "CRITICAL"},
        "audio":              {"OCCUPIED": "LOW",      "CLOSED": "MEDIUM",   "MAINTENANCE": "LOW"},
        "thermal":            {"OCCUPIED": "HIGH",     "CLOSED": "HIGH",     "MAINTENANCE": "HIGH"},
        "thermal:fire":       {"OCCUPIED": "CRITICAL", "CLOSED": "CRITICAL", "MAINTENANCE": "CRITICAL"},
        "anomaly":            {"OCCUPIED": "HIGH",     "CLOSED": "CRITICAL", "MAINTENANCE": "HIGH"},
        # Behavior subtypes — pinned LOW × OCCUPIED until classifier
        # mislabeling is addressed (see FOLLOWUPS entry on classifier).
        # CLOSED bumps the alarming subtypes to MEDIUM so they reach
        # Telegram at night.
        "behavior":           {"OCCUPIED": "INFO",     "CLOSED": "LOW",      "MAINTENANCE": "INFO"},
        "behavior:fallen":    {"OCCUPIED": "LOW",      "CLOSED": "MEDIUM",   "MAINTENANCE": "LOW"},
        "behavior:fighting":  {"OCCUPIED": "LOW",      "CLOSED": "MEDIUM",   "MAINTENANCE": "LOW"},
        "behavior:running":   {"OCCUPIED": "LOW",      "CLOSED": "LOW",      "MAINTENANCE": "LOW"},
        # Operator-defined forbidden zones — person inside any saved
        # polygon. Operator brief: CRITICAL all modes.
        "forbidden_zone":     {"OCCUPIED": "CRITICAL", "CLOSED": "CRITICAL", "MAINTENANCE": "CRITICAL"},
        # Predicted intrusion — extrapolated trajectory enters a forbidden
        # zone before the person physically does. Designed as an
        # *early warning*: HIGH during business hours (telegram, but
        # one rung below the actual breach), CRITICAL after-hours
        # (treat as imminent threat). Suppressed during MAINTENANCE
        # because trajectory data is unreliable when staff is moving
        # through zones for legitimate work.
        "predicted_intrusion": {"OCCUPIED": "HIGH",     "CLOSED": "CRITICAL", "MAINTENANCE": "INFO"},
        # Phone-use detected via YOLO cell-phone class (67) overlapping a
        # person bbox. Bumped MEDIUM → HIGH in Session 9 follow-up — at
        # MEDIUM the 180-second track-window dedup made the alerts feel
        # random ("got a ping then 3 minutes of silence even though I'm
        # still on the phone"). HIGH gets a 60-second window which feels
        # responsive without Telegram-spamming. Routing unchanged for
        # OCCUPIED/CLOSED (still telegram); MAINTENANCE upgrades from
        # suppressed → dashboard so it's visible during maintenance windows.
        "phone_use":          {"OCCUPIED": "HIGH",     "CLOSED": "HIGH",     "MAINTENANCE": "LOW"},
    },

    # ── Notification thresholds (Session 5) ───────────────────────
    # MAINTENANCE column suppresses everything except CRITICAL (HIGH still
    # writes to dashboard so operators can review post-window).
    "thresholds": {
        "CRITICAL": {"OCCUPIED": "telegram",   "CLOSED": "telegram",   "MAINTENANCE": "telegram"},
        "HIGH":     {"OCCUPIED": "telegram",   "CLOSED": "telegram",   "MAINTENANCE": "dashboard"},
        "MEDIUM":   {"OCCUPIED": "telegram",   "CLOSED": "telegram",   "MAINTENANCE": "suppressed"},
        "LOW":      {"OCCUPIED": "dashboard",  "CLOSED": "dashboard",  "MAINTENANCE": "suppressed"},
        "INFO":     {"OCCUPIED": "suppressed", "CLOSED": "dashboard",  "MAINTENANCE": "suppressed"},
    },

    # ── Per-kind alert allowlist (Phase 0b) ───────────────────────
    # Operator gate that constrains which event kinds surface to the
    # operator, independent of the severity table. Three modes:
    #   None   — no allowlist; severity table alone drives routing.
    #   []     — empty list silences EVERY kind (audit-only mode).
    #   [...]  — list of kind strings; only these reach events row,
    #            Telegram, and dashboard. Everything else writes an
    #            audit row (decision='suppressed_threshold',
    #            reason_detail='not_in_allowlist') and short-circuits.
    #
    # Legacy "alerts_only_forbidden_zone": True is still honoured for
    # backward compat — translates to ["forbidden_zone"] at startup
    # with a one-time deprecation log line. Mixing both raises at
    # import time so misconfigurations are loud, not subtle.
    "alert_kind_allowlist": ["forbidden_zone", "phone_use", "predicted_intrusion"],

    # ── Phone-use detection ────────────────────────────────────────
    # phone_use_containment is the PRODUCTION metric: fraction of the
    # phone bbox that overlaps the person bbox. ~1.0 when a phone is
    # held in front of the body; near 0 when the phone is across the
    # room from the person. IoU is unusable here because the phone is
    # geometrically tiny next to a person (~1% of area) — IoU caps at
    # ~0.01 even with full containment.
    #
    # ── Predicted-intrusion (Phase 2) ──────────────────────────────
    # Velocity-extrapolated trajectory crossing into a forbidden zone
    # before the person physically does. predict_horizon_s is how far
    # ahead we project; predict_min_frames is how much history we need
    # before we trust the velocity estimate; predict_min_velocity_px_s
    # gates the rule on actual movement (stationary people don't
    # generate predictions). predict_breach_consecutive demands N
    # straight frames of predicted breach before firing — kills the
    # one-frame-noise false alerts. predict_track_ttl_s is when stale
    # track history is purged from the rule's local dict.
    "predict_horizon_s":             2.5,
    "predict_min_frames":            5,
    "predict_min_velocity_px_s":     30.0,
    "predict_breach_consecutive":    3,
    "predict_track_ttl_s":           3.0,

    # Default 0.3 = "at least 30% of the phone overlaps the (padded)
    # person bbox." Loose enough to handle bbox jitter when the phone
    # is held at chest level; tighten toward 0.5+ if real warehouse
    # traffic shows false positives.
    "phone_use_containment":  0.3,
    # Person bbox padding (pixels). YOLO's person bbox covers the
    # torso/legs cleanly but often clips arms extended forward — so a
    # phone held up at chest level lands just OUTSIDE the bare bbox.
    # Padding by N pixels in every direction reclaims that area.
    # 40 px @ 640×360 inference ≈ a fully-extended forearm. Bump to
    # 60-80 if the user's arm reach extends further than that.
    "phone_use_person_pad_px":  40,

    # ── Three-layer dedup windows (Session 5, Telegram-bound only) ──
    "dedup": {
        "track_window_s":  300,    # 5 min — flat fallback (camera, track_id, kind)
        "zone_window_s":   120,    # 2 min — flat fallback (camera, zone, kind)
        "burst_window_s":   30,    # rolling burst window
        "burst_threshold":   3,    # N within burst_window_s → fire summary

        # Severity-aware overrides (Phase 0a). When a severity has an entry
        # here it wins over the flat *_window_s fallback above; absent
        # severities use the flat default. Real-intruder rule-of-thumb:
        # CRITICAL must re-fire fast enough that a lingering trespasser
        # produces multiple alerts, not one.
        "track_window_by_severity_s": {
            "CRITICAL":  30,
            "HIGH":      60,
            "MEDIUM":   180,
            "LOW":      300,
            "INFO":     300,
        },
        "zone_window_by_severity_s": {
            "CRITICAL":  20,
            "HIGH":      60,
            "MEDIUM":   120,
            "LOW":      120,
            "INFO":     120,
        },

        # Track-loss re-fire (Phase 0a). When the tracker drops a track that
        # previously fired, retire its dedup entries so a new track for the
        # same logical situation can re-alert without waiting out the full
        # window. `track_idle_s` is how long we tolerate a track being
        # absent before declaring it lost. `clear_zone_on_track_loss` also
        # clears the (camera, zone, kind) zone entry on retirement; False
        # keeps the zone window as a backstop against rapid re-fire churn.
        "track_idle_s":             5.0,
        "clear_zone_on_track_loss": True,
    },
}

ALLOWED_CLASSES = {0, 2, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 67}  # 67 = cell phone (phone_use)

# ─────────────────────────── LOGGING ──────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)-12s] %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("pipeline")


# ─────────────────────────── ALLOWLIST LEGACY TRANSLATION ────────────────────
# Honour the deprecated alerts_only_forbidden_zone flag if it's still set.
# Conflicts (legacy key set AND new allowlist supplied) are noisy on
# purpose — silently picking one would hide a real misconfiguration.
def _resolve_alert_kind_allowlist(cfg: dict) -> "Optional[list]":
    """Return the effective alert_kind_allowlist for the given cfg.

    Reads ``cfg["alert_kind_allowlist"]`` and the legacy
    ``cfg["alerts_only_forbidden_zone"]`` flag, logs a deprecation
    one-shot if the legacy flag is in use, and raises if both keys
    are set in conflicting ways.

    Returns:
        None to disable the gate, or a list of allowed kinds (possibly
        empty to silence everything).
    """
    legacy = cfg.get("alerts_only_forbidden_zone")
    new = cfg.get("alert_kind_allowlist", "__sentinel__")

    if legacy is None and new == "__sentinel__":
        return None  # neither set — gate disabled.

    if legacy is None:
        return None if new is None else list(new)

    # Legacy key is set. Translate True/False; refuse to silently merge.
    if new != "__sentinel__":
        # New key explicitly present — only fine if the legacy flag is
        # False (no-op) or the new value matches the legacy's translation.
        translated = ["forbidden_zone"] if legacy else None
        if new == translated:
            log.warning(
                "DEPRECATED: alerts_only_forbidden_zone=%s redundantly "
                "duplicates alert_kind_allowlist=%r — drop the legacy "
                "key from CFG", legacy, new,
            )
            return None if new is None else list(new)
        raise ValueError(
            "CFG has BOTH alerts_only_forbidden_zone=%r and "
            "alert_kind_allowlist=%r and they disagree. Drop the "
            "legacy flag — alert_kind_allowlist is authoritative."
            % (legacy, new)
        )

    log.warning(
        "DEPRECATED: alerts_only_forbidden_zone=%s — translating to "
        "alert_kind_allowlist=%r. Update CFG to drop the legacy key.",
        legacy, ["forbidden_zone"] if legacy else None,
    )
    return ["forbidden_zone"] if legacy else None


# Module-level cache, set on first call. emit_alert reads this rather
# than re-parsing CFG every event.
_alert_kind_allowlist: "Optional[list]" = _resolve_alert_kind_allowlist(CFG)


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
    # Session 5 — mode + audit observability (computed live in /health handler)
    "current_mode":                  "UNKNOWN",
    "alerts_fired_last_hour":        0,
    "alerts_suppressed_last_hour":   {},
    "audit_log_size":                0,
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
                # Session 5 — mode + audit observability.
                try:
                    _health["current_mode"] = resolve_mode(CFG)
                except Exception as e:
                    _health["current_mode"] = "ERROR"
                    log.warning("HEALTH resolve_mode failed: %s", e)
                fired, suppressed, total = _audit_count_since(3600)
                _health["alerts_fired_last_hour"]      = fired
                _health["alerts_suppressed_last_hour"] = suppressed
                _health["audit_log_size"]              = total
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


# ─────────────────────────── REPLAY MODE STATE (Session 8) ────────────────────
# When detect.py is invoked with --replay PATH, the pipeline reads from the
# given MP4 instead of RTSP, skips every network-bound side effect (MQTT
# broker, MJPEG server, /health server, snapshot writer, Telegram notifier),
# and captures every emit_alert decision into _replay_capture for later
# JSON dump. Tests subprocess-launch detect.py with these flags and assert
# against the JSON output. See tests/test_replay.py.

_replay_capture: "Optional[list]" = None  # accumulator; None means not in replay

# Per-frame action labels captured by the main loop when --emit-actions is set.
# Each entry: {"frame": n, "ts": float, "labels": {track_id: {action,
# next_action, track_id}}}. Used by tests to assert classifier behaviour over
# time without reading the audit DB.
_replay_action_capture: "Optional[list]" = None


# ─────────────────────────── MODE RESOLUTION (Session 5) ──────────────────────
# OCCUPIED / CLOSED / MAINTENANCE — resolved at decision time from CFG schedule
# and the system clock. Pure: never reads global state, never caches.

_VALID_MODES = ("OCCUPIED", "CLOSED", "MAINTENANCE")


def _parse_hhmm(s: str) -> "tuple[int, int]":
    """Parse 'HH:MM' → (hour, minute). Raises ValueError on malformed input."""
    parts = s.split(":")
    if len(parts) != 2:
        raise ValueError(f"expected HH:MM, got {s!r}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 24 and 0 <= m <= 59):
        raise ValueError(f"out-of-range time {s!r}")
    return h, m


def _validate_schedule(cfg: dict) -> None:
    """Fail loud at startup on schedule misconfiguration.

    Better to crash than to silently run in the wrong mode. Called once
    from run() before any worker thread spawns.
    """
    sched = cfg.get("schedule")
    if not isinstance(sched, dict):
        raise ValueError("CFG['schedule'] missing or not a dict")
    occ = sched.get("occupied_hours", {})
    if not isinstance(occ, dict):
        raise ValueError("CFG['schedule']['occupied_hours'] must be a dict")
    for wd, window in occ.items():
        if not (isinstance(wd, int) and 1 <= wd <= 7):
            raise ValueError(
                f"occupied_hours weekday must be int 1..7, got {wd!r}"
            )
        if not (isinstance(window, (list, tuple)) and len(window) == 2):
            raise ValueError(
                f"occupied_hours[{wd}] must be (start, end) pair, got {window!r}"
            )
        start_h, start_m = _parse_hhmm(str(window[0]))
        end_h, end_m = _parse_hhmm(str(window[1]))
        # Allow end <= start (zero-length or wraparound) — operator may use
        # ("00:00", "00:00") to flip a weekday into CLOSED for foreground tests.
        _ = (start_h, start_m, end_h, end_m)
    holidays = sched.get("holidays", [])
    if not isinstance(holidays, list):
        raise ValueError("CFG['schedule']['holidays'] must be a list")
    for d in holidays:
        try:
            datetime.strptime(str(d), "%Y-%m-%d")
        except ValueError as e:
            raise ValueError(f"holiday {d!r} not in YYYY-MM-DD format: {e}")
    if not isinstance(sched.get("maintenance", False), bool):
        raise ValueError("CFG['schedule']['maintenance'] must be bool")


def resolve_mode(cfg: dict, now: Optional[datetime] = None) -> str:
    """Return current operating mode: OCCUPIED, CLOSED, or MAINTENANCE.

    Precedence: maintenance flag > holiday > weekday window > CLOSED.

    Args:
        cfg: pipeline config dict (must contain a validated 'schedule').
        now: datetime to evaluate against; defaults to local now().

    Returns:
        One of OCCUPIED, CLOSED, MAINTENANCE.
    """
    # Replay/test override — bypasses schedule + maintenance flag so tests
    # are deterministic regardless of when they run.
    forced = cfg.get("replay_force_mode")
    if forced in _VALID_MODES:
        return forced

    sched = cfg["schedule"]
    if sched.get("maintenance", False):
        return "MAINTENANCE"
    if now is None:
        now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    if today_str in sched.get("holidays", []):
        return "CLOSED"
    weekday = now.isoweekday()  # 1..7
    window = sched.get("occupied_hours", {}).get(weekday)
    if not window:
        return "CLOSED"
    start_h, start_m = _parse_hhmm(str(window[0]))
    end_h, end_m = _parse_hhmm(str(window[1]))
    minute_of_day = now.hour * 60 + now.minute
    start = start_h * 60 + start_m
    end = end_h * 60 + end_m
    if start == end:
        return "CLOSED"  # zero-length window → effectively CLOSED
    if start < end:
        return "OCCUPIED" if start <= minute_of_day < end else "CLOSED"
    # Wraparound (e.g., night shift): OCCUPIED if either side of midnight.
    return "OCCUPIED" if (minute_of_day >= start or minute_of_day < end) else "CLOSED"


# ─────────────────────────── SEVERITY + DECISION (Session 5) ──────────────────
# Pure functions. Driven by CFG tables; no hidden constants.

_SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")
_DECISIONS = ("telegram", "dashboard", "suppressed")

# Logged-once-per-process so an unmapped kind doesn't spam.
_severity_unknown_seen: set = set()
_threshold_unknown_seen: set = set()


def _subtype_of(kind: str, detail: dict) -> str:
    """Derive severity-table subtype from event kind + detail.

    Returns "" when the kind has no sub-classification. Keeps callers
    free of the table's lookup convention — they keep emitting their
    natural detail keys (e.g., audio class names).
    """
    k = (kind or "").lower()
    if k == "audio":
        c = str(detail.get("class") or "").lower()
        if any(x in c for x in _CRITICAL_AUDIO_CLASSES):
            return "critical"
        return ""
    if k == "face_match":
        mk = str(detail.get("match_kind") or "").lower()
        if mk in ("banned", "known", "unknown"):
            return mk
        return ""
    if k == "thermal":
        # No fire classifier yet; keep entry forward-compatible.
        if str(detail.get("fire") or "").lower() in ("1", "true", "yes"):
            return "fire"
        return ""
    if k == "behavior":
        # Subtype is the pose-classifier action label. Map only the
        # alarming actions to severity-table rows; anything else
        # (standing, walking, unknown) falls through to the bare
        # "behavior" row → INFO/OCCUPIED → suppressed.
        action = str(detail.get("action") or "").lower()
        if action in ("fallen", "fighting", "running"):
            return action
        return ""
    return ""


def compute_severity(
    kind: str,
    detail: dict,
    mode: str,
    cfg: dict,
    zone: str = "main",
) -> str:
    """Map (kind, detail, mode, zone) → severity bucket.

    Pure. Reads the severity table from cfg so tests can inject a
    fixture table. Falls back to LOW for unknown (kind, mode) pairs
    and logs once per unknown kind.

    Args:
        kind: event kind string ("detection", "audio", ...).
        detail: event detail dict; subtype derived via _subtype_of.
        mode: one of OCCUPIED / CLOSED / MAINTENANCE.
        cfg: pipeline config dict (reads cfg["severity_table"]).
        zone: zone name; "main" until zone polygons land in a later session.

    Returns:
        One of CRITICAL, HIGH, MEDIUM, LOW, INFO.
    """
    table = cfg.get("severity_table", {})
    subtype = _subtype_of(kind, detail)
    keys = []
    if subtype:
        keys.append(f"{kind}:{subtype}")
    keys.append(kind)
    for key in keys:
        row = table.get(key)
        if row and mode in row:
            sev = row[mode]
            if sev in _SEVERITIES:
                return sev
    if kind not in _severity_unknown_seen:
        _severity_unknown_seen.add(kind)
        log.warning(
            "SEVERITY UNKNOWN kind=%s subtype=%s mode=%s — defaulting LOW",
            kind, subtype, mode,
        )
    return "LOW"


def compute_decision(severity: str, mode: str, cfg: dict) -> str:
    """Map (severity, mode) → 'telegram' | 'dashboard' | 'suppressed'.

    Pure. Reads the threshold table from cfg.
    """
    table = cfg.get("thresholds", {})
    row = table.get(severity)
    if row and mode in row:
        d = row[mode]
        if d in _DECISIONS:
            return d
    key = (severity, mode)
    if key not in _threshold_unknown_seen:
        _threshold_unknown_seen.add(key)
        log.warning(
            "THRESHOLD UNKNOWN severity=%s mode=%s — defaulting suppressed",
            severity, mode,
        )
    return "suppressed"


# ─────────────────────────── DEDUP (Session 5) ────────────────────────────────
# In-memory three-layer filter. Resets on restart (acceptable; windows ≤5min).
# Coarse single lock — see design §3.

# Track-aware: (camera, track_id, kind) → last fire ts
_dedup_track: "dict[tuple[str, int, str], float]" = {}
# Zone cooldown: (camera, zone, kind) → last fire ts
_dedup_zone: "dict[tuple[str, str, str], float]" = {}
# Burst window: (camera, zone, kind) → deque of timestamps within burst window
_dedup_burst: "dict[tuple[str, str, str], collections.deque]" = (
    collections.defaultdict(lambda: collections.deque(maxlen=64))
)
_dedup_lock = threading.Lock()
_last_dedup_prune: float = 0.0

# Phase 0a — track-activity ledger for track-loss re-fire.
# (camera, track_id) → last frame ts the tracker reported this id.
# Updated by _dedup_observe_tracks each frame from the main loop.
_dedup_known_tracks: "dict[tuple[str, int], float]" = {}


def _window_for_severity(
    base_window_s: float,
    severity: "Optional[str]",
    by_severity_map: "Optional[dict]",
) -> float:
    """Pick the dedup window for this severity, falling back to base.

    Pure helper. Severity is normalised to upper case before lookup.
    Missing or unrecognised entries fall through to ``base_window_s``
    so legacy configs without per-severity maps keep working.

    Args:
        base_window_s: Flat fallback (the legacy single-window value).
        severity: Computed severity bucket, e.g. ``"CRITICAL"``.
        by_severity_map: Optional dict of severity → window seconds.

    Returns:
        Window length in seconds, never less than zero.
    """
    if not by_severity_map or not severity:
        return float(base_window_s)
    val = by_severity_map.get(str(severity).upper())
    if val is None:
        return float(base_window_s)
    try:
        return max(0.0, float(val))
    except (TypeError, ValueError):
        return float(base_window_s)


def _dedup_prune(now: float, track_w: float, zone_w: float) -> None:
    """Drop stale entries from track/zone dicts. Caller holds _dedup_lock."""
    stale_t = [k for k, v in _dedup_track.items() if now - v > track_w]
    for k in stale_t:
        _dedup_track.pop(k, None)
    stale_z = [k for k, v in _dedup_zone.items() if now - v > zone_w]
    for k in stale_z:
        _dedup_zone.pop(k, None)


def _dedup_retire_track(
    camera: str,
    track_id: int,
    *,
    clear_zone: bool = True,
) -> None:
    """Drop dedup entries for one (camera, track_id) pair.

    Called when the tracker stops reporting a track that previously fired,
    so a new track for the same logical situation can re-alert without
    waiting out the full track window. Holds ``_dedup_lock`` internally —
    safe to call from the main loop.

    Args:
        camera: camera id whose track is being retired.
        track_id: ByteTrack id no longer being reported.
        clear_zone: also drop matching ``(camera, zone, kind)`` zone-window
            entries so the zone window does not block the re-fire.
    """
    with _dedup_lock:
        # Drop every (camera, track_id, kind) row for this id.
        stale = [k for k in _dedup_track if k[0] == camera and k[1] == track_id]
        for k in stale:
            kind = k[2]
            _dedup_track.pop(k, None)
            if clear_zone:
                # We don't know which zone the previous fire used, so clear
                # every zone-window entry for (camera, *, kind). Cheap —
                # the dict is small and zones are bounded per camera.
                zstale = [
                    zk for zk in _dedup_zone
                    if zk[0] == camera and zk[2] == kind
                ]
                for zk in zstale:
                    _dedup_zone.pop(zk, None)
        _dedup_known_tracks.pop((camera, track_id), None)


def _dedup_observe_tracks(
    camera: str,
    active_track_ids: "set[int]",
    now: float,
    cfg: dict,
) -> None:
    """Update the track-activity ledger and retire idle tracks.

    Called once per frame from the main loop with the set of track ids
    the tracker reported in this frame. A track that has been silent for
    longer than ``cfg["dedup"]["track_idle_s"]`` is retired
    (dedup entries dropped). Pure observability when a track keeps being
    seen — the cost is one dict update per active track per frame.

    Args:
        camera: camera id being observed (passed through, no inference).
        active_track_ids: track ids reported in the current frame.
        now: epoch seconds for "now". Pass ``time.time()`` from caller.
        cfg: pipeline config dict (reads ``cfg["dedup"]`` only).
    """
    d = cfg.get("dedup", {})
    idle_s = float(d.get("track_idle_s", 5.0))
    clear_zone = bool(d.get("clear_zone_on_track_loss", True))

    if idle_s <= 0:
        # Track-loss re-fire disabled — keep the ledger empty.
        return

    # Refresh last-seen for the tracks present in this frame.
    for tid in active_track_ids:
        _dedup_known_tracks[(camera, int(tid))] = now

    # Retire tracks idle longer than the threshold for this camera. We
    # iterate a snapshot so concurrent retirements from other camera ids
    # don't disturb iteration.
    expired = [
        (cam, tid)
        for (cam, tid), last in list(_dedup_known_tracks.items())
        if cam == camera and (now - last) > idle_s
    ]
    for (cam, tid) in expired:
        _dedup_retire_track(cam, tid, clear_zone=clear_zone)


def _dedup_filter(
    camera: str, kind: str, detail: dict, zone: str, cfg: dict,
    severity: "Optional[str]" = None,
) -> "tuple[Optional[str], int]":
    """Run the three-layer Telegram-bound dedup filter chain.

    Args:
        camera: camera id.
        kind: event kind.
        detail: alert detail dict (read for ``track_id`` only).
        zone: zone name; ``"main"`` until per-zone polygons land.
        cfg: pipeline config dict (reads ``cfg["dedup"]``).
        severity: optional computed severity bucket. When supplied, the
            track / zone window length is selected from
            ``dedup.track_window_by_severity_s`` /
            ``dedup.zone_window_by_severity_s``. Absent → flat fallback.

    Returns:
        Tuple of (suppress_reason, burst_count_at_call_time).
        suppress_reason is one of:
            "suppressed_dedup_track"
            "suppressed_dedup_zone"
            "suppressed_dedup_burst"
            None (no suppression — caller proceeds to telegram fire)
        burst_count is the number of recent same-kind events in the
        current burst window AT the moment of this call. The orchestrator
        uses it to decide whether to fire a burst-summary alongside the
        suppress.
    """
    global _last_dedup_prune
    d = cfg.get("dedup", {})
    track_base = float(d.get("track_window_s", 300))
    zone_base = float(d.get("zone_window_s", 120))
    burst_w = float(d.get("burst_window_s", 30))
    burst_th = int(d.get("burst_threshold", 3))
    track_w = _window_for_severity(
        track_base, severity, d.get("track_window_by_severity_s"),
    )
    zone_w = _window_for_severity(
        zone_base, severity, d.get("zone_window_by_severity_s"),
    )

    track_id = detail.get("track_id")
    has_track = isinstance(track_id, int) and track_id >= 0

    now = time.time()
    with _dedup_lock:
        # Periodic prune — synchronous, fast. Use the LARGEST window
        # across all severities so we never prune entries that are still
        # within their severity's window.
        if now - _last_dedup_prune > 60:
            t_max = max(
                [track_base]
                + [
                    float(v)
                    for v in (d.get("track_window_by_severity_s") or {}).values()
                    if isinstance(v, (int, float))
                ]
            )
            z_max = max(
                [zone_base]
                + [
                    float(v)
                    for v in (d.get("zone_window_by_severity_s") or {}).values()
                    if isinstance(v, (int, float))
                ]
            )
            _dedup_prune(now, t_max, z_max)
            _last_dedup_prune = now

        # 1. Track-aware
        if has_track:
            tk = (camera, int(track_id), kind)
            last = _dedup_track.get(tk)
            if last is not None and now - last < track_w:
                return ("suppressed_dedup_track", 0)
            _dedup_track[tk] = now

        # 2. Zone cooldown
        zk = (camera, zone, kind)
        last = _dedup_zone.get(zk)
        if last is not None and now - last < zone_w:
            return ("suppressed_dedup_zone", 0)
        _dedup_zone[zk] = now

        # 3. Burst window
        bk = (camera, zone, kind)
        dq = _dedup_burst[bk]
        # Trim entries outside the rolling window
        while dq and now - dq[0] > burst_w:
            dq.popleft()
        dq.append(now)
        burst_count = len(dq)
        if burst_count >= burst_th:
            # Clear so next burst-summary requires a fresh threshold count.
            # First post-clear event passes burst (count=1) but is still
            # subject to track and zone dedup downstream.
            dq.clear()
            return ("suppressed_dedup_burst", burst_count)

        return (None, burst_count)


# ─────────────────────────── AUDIT LOG (Session 5) ────────────────────────────
# Synchronous SQLite insert. Single shared connection guarded by a small lock.
# Failures never crash the pipeline; greppable "AUDIT WRITE FAILED" prefix.

_AUDIT_DECISIONS = (
    "fired_telegram",
    "fired_dashboard_only",
    "fired_burst_summary",
    "suppressed_threshold",
    "suppressed_dedup_track",
    "suppressed_dedup_zone",
    "suppressed_dedup_burst",
    "suppressed_ratelimit",
)

_audit_conn: "Optional[sqlite3.Connection]" = None
_audit_lock = threading.Lock()
# Separate read-only connection for /health queries. WAL allows concurrent
# readers without blocking the writer; using a distinct connection avoids
# Python's per-Connection internal mutex serializing /health reads against
# emit_alert writes.
_audit_read_conn: "Optional[sqlite3.Connection]" = None
_audit_read_lock = threading.Lock()


def _audit_db_path() -> str:
    """Resolve the audit DB path — same DB the dashboard uses."""
    return os.getenv(
        "DB_PATH",
        str(Path(__file__).parent / "sentinel_events.db"),
    )


def _audit_init() -> None:
    """Open the audit DB connection and ensure schema. Idempotent.

    Must be called before any worker thread or emit_alert. Sets WAL so
    concurrent writes from dashboard_server.py (its own connection) and
    this module are safe.
    """
    global _audit_conn, _audit_read_conn
    if _audit_conn is not None:
        return
    path = _audit_db_path()
    conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS alert_audit (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              INTEGER NOT NULL,
            event_id        INTEGER,
            camera_id       TEXT    NOT NULL,
            kind            TEXT    NOT NULL,
            computed_severity TEXT  NOT NULL,
            mode            TEXT    NOT NULL,
            decision        TEXT    NOT NULL CHECK (decision IN (
                'fired_telegram', 'fired_dashboard_only', 'fired_burst_summary',
                'suppressed_threshold', 'suppressed_dedup_track',
                'suppressed_dedup_zone', 'suppressed_dedup_burst',
                'suppressed_ratelimit'
            )),
            reason_detail   TEXT,
            zone            TEXT    NOT NULL DEFAULT 'main'
        );
        CREATE INDEX IF NOT EXISTS idx_audit_ts        ON alert_audit(ts);
        CREATE INDEX IF NOT EXISTS idx_audit_event_id  ON alert_audit(event_id);
        """
    )
    conn.commit()
    _audit_conn = conn

    # Read-only connection for /health (mode=ro via URI keeps it honest;
    # any accidental write attempt will raise rather than silently mutate).
    read_conn = sqlite3.connect(
        f"file:{path}?mode=ro", uri=True,
        check_same_thread=False, timeout=5.0,
    )
    _audit_read_conn = read_conn

    log.info("AUDIT DB ready: %s", path)


def _audit_write(
    event_id: "Optional[int]",
    camera_id: str,
    kind: str,
    computed_severity: str,
    mode: str,
    decision: str,
    reason_detail: "Optional[str]" = None,
    zone: str = "main",
) -> None:
    """Insert one decision row. Never raises.

    Also feeds the replay capture buffer when --replay is active, so test
    harnesses can assert against the JSON dump without needing to read the
    audit DB.

    Args:
        event_id: events.id when the decision wrote one; None for suppressed.
        camera_id: camera identifier.
        kind: event kind.
        computed_severity: CRITICAL/HIGH/MEDIUM/LOW/INFO.
        mode: OCCUPIED/CLOSED/MAINTENANCE.
        decision: one of _AUDIT_DECISIONS.
        reason_detail: optional free-form context (e.g., "count=5 window=30s").
        zone: zone name; "main" until zone polygons land.
    """
    ts = int(time.time())
    if _replay_capture is not None:
        _replay_capture.append({
            "ts": ts,
            "event_id": event_id,
            "camera_id": camera_id,
            "kind": kind,
            "computed_severity": computed_severity,
            "mode": mode,
            "decision": decision,
            "reason_detail": reason_detail,
            "zone": zone,
        })
    if _audit_conn is None:
        # _audit_init wasn't called; surface loudly but don't raise.
        log.error(
            "AUDIT WRITE FAILED — connection not initialized "
            "(decision=%s kind=%s)", decision, kind,
        )
        return
    try:
        with _audit_lock:
            _audit_conn.execute(
                "INSERT INTO alert_audit "
                "(ts, event_id, camera_id, kind, computed_severity, "
                " mode, decision, reason_detail, zone) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, event_id, camera_id, kind,
                 computed_severity, mode, decision, reason_detail, zone),
            )
            _audit_conn.commit()
    except Exception as e:
        log.error(
            "AUDIT WRITE FAILED decision=%s kind=%s err=%s",
            decision, kind, e,
        )


def _audit_count_since(seconds_ago: int) -> "tuple[int, dict, int]":
    """Compute /health stats from the audit log. Live, no caching.

    Reads via the dedicated read-only connection so /health never
    serializes against emit_alert writes (WAL allows concurrent readers).
    The smaller _audit_read_lock guards only the read connection's cursor.

    Returns:
        (alerts_fired_last_hour, alerts_suppressed_last_hour, audit_log_size).
    """
    if _audit_read_conn is None:
        return (0, {}, 0)
    cutoff = int(time.time()) - seconds_ago
    fired = 0
    suppressed: dict = {
        "suppressed_threshold":   0,
        "suppressed_dedup_track": 0,
        "suppressed_dedup_zone":  0,
        "suppressed_dedup_burst": 0,
        "suppressed_ratelimit":   0,
    }
    total = 0
    try:
        with _audit_read_lock:
            # NOTE: COUNT(*) on alert_audit is fine today. If /health latency
            # degrades or this table exceeds ~10M rows, switch to a cached
            # counter updated by _audit_write.
            total = _audit_read_conn.execute(
                "SELECT COUNT(*) FROM alert_audit"
            ).fetchone()[0]
            for row in _audit_read_conn.execute(
                "SELECT decision, COUNT(*) FROM alert_audit "
                "WHERE ts > ? GROUP BY decision",
                (cutoff,),
            ):
                dec, n = row[0], row[1]
                if dec.startswith("fired_"):
                    fired += n
                elif dec in suppressed:
                    suppressed[dec] = n
    except Exception as e:
        log.error("AUDIT READ FAILED err=%s", e)
    return (fired, suppressed, total)


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
        class_name = str(detail.get("class_name") or "object").replace("_", " ").lower()
        # Operator brief: include action when behavior classifier has one.
        # Suppress the routine defaults so "1 person — standing" doesn't add noise.
        action = str(detail.get("action") or "").lower()
        if action and action not in ("unknown", "standing", ""):
            return f"1 {class_name} — {action}"
        return f"1 {class_name} detected"
    if k == "forbidden_zone":
        zone = detail.get("zone") or detail.get("zone_name") or "zone"
        return f"Person in forbidden zone: {zone}"
    if k == "phone_use":
        tid = detail.get("track_id")
        if isinstance(tid, int) and tid >= 0:
            return f"Phone use detected — Person #{tid}"
        return "Phone use detected"
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
    severity: str, snapshot_path: "str | None",
) -> None:
    """Enqueue a notify job. Non-blocking.

    Called by emit_alert ONLY when the decision pipeline has already
    determined the event should ship to Telegram — severity gating now
    lives in compute_severity + compute_decision upstream. This function
    does no further filtering; it just formats and enqueues.

    Args:
        event_id: monotonic event id (real id, not None).
        camera_id: camera identifier.
        kind: event kind.
        detail: event detail dict.
        severity: pre-computed severity (CRITICAL/HIGH/MEDIUM/LOW/INFO).
        snapshot_path: optional snapshot path for the notifier worker.
    """
    global _notifier_queue_drops
    if telegram_notifier is None or not CFG.get("notifier_enabled", True):
        return
    summary = _summary_of(kind, detail)
    site = str(CFG.get("site_name") or "unknown")
    try:
        _notify_q.put_nowait(
            (severity, site, camera_id, summary, event_id, snapshot_path,
             kind, detail.get("track_id"))
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
    enqueued job is
        (severity, site, camera, summary, event_id, snapshot_path,
         kind, track_id).
    The trailing kind / track_id are used only to bridge the notifier's
    rate-limit drops into the audit log.
    """
    log.info("NOTIFIER worker thread started")
    last_seen_drop_count = (
        telegram_notifier.drop_count if telegram_notifier else 0
    )
    while True:
        try:
            item = _notify_q.get(timeout=1.0)
        except queue.Empty:
            continue
        try:
            (severity, site, camera, summary, event_id, snapshot_path,
             kind, _track_id) = item

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

            ok = telegram_notifier.notify(
                severity, site, camera, summary, event_id, snapshot_path,
            )

            # Bridge: if the notifier dropped this send because of its
            # internal rate limit, record a suppressed_ratelimit audit row
            # so /health counts the drop.
            if not ok and telegram_notifier is not None:
                new_drop_count = telegram_notifier.drop_count
                if (
                    new_drop_count > last_seen_drop_count
                    and telegram_notifier.last_error == "rate limit"
                ):
                    mode = resolve_mode(cfg)
                    _audit_write(
                        event_id=event_id,
                        camera_id=camera,
                        kind=kind,
                        computed_severity=severity,
                        mode=mode,
                        decision="suppressed_ratelimit",
                        reason_detail=f"telegram rate limit (drop #{new_drop_count})",
                    )
                last_seen_drop_count = new_drop_count
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


def _emit_burst_summary(
    camera_id: str, kind: str, zone: str, count: int,
    severity: str, mode: str,
) -> None:
    """Fire ONE summary alert when the burst threshold trips.

    Bypasses the dedup chain (it's already the burst owner). Writes the
    fired_burst_summary audit row, ships to MQTT and Telegram (both —
    burst summaries always go to Telegram regardless of mode threshold,
    since the underlying events were originally telegram-bound).
    """
    burst_w = int(CFG.get("dedup", {}).get("burst_window_s", 30))
    summary_event_id = _next_event_id()
    snapshot_path = _snapshot_enqueue(summary_event_id)

    # Pluralize for the "person" common case; otherwise generic.
    label = f"{count} {kind} events in {zone} in {burst_w}s"
    payload = {
        "camera":   camera_id,
        "kind":     "burst_summary",
        "ts":       time.time(),
        "label":    label,
        "severity": severity.lower(),
        "underlying_kind": kind,
        "burst_count": count,
        "burst_window_s": burst_w,
        "zone": zone,
        "event_id": summary_event_id,
    }
    try:
        _alert_q.put_nowait(payload)
    except queue.Full:
        log.warning("Alert queue full — dropped burst_summary")

    _dispatch_notify(
        summary_event_id, camera_id, kind, {"track_id": None},
        severity, snapshot_path,
    )

    _audit_write(
        event_id=summary_event_id,
        camera_id=camera_id,
        kind=kind,
        computed_severity=severity,
        mode=mode,
        decision="fired_burst_summary",
        reason_detail=f"count={count} window_s={burst_w}",
        zone=zone,
    )


def emit_alert(camera_id: str, kind: str, detail: dict) -> None:
    """Decide → audit → ship one alert.

    Orchestration only. The decision pipeline is:
        resolve_mode → compute_severity → compute_decision
            → (telegram path: three-layer dedup filter)
            → write events + audit + dispatch_notify

    Every call writes exactly one audit row (plus an additional
    fired_burst_summary row when a burst trips). Suppressed decisions
    write no events row, no Telegram, but always an audit row.
    """
    event_id = _next_event_id()
    zone = "main"  # PUBLIC zone only this session

    try:
        mode = resolve_mode(CFG)
    except Exception:
        log.exception("resolve_mode failed — defaulting to CLOSED (loudest)")
        mode = "CLOSED"

    severity = compute_severity(kind, detail, mode, CFG, zone=zone)
    decision_route = compute_decision(severity, mode, CFG)

    # Operator gate: per-kind allowlist (Phase 0b). Replaces the legacy
    # alerts_only_forbidden_zone kill-switch. Audit row still written so
    # operators can query "what would have fired without the gate?".
    if _alert_kind_allowlist is not None and kind not in _alert_kind_allowlist:
        _audit_write(
            event_id=None, camera_id=camera_id, kind=kind,
            computed_severity=severity, mode=mode,
            decision="suppressed_threshold",
            reason_detail="not_in_allowlist",
            zone=zone,
        )
        return

    # Audit-log integrity: if the threshold table chose "telegram" but the
    # notifier is unavailable (module import failed) or disabled at startup,
    # downgrade to "dashboard" so the audit row reflects what actually
    # happens. Keeping fired_telegram in the audit when no Telegram fires
    # would lie to operators querying the log.
    if decision_route == "telegram" and (
        telegram_notifier is None
        or not CFG.get("notifier_enabled", True)
    ):
        decision_route = "dashboard"

    # Suppressed by threshold table — no events row, no Telegram, audit only.
    if decision_route == "suppressed":
        _audit_write(
            event_id=None, camera_id=camera_id, kind=kind,
            computed_severity=severity, mode=mode,
            decision="suppressed_threshold", zone=zone,
        )
        return

    # Telegram path runs the three-layer dedup filter first.
    if decision_route == "telegram":
        suppress_reason, burst_count = _dedup_filter(
            camera_id, kind, detail, zone, CFG, severity=severity,
        )
        if suppress_reason is not None:
            # Suppressed individuals → audit only, no events row.
            _audit_write(
                event_id=None, camera_id=camera_id, kind=kind,
                computed_severity=severity, mode=mode,
                decision=suppress_reason, zone=zone,
                reason_detail=(
                    f"burst_count={burst_count}"
                    if suppress_reason == "suppressed_dedup_burst" else None
                ),
            )
            # Burst threshold tripped → fire ONE summary in addition.
            if suppress_reason == "suppressed_dedup_burst":
                _emit_burst_summary(
                    camera_id, kind, zone, burst_count, severity, mode,
                )
            return

    # Fired path (telegram or dashboard_only): write events row + Telegram.
    snapshot_path = _snapshot_enqueue(event_id)
    payload = {
        "camera":   camera_id,
        "kind":     kind,
        "ts":       time.time(),
        "label":    detail.get("label", kind),
        **detail,
        # Computed severity wins over any operator hint in detail. Placed
        # AFTER **detail so callers can't clobber. Lowercased to match the
        # dashboard's severity-meta keys ('critical'/'high'/'medium'/...).
        "severity": severity.lower(),
        "event_id": event_id,
    }
    try:
        _alert_q.put_nowait(payload)
    except queue.Full:
        log.warning("Alert queue full — dropped")

    if decision_route == "telegram":
        _dispatch_notify(
            event_id, camera_id, kind, detail, severity, snapshot_path,
        )
        _audit_write(
            event_id=event_id, camera_id=camera_id, kind=kind,
            computed_severity=severity, mode=mode,
            decision="fired_telegram", zone=zone,
        )
    else:
        # dashboard_only — events row written, no Telegram.
        _audit_write(
            event_id=event_id, camera_id=camera_id, kind=kind,
            computed_severity=severity, mode=mode,
            decision="fired_dashboard_only", zone=zone,
        )


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


# ─────────────────────────── FORBIDDEN-ZONE ENGINE ────────────────────────────
class ForbiddenZoneEngine:
    """Operator-drawn forbidden polygons, persisted in SQLite.

    Hot-reloads from the dashboard's writes within ~5 seconds via a
    cheap (count, max(updated_at)) check. Read-only DB connection so
    it never blocks the dashboard's writers. Empty zones list = no
    forbidden-zone alerts (graceful disable).

    Polygons are stored as JSON arrays of [x, y] floats in 0..1
    (image-content normalized) and converted to inference-frame pixels
    at load time.

    Thread-safe: ``check`` is called from the main loop while the
    reload thread mutates ``self._zones``. Both paths take ``self._lock``.
    """

    def __init__(self, db_path: str, frame_w: int, frame_h: int) -> None:
        self._db_path = db_path
        self._frame_w = frame_w
        self._frame_h = frame_h
        # zones format after load: [{"id": int, "name": str,
        #                            "poly": np.ndarray (N,2) int32}]
        self._zones: list = []
        self._last_meta: tuple = (-1, -1)  # (count, max_updated_at)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._conn: "Optional[sqlite3.Connection]" = None

    def _open_ro(self) -> None:
        try:
            self._conn = sqlite3.connect(
                f"file:{self._db_path}?mode=ro", uri=True,
                check_same_thread=False, timeout=5.0,
            )
        except Exception as e:
            log.error("FORBIDDEN_ZONE: read-only connect failed: %s", e)
            self._conn = None

    def _meta(self) -> "tuple[int, int]":
        if self._conn is None:
            return (0, 0)
        try:
            row = self._conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(updated_at), 0) FROM forbidden_zones"
            ).fetchone()
            return (int(row[0]), int(row[1]))
        except sqlite3.OperationalError:
            # Table doesn't exist yet (dashboard hasn't created it). Treat as empty.
            return (0, 0)
        except Exception as e:
            log.warning("FORBIDDEN_ZONE: meta query failed: %s", e)
            return self._last_meta

    def _reload(self) -> None:
        if self._conn is None:
            return
        new: list = []
        try:
            for row in self._conn.execute(
                "SELECT id, name, polygon FROM forbidden_zones ORDER BY id"
            ):
                try:
                    pts = json.loads(row[2])
                    poly = np.array(
                        [[round(float(p[0]) * self._frame_w),
                          round(float(p[1]) * self._frame_h)]
                         for p in pts],
                        dtype=np.int32,
                    )
                    if len(poly) >= 3:
                        new.append({"id": int(row[0]), "name": str(row[1]), "poly": poly})
                except Exception as e:
                    log.warning(
                        "FORBIDDEN_ZONE: skipping malformed polygon id=%s: %s",
                        row[0], e,
                    )
        except sqlite3.OperationalError:
            pass  # table not yet created
        except Exception as e:
            log.warning("FORBIDDEN_ZONE: reload failed: %s", e)
            return
        with self._lock:
            self._zones = new
        log.info("FORBIDDEN_ZONE reloaded: %d zone(s)", len(new))

    def start_reload_thread(self, interval_s: float = 5.0) -> None:
        self._open_ro()
        # Initial load before the loop so the engine is ready immediately.
        self._last_meta = self._meta()
        if self._last_meta != (0, 0):
            self._reload()

        def _loop() -> None:
            while not self._stop.is_set():
                try:
                    m = self._meta()
                    if m != self._last_meta:
                        self._last_meta = m
                        self._reload()
                except Exception:
                    log.exception("FORBIDDEN_ZONE reload thread error — continuing")
                self._stop.wait(interval_s)

        threading.Thread(
            target=_loop, daemon=True, name="forbidden-zone-reload",
        ).start()
        log.info("FORBIDDEN_ZONE reload thread started (interval=%.1fs)", interval_s)

    def check(self, cx: int, cy: int) -> "Optional[dict]":
        """Return matching zone dict if (cx,cy) is inside any forbidden polygon."""
        with self._lock:
            zones = list(self._zones)  # snapshot for the loop
        for z in zones:
            if cv2.pointPolygonTest(z["poly"], (cx, cy), False) >= 0:
                return z
        return None

    def all_polys(self) -> list:
        """Return shallow copy of zones for the live-feed overlay."""
        with self._lock:
            return list(self._zones)


# ─────────────────────────── ZONE ENGINE ──────────────────────────────────────
class ZoneEngine:
    """Per-track dwell timer per polygon — emits loitering when dwell exceeded.

    Refactored in Phase 1c: ``observe`` is a generator yielding event
    detail dicts instead of calling ``emit_alert`` directly. The rule
    layer (``LoiteringRule``) routes the detail dicts through
    ``emit_alert`` so dedup, severity, and audit live in one place.
    """

    def __init__(self, zones: list):
        self.zones = []
        for z in zones:
            poly = np.array(z["polygon"], dtype=np.int32)
            self.zones.append({
                "name": z["name"], "poly": poly,
                "dwell_sec": z["dwell_sec"], "dwell": {},
            })

    def observe(self, track_id: int, cx: int, cy: int):
        """Yield one detail dict per zone whose dwell threshold trips this call.

        Dwell timers are per-track-per-zone. Leaving a zone clears the
        timer for that track. Re-firing inside a zone resets the timer
        on each fire (so a long lingering track produces one detail
        every ``dwell_sec`` seconds — central dedup smooths from there).

        Args:
            track_id: ByteTrack id (or 0 for untracked objects, matching
                the legacy contract).
            cx: Centroid x in inference-frame pixels.
            cy: Centroid y.

        Yields:
            Detail dicts ready for ``emit_alert(camera, "loitering", ...)``.
        """
        now = time.time()
        for z in self.zones:
            inside = cv2.pointPolygonTest(z["poly"], (cx, cy), False) >= 0
            if inside:
                entered = z["dwell"].setdefault(track_id, now)
                if now - entered >= z["dwell_sec"]:
                    yield {
                        "zone":      z["name"],
                        "track_id":  track_id,
                        "dwell_sec": round(now - entered, 1),
                        "label":     f"Loitering in {z['name']}",
                        "severity":  "medium",
                    }
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


def containment_xyxy(small, big) -> float:
    """Fraction of ``small`` bbox area contained inside ``big`` bbox.

    Used by the phone-use rule: a held phone is geometrically tiny
    next to the person bbox (~1% of person area), so IoU caps at
    ~0.01 even when the phone is 100% inside the person — which makes
    IoU useless as a "phone in front of body" detector. Containment
    answers the right question instead: "what fraction of the phone
    is overlapping the person?". Returns a value in [0, 1] with 1.0
    meaning ``small`` is fully inside ``big``.

    Args:
        small: ``(x1, y1, x2, y2)`` of the smaller bbox (e.g. phone).
        big:   ``(x1, y1, x2, y2)`` of the larger bbox (e.g. person).

    Returns:
        Fraction of ``small``'s area contained in ``big``. ``0.0`` if
        ``small`` has zero or negative area.
    """
    sx1, sy1, sx2, sy2 = small
    bx1, by1, bx2, by2 = big
    ix1 = max(sx1, bx1); iy1 = max(sy1, by1)
    ix2 = min(sx2, bx2); iy2 = min(sy2, by2)
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    small_area = max(0, sx2 - sx1) * max(0, sy2 - sy1)
    if small_area <= 0:
        return 0.0
    return inter / small_area

def draw_tracks(frame, tracks):
    """Draw a clean bounding box + class name per track.

    Track id and confidence are intentionally omitted from the on-screen
    label — they're useful for debugging but cluttered for an operator-
    facing view. The track id is still attached to every emitted event,
    so audit/dedup behavior is unchanged. Behavior overlays are owned by
    ``behavior.draw_behavior``.
    """
    if tracks is None or len(tracks) == 0:
        return
    for t in tracks:
        x1, y1, x2, y2 = int(t[0]), int(t[1]), int(t[2]), int(t[3])
        tid = int(t[4])
        cls = int(t[6]) if len(t) > 6 else -1
        c   = _color(tid)
        name = COCO_NAMES[cls] if 0 <= cls < len(COCO_NAMES) else ""
        lbl  = name.title() if name else ""

        cv2.rectangle(frame, (x1, y1), (x2, y2), c, 2)
        if lbl:
            cv2.putText(frame, lbl, (x1, y1 - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, c, 1, cv2.LINE_AA)

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

def draw_forbidden_zones(frame, zones: list) -> None:
    """Outline every persisted forbidden polygon in red on the live feed.

    Visual confirmation that the rule is active. Drawn before draw_tracks
    so person bboxes overlay it.
    """
    if not zones:
        return
    for z in zones:
        poly = z["poly"]
        # Translucent red fill + solid red border.
        overlay = frame.copy()
        cv2.fillPoly(overlay, [poly], (40, 40, 220))
        cv2.addWeighted(overlay, 0.18, frame, 0.82, 0, dst=frame)
        cv2.polylines(frame, [poly], isClosed=True,
                      color=(40, 40, 220), thickness=2, lineType=cv2.LINE_AA)
        # Zone name intentionally not rendered on the live view —
        # the colored fill already communicates "this is a forbidden
        # area"; the human-readable name lives in events / dashboard.


def draw_anomaly(frame, score: float):
    """Show a clean ANOMALY banner when score exceeds the threshold.

    Numeric score is intentionally omitted — operators don't act on the
    raw value; the audit log keeps it for forensics.
    """
    if score > CFG["anomaly_thresh"]:
        cv2.putText(frame, "ANOMALY", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2, cv2.LINE_AA)

def draw_hud(frame, fps: float, n_tracks: int, thermal: float):
    """No-op — every HUD element is now suppressed on the live view.

    Parameters are kept for caller stability so the main loop's
    annotation block doesn't need to change. Diagnostic numbers (fps /
    tracks / thermal) and the wall-clock timestamp are all available on
    the /health endpoint and the dashboard.
    """
    return


# ─────────────────────────── MAIN LOOP ────────────────────────────────────────
def run(cfg: dict):
    # Session 5 — validate schedule + init audit DB BEFORE any worker thread
    # so misconfigured schedules crash loudly at startup (per spec failure
    # mode: "Better to crash than silently run in wrong mode").
    try:
        _validate_schedule(cfg)
    except Exception as e:
        log.critical("SCHEDULE INVALID — refusing to start: %s", e)
        sys.exit(1)
    try:
        _audit_init()
    except Exception as e:
        log.critical("AUDIT INIT FAILED — refusing to start: %s", e)
        sys.exit(1)
    log.info("Mode at startup: %s", resolve_mode(cfg))

    # Replay mode (offline test harness) skips every network-bound side
    # effect. Inference + decision pipeline still run; everything that
    # would touch operators or external systems is gated off.
    replay = bool(cfg.get("replay_mode"))
    if replay:
        cfg["snapshot_enabled"] = False
        cfg["notifier_enabled"] = False
        log.info(
            "REPLAY mode active — MQTT / MJPEG / health / snapshots / "
            "Telegram disabled. Source: %s", cfg.get("rtsp_url"),
        )

    # ── Start support services ─────────────────────────────────────────────────
    if not replay:
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
    # Operator-drawn forbidden polygons — DB-backed, hot-reloaded every 5s.
    forbidden = ForbiddenZoneEngine(
        _audit_db_path(),
        frame_w=cfg["resize_w"],
        frame_h=cfg["resize_h"],
    )
    forbidden.start_reload_thread(interval_s=5.0)

    # ── Rules engine (Phase 1 + 2) ─────────────────────────────────────────────
    # Phase 1: forbidden_zone, loitering.
    # Phase 2: predicted_intrusion (trajectory extrapolation onto the
    #          same forbidden polygons; fires as an early warning before
    #          the person actually crosses the boundary).
    # Future phases: ppe_violation, port phone_use + detection emits.
    rules_engine = RulesEngine([
        ForbiddenZoneRule(forbidden),
        PredictedIntrusionRule(forbidden),
        LoiteringRule(zones),
    ])
    log.info(
        "RULES engine ready: %s",
        ", ".join(r.name for r in rules_engine.rules) or "(none)",
    )

    cam = open_camera(cfg["rtsp_url"])

    running        = True
    n_frame        = 0
    last_infer     = 0.0
    # Replay mode runs flat-out — every frame in the MP4 should be processed.
    infer_gap      = 0.0 if replay else 1.0 / max(cfg["inference_fps"], 1.0)
    last_tracks    = np.empty((0, 6))
    last_faces:    list = []
    last_labels:   list = []
    last_anomaly   = 0.0
    last_summary_counts: dict = {}
    last_summary_ts = 0.0

    # FPS rolling window
    fps_window: collections.deque = collections.deque(maxlen=30)
    last_fps_ts = time.time()

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
            # Drop stale frames for low latency on the live RTSP path.
            # Replay mode reads every frame of the MP4 — no dropping.
            if not replay:
                for _ in range(3):
                    cam.grab()

            ret, raw = cam.read()
            if not ret or raw is None:
                if replay:
                    log.info("REPLAY: end of clip — exiting")
                    running = False
                    continue
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

                # ── Track-loss re-fire ledger (Phase 0a) ───────────────────────
                # Tell the dedup module which track ids are alive RIGHT NOW so
                # it can retire entries for tracks the tracker has dropped.
                # Without this, a 5-min lingering trespasser produces 1 alert
                # then 100s of suppressed_dedup_track rows.
                if tracker is not None:
                    active_ids = {
                        int(t[4])
                        for t in last_tracks
                        if len(t) > 6 and int(t[4]) >= 0
                    }
                    _dedup_observe_tracks(cfg["camera_id"], active_ids, now, cfg)

                # ── Normalised tracks (Phase 1) ────────────────────────────────
                # One pass over last_tracks builds the typed list every rule
                # consumes. Decoder lives here so individual rules don't have
                # to handle the with-tracker vs without-tracker shape variants.
                tracked_objects: "list[TrackedObject]" = []
                for t in last_tracks:
                    x1_, y1_, x2_, y2_ = int(t[0]), int(t[1]), int(t[2]), int(t[3])
                    if tracker is not None and len(t) > 6:
                        tid_ = int(t[4]); conf_ = float(t[5]); cls_ = int(t[6])
                    else:
                        conf_ = float(t[4]) if len(t) > 4 else 0.0
                        cls_ = int(t[5]) if len(t) > 5 else -1
                        tid_ = -1
                    tracked_objects.append(TrackedObject(
                        track_id=tid_,
                        bbox=(x1_, y1_, x2_, y2_),
                        cls=cls_,
                        conf=conf_,
                        cx=(x1_ + x2_) // 2,
                        cy=(y1_ + y2_) // 2,
                    ))

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

                # ── Per-track detection events ─────────────────────────────────
                # Per-object cooldown removed in Session 5 — dedup now lives in
                # the central decision pipeline (compute_decision + three-layer
                # filter inside emit_alert). Every track emits; the orchestrator
                # decides what's noise.
                #
                # Action cache (Session 7): read the latest behavior labels
                # ONCE per frame and stamp the action onto each detection so
                # _summary_of can render "1 person — sitting" etc.
                action_by_track = (
                    beh_analyzer.get_labels() if beh_analyzer else {}
                )

                # ── Publish snapshot frame BEFORE emits ────────────────
                # The bottom-of-loop annotation block runs after alerts
                # fire, so without this an event triggered on frame N
                # would save the previously-published `vis` (frame N-1
                # or older). For events like phone_use that depend on a
                # transient overlap, the saved JPEG often showed nobody
                # using the phone. Annotating once here on the SAME
                # frame whose detections produced the event makes the
                # snapshot match what the rule actually saw.
                vis_snap = frame.copy()
                draw_forbidden_zones(vis_snap, forbidden.all_polys())
                draw_tracks(vis_snap, last_tracks)
                if beh_analyzer:
                    try:
                        from behavior import draw_behavior
                        draw_behavior(vis_snap, last_tracks, action_by_track)
                    except Exception:
                        pass
                draw_faces(vis_snap, last_faces, last_labels)
                draw_anomaly(vis_snap, last_anomaly)
                draw_hud(vis_snap, fps, len(last_tracks), _health.get("thermal_c", 0.0))
                global _snapshot_frame
                _snapshot_frame = vis_snap

                # First pass: collect phone bboxes for the phone-use check.
                phone_bboxes: list = []
                for t in last_tracks:
                    cls_t = int(t[6]) if (tracker is not None and len(t) > 6) else (
                        int(t[5]) if len(t) > 5 else -1
                    )
                    if cls_t == 67:  # cell phone
                        phone_bboxes.append([int(t[0]), int(t[1]), int(t[2]), int(t[3])])

                phone_containment = float(cfg.get("phone_use_containment", 0.3))
                phone_pad = int(cfg.get("phone_use_person_pad_px", 40))

                # Single-owner phone assignment: each phone fires phone_use
                # for AT MOST one person — the one whose padded bbox best
                # contains it. Without this, two adjacent persons (e.g. ids
                # 9 and 10 standing close) both have padded boxes that pass
                # the containment threshold, so the phone_use alert lands
                # on whichever track was iterated first instead of the
                # actual phone holder.
                phone_owner_idx: list = []  # parallel to phone_bboxes; -1 = unowned
                for ph in phone_bboxes:
                    best_idx = -1
                    best_cont = phone_containment  # must beat the threshold
                    for idx_p, t in enumerate(last_tracks):
                        cls_p = int(t[6]) if (tracker is not None and len(t) > 6) else (
                            int(t[5]) if len(t) > 5 else -1
                        )
                        if cls_p != 0:
                            continue
                        bx1, by1, bx2, by2 = int(t[0]), int(t[1]), int(t[2]), int(t[3])
                        padded_p = (
                            bx1 - phone_pad, by1 - phone_pad,
                            bx2 + phone_pad, by2 + phone_pad,
                        )
                        c = containment_xyxy(ph, padded_p)
                        if c > best_cont:
                            best_cont = c
                            best_idx = idx_p
                    phone_owner_idx.append(best_idx)

                for _track_idx, t in enumerate(last_tracks):
                    x1, y1, x2, y2 = int(t[0]), int(t[1]), int(t[2]), int(t[3])
                    bbox = [x1, y1, x2, y2]

                    if tracker is not None and len(t) > 6:
                        track_id = int(t[4]); conf = float(t[5]); cls = int(t[6])
                    else:
                        conf = float(t[4]) if len(t) > 4 else 0.0
                        cls = int(t[5]) if len(t) > 5 else -1
                        track_id = -1

                    cx = int((x1 + x2) / 2); cy = int((y1 + y2) / 2)
                    # Loitering moved to LoiteringRule (Phase 1c); evaluated
                    # outside this loop via rules_engine.evaluate.

                    if not (0 <= cls < len(COCO_NAMES)):
                        continue
                    class_name = COCO_NAMES[cls]

                    # Cell-phone detections feed phone_use only — no
                    # standalone "1 cell phone detected" events.
                    if cls == 67:
                        continue

                    # Forbidden-zone moved to ForbiddenZoneRule (Phase 1b);
                    # evaluated outside this loop via rules_engine.evaluate.

                    # ── Phone-use check (person tracks only) ──────────────
                    # Fires per-frame while phone overlaps person; track-dedup
                    # (5min window) collapses to one fired audit row. Same
                    # pattern as detection events. Audit log will show
                    # suppressed_dedup_track rows by design (Section 8
                    # traceability). Ownership was decided above — at most
                    # one person fires per phone, even if several persons'
                    # padded bboxes overlap it.
                    if cls == 0 and phone_bboxes:
                        padded_person = (
                            bbox[0] - phone_pad, bbox[1] - phone_pad,
                            bbox[2] + phone_pad, bbox[3] + phone_pad,
                        )
                        for ph_i, owner_idx in enumerate(phone_owner_idx):
                            if owner_idx != _track_idx:
                                continue
                            ph = phone_bboxes[ph_i]
                            cont = containment_xyxy(ph, padded_person)
                            emit_alert(cfg["camera_id"], "phone_use", {
                                "track_id":  track_id if track_id >= 0 else None,
                                "bbox":      bbox,
                                "phone_bbox": ph,
                                "containment": round(cont, 3),
                                "label":     "Phone use detected",
                            })
                            break

                    # Action cache stamp — pure read, no mutation. Stale by
                    # a few hundred ms is fine; analyzer prunes on its own.
                    action_label = None
                    if track_id >= 0:
                        info = action_by_track.get(track_id)
                        if info:
                            action_label = info.get("action")

                    emit_alert(cfg["camera_id"], "detection", {
                        "class_name": class_name,
                        "confidence": round(conf, 3),
                        "bbox":       bbox,
                        "track_id":   track_id if track_id >= 0 else None,
                        "action":     action_label,
                        "label":      f"{class_name} detected",
                        "severity":   "medium" if class_name == "person" else "low",
                    })

                # ── Rules engine evaluation (Phase 1) ──────────────────────────
                # All rules registered with rules_engine see the same
                # per-frame context. Each result is dispatched through the
                # central emit_alert pipeline (severity, dedup, audit, notify
                # all stay in one place). Per-rule failures are logged but
                # never propagate — the engine isolates them.
                rule_ctx = RuleContext(
                    camera_id=cfg["camera_id"],
                    ts=now,
                    n_frame=n_frame,
                    mode=resolve_mode(cfg),
                    cfg=cfg,
                    tracks=tracked_objects,
                    has_tracker=tracker is not None,
                    action_by_track=action_by_track,
                    frame=frame,
                )
                for _rule, _result in rules_engine.evaluate(rule_ctx):
                    emit_alert(cfg["camera_id"], _result.kind, _result.detail)

            # ── FACE (disabled until ORT is stable on this Jetson) ─────────────
            # if face_det and n_frame % cfg["face_every_n"] == 0: ...

            # ── ANOMALY (disabled until patchcore is built) ────────────────────
            # if patchcore and n_frame % cfg["anomaly_every_n"] == 0: ...

            # ── ANNOTATE + STREAM ──────────────────────────────────────────────
            vis = frame.copy()
            beh_labels = beh_analyzer.get_labels() if beh_analyzer else {}

            # Replay mode: capture per-frame action labels for offline
            # assertions. Cheap (dict copy already done by get_labels()).
            if _replay_action_capture is not None and beh_labels:
                _replay_action_capture.append({
                    "frame":  n_frame,
                    "ts":     now,
                    "labels": beh_labels,
                })

            draw_forbidden_zones(vis, forbidden.all_polys())
            draw_tracks(vis, last_tracks)
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
            # Atomic reference rebind under the GIL; consumers .copy()
            # before use. Background-thread alerts (e.g. thermal) read
            # this; in-loop alerts read the earlier publish made before
            # emits, so they match the frame that triggered them.
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
        # Dump replay decision capture if requested.
        out_path = cfg.get("replay_emit_json")
        if _replay_capture is not None and out_path:
            try:
                Path(out_path).write_text(json.dumps(_replay_capture, indent=2))
                log.info(
                    "REPLAY: wrote %d decision(s) to %s",
                    len(_replay_capture), out_path,
                )
            except Exception as e:
                log.error("REPLAY: failed to write %s: %s", out_path, e)
        # Dump per-frame action capture if requested.
        actions_path = cfg.get("replay_emit_actions")
        if _replay_action_capture is not None and actions_path:
            try:
                Path(actions_path).write_text(
                    json.dumps(_replay_action_capture, indent=2)
                )
                log.info(
                    "REPLAY: wrote %d action frame(s) to %s",
                    len(_replay_action_capture), actions_path,
                )
            except Exception as e:
                log.error("REPLAY: failed to write %s: %s", actions_path, e)


def main() -> None:
    """CLI entry point.

    Default invocation runs the production pipeline against the configured
    RTSP source. ``--replay`` switches to offline test mode: source is an
    MP4 path, every network-bound side effect is gated off, and every
    decision the pipeline would make is captured to JSON for assertion in
    pytest.
    """
    import argparse
    parser = argparse.ArgumentParser(
        description="Sentinel security pipeline (production + replay).",
    )
    parser.add_argument(
        "--replay", metavar="PATH",
        help="Path to an MP4/AVI/etc. — runs offline against this file "
             "instead of RTSP. Disables MQTT, MJPEG, /health, snapshots, "
             "and Telegram. Exits cleanly on EOF.",
    )
    parser.add_argument(
        "--emit-json", dest="emit_json", metavar="PATH",
        help="Replay only: write every emitted decision (kind, severity, "
             "mode, decision, ...) to this JSON file on exit.",
    )
    parser.add_argument(
        "--emit-actions", dest="emit_actions", metavar="PATH",
        help="Replay only: write per-frame behavior action labels (track_id "
             "→ {action, next_action}) to this JSON file on exit. Used by "
             "the classifier-accuracy regression tests.",
    )
    parser.add_argument(
        "--mode", choices=list(_VALID_MODES),
        help="Force resolve_mode to return this value, bypassing the "
             "schedule. Useful for replay tests that need deterministic "
             "mode regardless of when they run.",
    )
    args = parser.parse_args()

    if args.replay:
        global _replay_capture, _replay_action_capture
        _replay_capture = []
        if args.emit_actions:
            _replay_action_capture = []
        CFG["rtsp_url"] = args.replay
        CFG["replay_mode"] = True
        CFG["replay_emit_json"] = args.emit_json
        CFG["replay_emit_actions"] = args.emit_actions
    elif args.emit_json or args.emit_actions:
        parser.error("--emit-json / --emit-actions requires --replay")

    if args.mode:
        CFG["replay_force_mode"] = args.mode

    run(CFG)


if __name__ == "__main__":
    main()
