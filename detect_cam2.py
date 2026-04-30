"""detect_cam2.py — Phase 1 cam2 dev pipeline (standalone, isolated).

Reads RTSP from the second camera (INP-54M2812M0A motorized varifocal),
runs YOLO person detection, estimates per-person distance via the
assumed-height pinhole formula, draws annotations, and serves an MJPEG
preview + /health endpoint on dedicated ports.

Fully isolated from production:
  - distinct ports (8082 health, 8083 MJPEG; production uses 8080/8081)
  - no shared MQTT topic, no SQLite write, no systemd unit
  - no edits to detect.py, detect_v2.py, or sentinel_events.db

Phase 2 (later) will add optical-zoom control once the camera's
encrypted-login REST API at /API/PreviewChannel/PTZ/* is unblocked
(needs a captured browser session or a JS-RE'd login client).

Run:
    source .venv/bin/activate
    python3 detect_cam2.py
"""

import http.server
import json
import logging
import os
import queue
import socketserver
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

import cv2
import numpy as np
import torch

os.environ.setdefault("YOLO_CONFIG_DIR", "/tmp/Ultralytics")
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;5000000"


def _load_env_file(path: Path) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ.

    Existing env vars take precedence (setdefault). Lines starting with
    '#' and blank lines are skipped. Surrounding single/double quotes
    around values are stripped.

    Args:
        path: Path to a .env file. Missing files are silently ignored.
    """
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_file(Path(__file__).parent / "cam2.env")


def _required_env(name: str) -> str:
    """Return env var ``name`` or exit with a helpful error message."""
    v = os.getenv(name)
    if not v:
        sys.stderr.write(
            f"missing required env var {name} — set it in cam2.env\n"
        )
        sys.exit(2)
    return v


CFG = {
    # Source — credentials loaded from gitignored cam2.env.
    "rtsp_url": (
        f"rtsp://{_required_env('CAM2_USER')}:{_required_env('CAM2_PASS')}"
        f"@{os.getenv('CAM2_HOST', '192.168.2.117')}"
        f":{os.getenv('CAM2_RTSP_PORT', '554')}"
        f"{os.getenv('CAM2_RTSP_PATH', '/rtsp/streaming?channel=01&subtype=0')}"
    ),
    "camera_id": "cam_02",

    # Detector
    "yolo_model":      "models/yolov9c.pt",   # shared read-only with prod
    "yolo_conf":       0.35,
    "yolo_iou":        0.5,
    "person_class_id": 0,                     # COCO 'person'
    "infer_w":         1280,                  # downscale before YOLO
    "infer_h":         720,
    "inference_fps":   8.0,                   # ceiling, not floor

    # Distance estimation (pinhole, assumed-height).
    # Default focal_length_px is for the 2.8mm wide setting on a 1/2.8" 5MP
    # sensor (~2.8mm * 1440px / 4.3mm sensor height ≈ 938px). Calibrate by
    # placing a 1.7m-tall person at a known distance and back-solving:
    #   focal_length_px = (distance_m * bbox_h_px) / 1.7
    "assumed_person_height_m": 1.7,
    "focal_length_px":         938.0,
    "far_threshold_m":         8.0,
    "min_bbox_height_px":      20,            # ignore detections below this

    # Servers — MJPEG re-encodes the annotated frame at this size + fps so the
    # browser preview is fast even though inference runs on full 1440p frames.
    "mjpeg_port":      8083,                  # prod uses 8080
    "mjpeg_w":         1280,
    "mjpeg_h":         720,
    "mjpeg_fps":       15,
    "mjpeg_quality":   55,
    "health_port":     8082,                  # prod uses 8081

    # Observability
    "event_log_path":  "cam2_events.jsonl",

    # Reconnect
    "reconnect_delay_s": 3.0,

    # Auto-zoom on far person (Phase 2). Disabled with zoom_enabled=False so
    # the pipeline still works as detection-only if the camera is unreachable
    # for PTZ or cryptography is not installed.
    #
    # Closed-loop: keep firing zoom-in pulses until the largest detected
    # person's bbox fills `zoom_target_bbox_ratio` of the frame height.
    # When no far person is seen for `zoom_out_after_clear_s`, walk the
    # zoom level back to 0 one pulse at a time.
    "zoom_enabled":               True,
    "zoom_in_after_far_frames":   2,     # consecutive frames before triggering
    "zoom_in_pulse_s":            1.5,   # bigger pulse — visible movement
    "zoom_in_cooldown_s":         2.0,   # min spacing between pulses
    "zoom_target_bbox_ratio":     0.55,  # stop zooming when bbox fills this
    "zoom_out_after_clear_s":     6.0,   # idle time before reverting
    "zoom_out_pulse_s":           1.5,
    "zoom_max_pulses":            8,     # cap (matches ~4× lens range)

    # Digital follow-zoom on the MJPEG preview. Independent of optical PTZ —
    # works even if the lens never moves. When a FAR person is detected, the
    # live stream crops around their bbox and upscales to mjpeg_w x mjpeg_h.
    "follow_zoom_enabled":        True,
    "follow_zoom_padding":        1.8,    # crop is 1.8x bbox in each dim
    "follow_zoom_smooth_alpha":   0.25,   # 0..1, larger = snappier (less smooth)
    "follow_zoom_min_bbox_h_px":  40,     # don't crop on tiny detections
}


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("cam2")


def estimate_distance_m(
    bbox_h_px: float, focal_px: float, real_h_m: float
) -> float:
    """Estimate distance to a vertical object via pinhole projection.

    Uses the assumed-height model: ``distance = (real_h * focal_px) / bbox_h``.
    Accuracy is ~±15-25% without per-camera calibration; good enough for a
    far/near gate. Caller should pre-filter very small bboxes.

    Args:
        bbox_h_px: Detection bounding-box height, in pixels.
        focal_px: Camera focal length expressed in pixels at the current zoom.
        real_h_m: Assumed real-world height of the object class, in meters.

    Returns:
        Distance to the object in meters, or +inf for non-positive input.
    """
    if bbox_h_px <= 0 or focal_px <= 0:
        return float("inf")
    return (real_h_m * focal_px) / float(bbox_h_px)


_latest_jpeg: Optional[bytes] = None
_latest_lock = threading.Lock()
_state: dict = {
    "started_at": time.time(),
    "frames_in": 0,
    "last_frame_at": 0.0,
    "last_persons": [],
    "rtsp_open": False,
    "errors_total": 0,
    "zoom_enabled": False,
    "zoom_level": 0,
    "zoom_actions_total": 0,
    "last_zoom_action_t": 0.0,
}


class _MJPEGHandler(http.server.BaseHTTPRequestHandler):
    """multipart/x-mixed-replace MJPEG stream of the latest annotated frame."""

    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        if self.path != "/stream":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.send_header(
            "Content-Type", "multipart/x-mixed-replace; boundary=frame"
        )
        self.end_headers()
        try:
            while True:
                with _latest_lock:
                    jpg = _latest_jpeg
                if jpg is None:
                    time.sleep(0.05)
                    continue
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                self.wfile.write(b"Content-Length: ")
                self.wfile.write(str(len(jpg)).encode())
                self.wfile.write(b"\r\n\r\n")
                self.wfile.write(jpg)
                self.wfile.write(b"\r\n")
                time.sleep(1.0 / max(CFG["mjpeg_fps"], 1))
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, fmt, *args):  # silence default access log
        return


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    """JSON status endpoint for the cam2 pipeline."""

    def do_GET(self):  # noqa: N802
        if self.path != "/health":
            self.send_error(404)
            return
        last_age = (
            time.time() - _state["last_frame_at"]
            if _state["last_frame_at"] else None
        )
        body = json.dumps({
            "ok": _state["rtsp_open"],
            "camera_id": CFG["camera_id"],
            "uptime_s": round(time.time() - _state["started_at"], 1),
            "frames_in": _state["frames_in"],
            "last_frame_age_s": (
                round(last_age, 2) if last_age is not None else None
            ),
            "errors_total": _state["errors_total"],
            "last_persons": _state["last_persons"],
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


class _ThreadedHTTP(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Threading HTTP server with quick port reuse on restart."""

    daemon_threads = True
    allow_reuse_address = True


