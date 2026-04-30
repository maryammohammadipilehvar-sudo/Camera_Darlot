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
import socketserver
import sys
import threading
import time
from pathlib import Path
from typing import Optional

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


def main() -> None:
    """Run the cam2 Phase-1 loop: RTSP -> YOLO -> distance -> MJPEG/health."""
    from ultralytics import YOLO  # heavy import; defer until run

    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    log.info("device=%s yolo=%s", device, CFG["yolo_model"])
    model = YOLO(CFG["yolo_model"], task="detect")

    _start_servers(CFG["mjpeg_port"], CFG["health_port"])

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

        annotated = _annotate(frame, persons, CFG["far_threshold_m"])
        # Downscale + JPEG-encode for the MJPEG preview only. Detection still
        # used the full-resolution frame above, so far-person reach isn't lost.
        if (
            annotated.shape[1] != CFG["mjpeg_w"]
            or annotated.shape[0] != CFG["mjpeg_h"]
        ):
            preview = cv2.resize(
                annotated, (CFG["mjpeg_w"], CFG["mjpeg_h"]),
                interpolation=cv2.INTER_AREA,
            )
        else:
            preview = annotated
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