def _start_servers(mjpeg_port: int, health_port: int) -> None:
    """Start MJPEG and health endpoints on background daemon threads."""
    mj = _ThreadedHTTP(("0.0.0.0", mjpeg_port), _MJPEGHandler)
    he = _ThreadedHTTP(("0.0.0.0", health_port), _HealthHandler)
    threading.Thread(
        target=mj.serve_forever, name="cam2-mjpeg", daemon=True
    ).start()
    threading.Thread(
        target=he.serve_forever, name="cam2-health", daemon=True
    ).start()
    log.info("MJPEG  -> http://0.0.0.0:%d/stream", mjpeg_port)
    log.info("Health -> http://0.0.0.0:%d/health", health_port)


def open_camera(url: str) -> cv2.VideoCapture:
    """Open the RTSP stream with the FFmpeg backend (TCP transport).

    Args:
        url: Full RTSP URL including credentials and path.

    Returns:
        An (already-opened or attempting-to-open) cv2.VideoCapture. The
        caller must check ``cap.isOpened()`` before using it.
    """
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


_event_lock = threading.Lock()


def _log_event(path: str, event: dict) -> None:
    """Append a JSON-line event record to ``path`` (thread-safe)."""
    record = dict(event)
    record["ts"] = time.time()
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with _event_lock:
        with open(path, "a") as f:
            f.write(line)


# ─────────────────────────── ZOOM CONTROLLER ──────────────────────────────────

class _ZoomController:
    """Auto-zoom-on-far state machine + background worker.

    Runs in its own daemon thread, consumes pulse requests from a bounded
    queue, and drives PTZ via a Cam2Session. Detection thread only enqueues
    decisions; it never blocks on HTTP.

    State rules:
      - Zoom in when ``zoom_in_after_far_frames`` consecutive inference
        frames contain a far person, the cooldown has elapsed, and the
        net zoom level is below ``zoom_max_pulses``.
      - Zoom out when no far person has been seen for
        ``zoom_out_after_clear_s`` and the net zoom level is > 0.
    """

    def __init__(self, session: "Any", cfg: dict) -> None:
        self._session = session
        self._cfg = cfg
        self._queue: "queue.Queue[str]" = queue.Queue(maxsize=4)
        self._consec_far_frames = 0
        self._last_far_seen_t = 0.0
        self._zoom_level = 0
        self._last_action_t = 0.0
        self._stop = threading.Event()
        self._t = threading.Thread(
            target=self._worker, name="cam2-zoom", daemon=True,
        )

    def start(self) -> None:
        self._t.start()

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                action = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if action == "zoom-in":
                    ok = self._session.ptz_pulse(
                        "Ptz_Cmd_ZoomAdd",
                        duration_s=self._cfg["zoom_in_pulse_s"],
                    )
                    log.info("zoom-in pulse: %s", "OK" if ok else "FAIL")
                elif action == "zoom-out":
                    ok = self._session.ptz_pulse(
                        "Ptz_Cmd_ZoomMinus",
                        duration_s=self._cfg["zoom_out_pulse_s"],
                    )
                    log.info("zoom-out pulse: %s", "OK" if ok else "FAIL")
            except Exception as e:
                log.warning("zoom worker error: %s", e)

    def update(self, persons: list, frame_h: int) -> None:
        """Call after each inference. Decides whether to enqueue a pulse.

        Closed-loop policy:
          - If a FAR person is present and the largest bbox is smaller than
            ``zoom_target_bbox_ratio * frame_h``, fire zoom-in pulses (up to
            ``zoom_max_pulses``, throttled by the cooldown) until the
            target ratio is reached.
          - If no person has been FAR for ``zoom_out_after_clear_s``, walk
            the net zoom level back to 0 one pulse per cycle.

        Args:
            persons: List of person dicts (must contain ``far`` and
                ``bbox_h_px``).
            frame_h: Source frame height in pixels (used as the bbox-ratio
                denominator).
        """
        now = time.time()
        has_far = any(p.get("far") for p in persons)
        max_bbox_h = max(
            (p.get("bbox_h_px", 0) for p in persons), default=0
        )
        target_h = self._cfg["zoom_target_bbox_ratio"] * max(frame_h, 1)

        if has_far:
            self._consec_far_frames += 1
            self._last_far_seen_t = now
        else:
            self._consec_far_frames = 0

        in_cooldown = (
            now - self._last_action_t < self._cfg["zoom_in_cooldown_s"]
        )

        # Zoom-in: a far person exists AND we haven't framed them yet.
        if (
            has_far
            and self._consec_far_frames
                >= self._cfg["zoom_in_after_far_frames"]
            and max_bbox_h < target_h
            and not in_cooldown
            and self._zoom_level < self._cfg["zoom_max_pulses"]
        ):
            log.info(
                "zoom-in: bbox=%dpx target=%.0fpx level=%d",
                int(max_bbox_h), target_h, self._zoom_level + 1,
            )
            self._enqueue("zoom-in")
            self._zoom_level += 1
            self._last_action_t = now
            self._consec_far_frames = 0

        # Zoom-out: nobody far for a while; walk back to wide one pulse at a time.
        elif (
            not has_far
            and self._zoom_level > 0
            and now - self._last_far_seen_t
                >= self._cfg["zoom_out_after_clear_s"]
            and not in_cooldown
        ):
            log.info("zoom-out: level=%d", self._zoom_level - 1)
            self._enqueue("zoom-out")
            self._zoom_level -= 1
            self._last_action_t = now

        _state["zoom_level"] = self._zoom_level
        _state["last_zoom_action_t"] = self._last_action_t

    def _enqueue(self, action: str) -> None:
        try:
            self._queue.put_nowait(action)
            _state["zoom_actions_total"] += 1
        except queue.Full:
            log.warning("zoom queue full — dropping %s", action)


# ─────────────────────────── DIGITAL FOLLOW-ZOOM ──────────────────────────────

class _FollowZoom:
    """Crop + upscale the MJPEG preview around a far person.

    Independent of optical PTZ. The detection pipeline still runs on the full
    frame, but the live preview shows a cropped, upscaled view of the largest
    far person — so the operator can see the face even if the lens hasn't
    moved much.

    Smoothing: target crop coords are blended with the previous crop via an
    exponential moving average so the view doesn't jitter frame-to-frame.

    Args:
        cfg: Reference to the module ``CFG`` dict.
        out_w: Output width (MJPEG width).
        out_h: Output height (MJPEG height).
    """

    def __init__(self, cfg: dict, out_w: int, out_h: int) -> None:
        self._cfg = cfg
        self._aspect = out_w / out_h
        self._last: Optional[tuple] = None  # (x1, y1, x2, y2) in source coords

    def _target(
        self, persons: list, fw: int, fh: int
    ) -> Optional[tuple]:
        """Compute the target crop rectangle for a frame (or None)."""
        far = [
            p for p in persons
            if p.get("far")
            and p.get("bbox_h_px", 0)
                >= self._cfg["follow_zoom_min_bbox_h_px"]
        ]
        if not far:
            return None
        # Choose the farthest person (most useful to zoom on).
        p = max(far, key=lambda p: p.get("distance_m", 0))
        x1, y1, x2, y2 = p["bbox"]
        cx = (x1 + x2) / 2
        cy = (y1 + y2) / 2
        bw = (x2 - x1) * self._cfg["follow_zoom_padding"]
        bh = (y2 - y1) * self._cfg["follow_zoom_padding"]

        # Match the MJPEG output aspect ratio so the upscale doesn't distort.
        if bw / max(bh, 1) > self._aspect:
            bh = bw / self._aspect
        else:
            bw = bh * self._aspect

        # Keep crop inside frame; if too big, scale down.
        if bw > fw:
            bh *= fw / bw
            bw = fw
        if bh > fh:
            bw *= fh / bh
            bh = fh

        cx1 = int(round(cx - bw / 2))
        cy1 = int(round(cy - bh / 2))
        cx2 = int(round(cx + bw / 2))
        cy2 = int(round(cy + bh / 2))
        # Clamp inside the frame
        if cx1 < 0:
            cx2 -= cx1; cx1 = 0
        if cy1 < 0:
            cy2 -= cy1; cy1 = 0
        if cx2 > fw:
            cx1 -= cx2 - fw; cx2 = fw
        if cy2 > fh:
            cy1 -= cy2 - fh; cy2 = fh
        cx1 = max(0, cx1); cy1 = max(0, cy1)
        return (cx1, cy1, cx2, cy2)

    def update(
        self, persons: list, fw: int, fh: int
    ) -> Optional[tuple]:
        """Return the crop rect to use this frame, or None for full frame."""
        target = self._target(persons, fw, fh)
        if target is None:
            self._last = None
            return None
        if self._last is None:
            self._last = target
        else:
            a = self._cfg["follow_zoom_smooth_alpha"]
            self._last = tuple(
                int(round(a * t + (1 - a) * l))
                for t, l in zip(target, self._last)
            )
        return self._last


def _annotate(
    frame: np.ndarray, persons: list, far_threshold_m: float
) -> np.ndarray:
    """Draw bbox + distance label for each person on a copy of ``frame``."""
    out = frame
    for p in persons:
        x1, y1, x2, y2 = p["bbox"]
        d = p["distance_m"]
        far = d >= far_threshold_m
        # orange if far (zoom candidate), green if near
        color = (0, 165, 255) if far else (0, 255, 0)
        cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
        label = f"{d:.1f}m" + ("  FAR" if far else "")
        cv2.putText(
            out, label, (x1, max(0, y1 - 8)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA,
        )
    return out


def _run_inference(model, frame_bgr: np.ndarray) -> list:
    """Run YOLO on ``frame_bgr`` (already at infer_w x infer_h) and return persons.

    Each person dict has bbox in INFER coordinates (caller must scale).
    """
    results = model.predict(
        frame_bgr,
        conf=CFG["yolo_conf"],
        iou=CFG["yolo_iou"],
        classes=[CFG["person_class_id"]],
        verbose=False,
    )[0]
    out = []
    if results.boxes is None or len(results.boxes) == 0:
        return out
    for box in results.boxes:
        xyxy = box.xyxy[0].detach().cpu().numpy().tolist()
        conf = float(box.conf[0].detach().cpu().numpy())
        out.append({"xyxy_infer": xyxy, "conf": conf})
    return out


def _maybe_init_zoom() -> Optional["_ZoomController"]:
    """Build and start a zoom controller, or return None if disabled/blocked.

    Failures here (cryptography missing, network down, bad creds) are logged
    and degrade the pipeline to detection-only — they never abort startup.
    """
    if not CFG.get("zoom_enabled"):
        log.info("zoom disabled in config; running detection-only")
        return None
    try:
        from cam2_login import session_from_env  # heavy + optional dep
    except Exception as e:
        log.warning(
            "cam2_login unavailable (%s); running detection-only", e
        )
        return None
    try:
        sess = session_from_env()
        sess.login()
        sess.start_heartbeat(interval_s=30.0)
    except Exception as e:
        log.warning("cam2 login failed (%s); running detection-only", e)
        return None
    ctrl = _ZoomController(sess, CFG)
    ctrl.start()
    _state["zoom_enabled"] = True
    log.info("zoom controller started")
    return ctrl


def main() -> None:
    """Run the cam2 loop: RTSP -> YOLO -> distance -> MJPEG/health (+ zoom)."""
    from ultralytics import YOLO  # heavy import; defer until run

    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    log.info("device=%s yolo=%s", device, CFG["yolo_model"])
    model = YOLO(CFG["yolo_model"], task="detect")

    _start_servers(CFG["mjpeg_port"], CFG["health_port"])
    zoom_ctrl = _maybe_init_zoom()
    follow_zoom = (
        _FollowZoom(CFG, CFG["mjpeg_w"], CFG["mjpeg_h"])
        if CFG.get("follow_zoom_enabled") else None
    )

    cap = open_camera(CFG["rtsp_url"])
    if not cap.isOpened():
        log.error("rtsp not yet open at start; will retry in main loop")

    target_dt = 1.0 / max(CFG["inference_fps"], 1.0)
    last_inf_t = 0.0
    persons: list = []  # last computed list, reused between inferences
    global _latest_jpeg

    while True:
        ok, frame = cap.read()
        if not ok or frame is None:
            _state["rtsp_open"] = False
            _state["errors_total"] += 1
            log.warning(
                "rtsp read failed; reopening in %.1fs",
                CFG["reconnect_delay_s"],
            )
            time.sleep(CFG["reconnect_delay_s"])
            try:
                cap.release()
            except Exception:
                pass
            cap = open_camera(CFG["rtsp_url"])
            continue

        _state["rtsp_open"] = True
        _state["frames_in"] += 1
        _state["last_frame_at"] = time.time()

        now = time.time()
        if now - last_inf_t >= target_dt:
            last_inf_t = now
            H_full, W_full = frame.shape[:2]
            infer = cv2.resize(
                frame, (CFG["infer_w"], CFG["infer_h"])
            )
            raw = _run_inference(model, infer)

            scale_x = W_full / float(CFG["infer_w"])
            scale_y = H_full / float(CFG["infer_h"])
            persons = []
            for r in raw:
                x1, y1, x2, y2 = r["xyxy_infer"]
                x1i = int(x1 * scale_x); y1i = int(y1 * scale_y)
                x2i = int(x2 * scale_x); y2i = int(y2 * scale_y)
                bbox_h = y2i - y1i
                if bbox_h < CFG["min_bbox_height_px"]:
                    continue
                d = estimate_distance_m(
                    bbox_h,
                    CFG["focal_length_px"],
                    CFG["assumed_person_height_m"],
                )
                persons.append({
                    "bbox": (x1i, y1i, x2i, y2i),
                    "conf": round(r["conf"], 3),
                    "bbox_h_px": bbox_h,
                    "distance_m": round(d, 2),
                    "far": d >= CFG["far_threshold_m"],
                })

            _state["last_persons"] = persons
            far_persons = [p for p in persons if p["far"]]
            if far_persons:
                log.info(
                    "FAR person(s) at %s",
                    [f"{p['distance_m']:.1f}m" for p in far_persons],
                )
                _log_event(
                    CFG["event_log_path"],
                    {
                        "kind": "far_person",
                        "camera_id": CFG["camera_id"],
                        "persons": far_persons,
                    },
                )

            if zoom_ctrl is not None:
                zoom_ctrl.update(persons, frame_h=H_full)

        annotated = _annotate(frame, persons, CFG["far_threshold_m"])

        # Digital follow-zoom: when a far person is present, crop around them
        # before scaling to the MJPEG output. Detection above already used
        # the full frame, so this only affects what the operator sees.
        crop_src = annotated
        if follow_zoom is not None:
            rect = follow_zoom.update(
                persons, frame.shape[1], frame.shape[0]
            )
            if rect is not None:
                x1, y1, x2, y2 = rect
                if x2 > x1 and y2 > y1:
                    crop_src = annotated[y1:y2, x1:x2]

        # Downscale + JPEG-encode for the MJPEG preview only.
        if (
            crop_src.shape[1] != CFG["mjpeg_w"]
            or crop_src.shape[0] != CFG["mjpeg_h"]
        ):
            preview = cv2.resize(
                crop_src, (CFG["mjpeg_w"], CFG["mjpeg_h"]),
                interpolation=cv2.INTER_LINEAR,
            )
        else:
            preview = crop_src
        ok_enc, jpg = cv2.imencode(
            ".jpg", preview,
            [int(cv2.IMWRITE_JPEG_QUALITY), CFG["mjpeg_quality"]],
        )
        if ok_enc:
            with _latest_lock:
                _latest_jpeg = jpg.tobytes()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("shutdown (SIGINT)")
        sys.exit(0)
