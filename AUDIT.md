# Security Pipeline — Comprehensive Audit

Produced: 2026-04-23
Branch: `agent/auto-dev`
Scope: all `.py` files in the repo root. `.venv/` and model binaries excluded.

---

## 1. ENTRY POINT RESOLUTION

**Canonical entry point: `detect.py`.** High confidence.

Evidence:

| Signal | detect.py | detect_v2.py |
|---|---|---|
| Referenced by `security_pipeline.service` | ✅ `ExecStart=… detect.py` (line 10 of the unit) | ❌ not referenced |
| File mtime | 2026-04-17 14:14:04 | 2026-04-17 11:54:39 (~2 h older) |
| Line count | 934 | 893 |
| Git history | single baseline commit `0b512a4` | single baseline commit `0b512a4` |
| Behavior labels wired into `draw_tracks` | ✅ yes (line 899: `draw_tracks(vis, last_tracks, beh_labels)`) | ❌ no (calls `draw_tracks(vis, last_tracks)` only) |
| `pose_engine` default | `models/yolov8n-pose.pt` (line 64) | `models/yolov8n-pose.engine` |

Git history is not useful here — both files landed in the same single baseline commit (`0b512a4 Baseline before agent automation`) and there is no `--follow` rename relationship between them.

**Concrete differences (diff of `detect_v2.py` → `detect.py`, 84 lines):**

1. `detect.py` adds a module-level helper `_clean_behavior_label(v)` at lines 21–45 (strips `???`, trailing "unknown", whitespace; returns `"unknown"` on any error). Not present in `detect_v2.py`.
2. `detect.py` changes `CFG["pose_engine"]` from `models/yolov8n-pose.engine` to `models/yolov8n-pose.pt` (line 64). The `.pt` blob is present in the repo; no `.engine` has been built.
3. `draw_tracks` in `detect.py` accepts a `behavior_labels` dict and picks a behavior string (cleaned through `_clean_behavior_label`) over the default class/id/conf string (lines 640–662). `detect_v2.py` only renders `f"{name} #{tid} {conf:.0%}"`.
4. `draw_faces` in `detect.py` double-applies `_clean_behavior_label` to the face label inside `try/except Exception: pass` (lines 664–673), tagged with comment `# AUTO_CLEAN_LABEL`. `detect_v2.py` has no such block.
5. Main loop in `detect.py` passes `beh_labels` into `draw_tracks`; `detect_v2.py` does not.

**Interpretation.** `detect.py` is `detect_v2.py` with a behavior-label cleanup patch layered on top. The `AUTO_CLEAN_LABEL` marker plus the duplicate-call (`_clean_behavior_label(_clean_behavior_label(label))`) strongly suggest an automated tool applied this patch. `detect_v2.py` is an older snapshot — it should be treated as dead and either deleted or renamed to `detect.py.pre_autoclean` for clarity. Flagging in §7.

---

## 2. PIPELINE MAP

Data flow through `detect.py` (canonical). Functions/classes are listed with their defining file and line.

| # | Stage | Function / Class | File:line |
|---|---|---|---|
| 1 | Capture (RTSP frame grab + low-latency drain) | `open_camera`, `reopen_camera`, inline `cam.grab()` / `cam.read()` in main loop | `detect.py:592`, `detect.py:601`, `detect.py:753–767` |
| 2 | Pre-resize | `cv2.resize(raw, (resize_w, resize_h))` inline | `detect.py:769` |
| 3 | Object detection (YOLOv9-C) | `load_yolo` + direct `yolo(frame, …)` call in main loop | `detect.py:324`, `detect.py:785–790` |
| 4 | Class filtering | inline membership test against `ALLOWED_CLASSES` | `detect.py:112`, `detect.py:800` |
| 5 | Multi-object tracking (ByteTrack) | `_init_bytetrack`, `tracker.update(dets, frame)` | `detect.py:335`, `detect.py:807` |
| 6 | Pose + behavior analysis | `BehaviorAnalyzer.submit` (detect side), `BehaviorAnalyzer._worker` → `PoseEstimator.infer` → `ActionClassifier.classify` → `predict_next` → alert emission | `behavior.py:303`, `behavior.py:321`, `behavior.py:241`, `behavior.py:110`, `behavior.py:72` |
| 7 | Scene summary / detection alerts | inline class-count diff + per-object dedup block | `detect.py:815–888` |
| 8 | Zone / loitering rules | `ZoneEngine.update` | `detect.py:562`, called at `detect.py:865` |
| 9 | Face detection (SCRFD) | `SCRFDDetector.detect` | `detect.py:352` — **not wired into main loop** (see §3) |
| 10 | Face embedding (AdaFace) | `AdaFaceEmbedder.embed` | `detect.py:393` — **not wired** |
| 11 | Identity search (Faiss) | `WatchlistDB.search` | `detect.py:427` — **not wired** |
| 12 | Audio event classification (YAMNet) | `YAMNetAudio.classify`, thread via `start_audio_thread` | `detect.py:496`, `detect.py:532` — **defined but never called from `run()`** |
| 13 | Visual anomaly (PatchCore) | `PatchCoreAnomaly.score` | `detect.py:457` — **not wired** |
| 14 | Annotation | `draw_tracks`, `draw_behavior`, `draw_faces`, `draw_anomaly`, `draw_hud` | `detect.py:640`, `behavior.py:465`, `detect.py:664`, `detect.py:675`, `detect.py:680` |
| 15 | MJPEG streaming | `_FrameBus`, `_MJPEGHandler`, `start_mjpeg` | `detect.py:144`, `detect.py:164`, `detect.py:187` |
| 16 | Alert emission (MQTT) | `emit_alert` → `_alert_q` → `_mqtt_worker` thread | `detect.py:308`, `detect.py:250`, `detect.py:253` |
| 17 | Health endpoint | `_HealthHandler`, `start_health`, `_hset` | `detect.py:195`, `detect.py:211`, `detect.py:137` |
| 18 | Thermal monitor | `_read_thermal_zone`, `start_thermal_monitor` | `detect.py:219`, `detect.py:227` |

Shutdown path: `signal.signal(SIGINT, SIGTERM → _stop)` flips a `running` flag; `finally:` releases the camera, calls `beh_analyzer.stop()`, sets `_health[status]="stopped"`. Daemon threads (mqtt, mjpeg, health, thermal, audio-if-wired, behavior) die with the process.

---

## 3. DISABLED / GATED FEATURES

### 3.1 SCRFD face detection (hard-coded `None`)

- **File:line:** `detect.py:710`
- **What:** Instantiates `SCRFDDetector` to detect faces in each inference frame.
- **Why disabled (code comment):** `# SCRFDDetector(cfg["scrfd_model"])  — re-enable when stable` — combined with the CPU-provider workaround at `detect.py:359` (`# Force CPU provider to avoid ORT crash on this Jetson`) and the main-loop block marker at `detect.py:890` (`# ── FACE (disabled until ORT is stable on this Jetson) ─────`).
- **Safe re-enable steps:**
  1. Confirm `onnxruntime-gpu` from JetPack wheel is installed (CLAUDE.md warns this cannot come from PyPI).
  2. Change `detect.py:710` to `face_det = SCRFDDetector(cfg["scrfd_model"])` and similarly for `face_emb = AdaFaceEmbedder(...)` and `watchlist = WatchlistDB(...)`.
  3. Uncomment the main-loop face branch and implement it fully (currently `detect.py:891` is `# if face_det and n_frame % cfg["face_every_n"] == 0: ...` — the body is a literal ellipsis comment). Populate `last_faces`, `last_labels` each time.
  4. Set `cfg["face_every_n"]` to a sane value (currently `999999`, `detect.py:85`). Make it a config toggle so the path can be disabled again (per CLAUDE.md rule #13).
  5. Verify SCRFD stays on `CPUExecutionProvider` until ORT GPU stability is confirmed, then reintroduce `CUDAExecutionProvider` in the provider list and retest.

### 3.2 AdaFace embedding (hard-coded `None`)

- **File:line:** `detect.py:711` (`face_emb = None`)
- **What:** 512-d face embedder feeding the Faiss watchlist.
- **Why:** Gated behind face detection — no embedder needed while face_det is off.
- **Safe re-enable steps:** enabled together with §3.1; same ORT-wheel prerequisite.

### 3.3 Faiss watchlist (hard-coded `None`)

- **File:line:** `detect.py:712` (`watchlist = None`)
- **What:** 1:N identity search.
- **Why:** No embeddings to search without §3.2.
- **Safe re-enable steps:** `WatchlistDB` already gracefully returns `(None, 0.0)` if the index is missing, so instantiation is safe. Build the index first via `manage_watchlist.py add-dir` before relying on matches.

### 3.4 PatchCore visual anomaly (hard-coded `None`)

- **File:line:** `detect.py:713` (`patchcore = None`) + `detect.py:893` main-loop comment.
- **What:** Memory-bank cosine-distance anomaly score per frame.
- **Why disabled (code comment):** `# ── ANOMALY (disabled until patchcore is built) ─────`. `build_patchcore.py` must run once to produce `models/patchcore_memory.pt`.
- **Safe re-enable steps:**
  1. Run `python build_patchcore.py --source <normal_clip.mp4> --frames 500`.
  2. Replace `patchcore = None` with `patchcore = PatchCoreAnomaly(cfg["patchcore_model"])`.
  3. Implement the main-loop branch (`detect.py:894` is a `...` placeholder) — call `last_anomaly = patchcore.score(frame)` and gate `emit_alert` on `last_anomaly > cfg["anomaly_thresh"]`.
  4. Set `cfg["anomaly_every_n"]` from `999999` to e.g. `30`. Keep behind config toggle.
  5. **Security:** `PatchCoreAnomaly.__init__` (`detect.py:462`) calls `torch.load(..., weights_only=False)`. This is RCE-unsafe if the `.pt` file ever comes from an untrusted source. Either trust only locally built files or migrate to `weights_only=True` + explicit state-dict load. See §5 risk #8.

### 3.5 YAMNet audio pipeline (thread defined but never started)

- **File:line:** `detect.py:532` defines `start_audio_thread`; `run()` (`detect.py:688` onward) never calls it. `yamnet = None` is set at `detect.py:714` and stays that way.
- **What:** Microphone capture → YAMNet classification → alerts for `Gunshot`, `Glass break`, `Screaming`, `Alarm`, `Siren`.
- **Why disabled (code comment):** no explicit comment. Inferred from absence of wiring. The `sounddevice` import inside the thread suggests it was gated on audio hardware availability.
- **Safe re-enable steps:**
  1. **Pre-req:** add `audio_sample_rate` and `audio_window_ms` to `CFG` (currently missing — `start_audio_thread` reads `cfg["audio_sample_rate"]` at line 536, which would `KeyError` today).
  2. Load the model: `yamnet = YAMNetAudio(cfg["yamnet_model"])`.
  3. Call `start_audio_thread(yamnet, cfg["camera_id"], cfg)` from `run()` alongside the other `start_*` helpers around `detect.py:690–693`.
  4. Verify the JetPack `tflite-runtime` wheel is installed (CLAUDE.md warns against PyPI).
  5. Add a config toggle `cfg["audio_enabled"]` so the whole audio stack can be disabled.

### 3.6 Pose engine format mismatch

- **File:line:** `detect.py:64`
- **What:** `pose_engine` points at `models/yolov8n-pose.pt` (PyTorch weights) rather than the TensorRT `.engine` that `export_pose_model.py` produces. Pose runs via the ultralytics PyTorch path, not TRT — fine functionally, but loses the advertised "TRT FP16 ~8ms" advantage.
- **Why:** Most likely the `.engine` build failed or is missing (git-tracked file `models/yolov8n-pose.engine` exists in `git ls-files`, but the config was explicitly switched to `.pt` in the `detect_v2.py → detect.py` diff).
- **Safe re-enable steps:** run `python export_pose_model.py` after confirming trtexec is on `PATH`; switch `cfg["pose_engine"]` back to `.engine`; re-warm.

---

## 4. TODO / FIXME / HACK INVENTORY

`grep -nE "TODO|FIXME|XXX|HACK|NOTE:|NOTE "` across all `.py` files returned **zero matches**. There are no conventional task markers.

Two non-standard markers that serve a similar role:

### 4.1 `# AUTO_CLEAN_LABEL`  (detect.py:667)
Context:
```
  665  def draw_faces(frame, faces, labels):
  666      for (x1,y1,x2,y2,_), label in zip(faces, labels):
  667          cv2.rectangle(frame, (x1,y1), (x2,y2), (255,200,0), 2)
  668          # AUTO_CLEAN_LABEL
  669          try:
  670              label = _clean_behavior_label(_clean_behavior_label(label))
  671          except Exception:
  672              pass
  673          cv2.putText(frame, label, (x1, y1-6),
```
Marker left by an automated patch. The double-`_clean_behavior_label` call is redundant — idempotent function applied twice.

### 4.2 `# Sitting: auto rule`  (behavior.py:171)
Context:
```
  169              if torso_ratio >= 0.18 and knee_hip_gap < 0.22 and speed < 0.08:
  170                  return "sitting"
  171              # Sitting: auto rule
  172  
  173              knee_hip_gap = abs(kne_y - hip_y) / total_h
  174  
  175              if 0.16 <= torso_ratio <= 0.60 and knee_hip_gap < 0.42 and speed < 0.14:
  176  
  177                  return "sitting"
```
A second "sitting" rule block was appended below the first with looser thresholds. The `knee_hip_gap` local is also recomputed identically. See §5 risk #5.

### 4.3 Soft-disabled markers already covered in §3
- `detect.py:710` — `# SCRFDDetector(cfg["scrfd_model"])  — re-enable when stable`
- `detect.py:890` — `# ── FACE (disabled until ORT is stable on this Jetson) ─────`
- `detect.py:891` — `# if face_det and n_frame % cfg["face_every_n"] == 0: ...`
- `detect.py:893` — `# ── ANOMALY (disabled until patchcore is built) ─────`
- `detect.py:894` — `# if patchcore and n_frame % cfg["anomaly_every_n"] == 0: ...`
- `detect.py:85`  — `"face_every_n":  999999,    # set to e.g. 3 when face pipeline is stable`
- `detect.py:87`  — `"anomaly_every_n": 999999,  # set to e.g. 30 when patchcore is built`
- `detect.py:63`  — `"yolo_engine": "models/yolov9c.pt",      # swap to .engine after trtexec`
- `detect.py:359` — `# Force CPU provider to avoid ORT crash on this Jetson`

---

## 5. RISK REGISTER

Ranked highest-to-lowest. Line numbers are where the defect lives.

### CRITICAL

**1. YAMNet audio pipeline is advertised but never started.** — `detect.py:714` + `detect.py:532`.
`run()` sets `yamnet = None` and never calls `start_audio_thread`. Additionally, `start_audio_thread` would `KeyError` on `cfg["audio_sample_rate"]` (not defined in `CFG`) if it ever were called. Gunshot / scream / alarm detection — the scariest events — do not fire.
**Fix (one-liner):** add `audio_sample_rate`/`audio_window_ms` to `CFG`, load `yamnet = YAMNetAudio(cfg["yamnet_model"])`, then `start_audio_thread(yamnet, cfg["camera_id"], cfg)` in `run()`.

### HIGH

**2. `NameError` inside `behavior.draw_behavior` silently suppresses the overlay.** — `behavior.py:481`. **RESOLVED 2026-04-23, `94cb671`.**
```
label = str(label).split("???")[0].strip() if label else "unknown"
```
`label` is never bound in this function; `info`, `action`, `nxt` are. This raises `NameError` on every call with non-empty `labels`. In `detect.py:902–905` the call is wrapped in `try/except Exception: pass`, so behavior labels never render — silently.
**Fix:** replace `label = ...` with `label = str(action).split("???")[0].strip() if action else "unknown"` (or drop the line; `action` is already cleaned upstream).
**Resolution:** Session 2. Chose Option Y (full rename): both line 481 and line 482 now use `action`, eliminating the vestigial `label` variable entirely. Defensive `split("???")`/`strip`/`else "unknown"` preserved as belt-and-suspenders per user decision. MJPEG overlay rendering not visually verified in terminal session — follow-up unit test candidate per §6 #6.

**3. Zone loitering collapses all untracked objects into `track_id=0`.** — `detect.py:865`.
```
zones.update(track_id if track_id >= 0 else 0, cx, cy, cfg["camera_id"])
```
`ZoneEngine.dwell` is keyed on track_id. When ByteTrack is unavailable (its init is wrapped in `try/except` and returns `None`), every untracked object shares id 0 — their dwell timers overwrite each other, producing either false loitering alerts or none at all.
**Fix:** skip `zones.update` entirely when `track_id < 0`, or synthesize a stable key from `object_key`.

**4. No exception guard around YOLO inference or `tracker.update`.** — `detect.py:785–807`.
Transient CUDA OOM, TRT engine fault, or malformed frame raises out of the main loop; the pipeline dies and relies on systemd `Restart=always` (5-s backoff) to come back up. Every restart re-warms YOLO (3 dummy passes at `detect.py:328`) and reconnects MQTT, so recovery costs seconds of missed frames.
**Fix:** wrap lines 785–807 in `try/except Exception as e: log.exception(...); continue` so the loop survives.

**5. Duplicate "sitting" classifier rules produce unstable labels.** — `behavior.py:167–177`.
Two `if … return "sitting"` blocks with different thresholds are stacked; the first (strict) runs, and if it returns, the second (loose) is unreachable. If the strict one is ever removed or reordered, the loose one fires much more frequently. Also `"sitting"` is not a member of the `Action` taxonomy, so downstream Markov / alert logic falls through to `UNKNOWN` transitions.
**Fix:** pick one rule, add `Action.SITTING`, add it to `_TRANSITIONS` and `ACTION_COLORS`.

### MEDIUM

**6. `_FrameBus._event` is never cleared — MJPEG consumers spin.** — `detect.py:144–160`.
```
def put(self, jpg): ...; self._event.set()
def get(self, timeout=2.0):
    self._event.wait(timeout); ...; return self._jpg
```
After the first frame, `wait` returns instantly forever. A connected MJPEG client loops at main-loop speed emitting stale JPEGs between producer updates, burning CPU and network for duplicated frames.
**Fix:** make `_FrameBus` block-on-new — use a `threading.Condition` or clear the event inside `get` and re-set only in `put`.

**7. `paho-mqtt>=1.6.1` callback signatures assume v1.** — `detect.py:258/261/269`, `dashboard_server.py:307/258/268`.
`_on_connect(c, ud, flags, rc)` and `mqtt.Client(client_id=..., protocol=...)` are v1 APIs. `paho-mqtt>=1.6.1` resolves to 2.x on a fresh PyPI install and 2.x changes both. A re-install of the venv will break MQTT silently (`on_connect` is never called, `client_id` kwarg is rejected).
**Fix:** pin `paho-mqtt<2`, or migrate to `mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=...)`.

**8. `torch.load(..., weights_only=False)` on the PatchCore memory bank.** — `detect.py:462`.
If `models/patchcore_memory.pt` is ever overwritten from an untrusted source, this is arbitrary code execution at pipeline start. Low probability in current setup (file is locally built) but high impact.
**Fix:** save/load as a plain `numpy.savez` or use `weights_only=True` with explicit tensor reconstruction.

**9. `_mqtt_worker` initial-connect loop never sees shutdown.** — `detect.py:276–284`.
If the broker is down at startup, the worker `while True: client.connect(...); time.sleep(5)` loops forever. A SIGTERM flips `running=False` in main, but the MQTT thread has no reference to that flag. It's a daemon thread so it dies with the process — but no final log line or drain attempt runs.
**Fix:** pass a stop-event and check it in the retry loop; also drain the alert queue on shutdown.

**10. `thermal_monitor` crash kills thermal alerting silently.** — `detect.py:227–246`. **RESOLVED 2026-04-23, `0f6cfee`.**
The loop body has no `try/except`. If `_read_thermal_zone` starts returning non-ints (e.g., a BSP update changes the sysfs format), or `emit_alert` ever raises, the thread exits and there is no log record at that level to tell you thermal monitoring is gone. Same pattern in `_mqtt_worker`'s publish loop (`detect.py:286–305` — protected) and in `BehaviorAnalyzer._worker` (`behavior.py:321` — catches implicitly via the pose `try`, but not the classify path).
**Fix:** wrap the thermal loop body in `try/except Exception: log.exception(...); continue`.
**Resolution:** Session 2. Loop body wrapped in `try/except Exception` with `log.exception("THERMAL MONITOR error — continuing")`. `time.sleep(15)` kept outside the try block so both success and exception paths observe the normal 15-second cadence (no tight-spin on repeated failures). No explicit `continue` needed — natural fall-through preserves timing. Greppable prefix matches Fix A's "TRACKING DISABLED" convention.

---

## 6. TEST SURFACE

Ten functions that are pure enough to exercise without GPU, camera, MQTT broker, or audio device. Numbered in order I'd write them.

| # | File:function | Why it's cheap |
|---|---|---|
| 1 | `detect.py:_clean_behavior_label` (line 23) | Pure string cleaner; no imports beyond `re`. Cases: `None`, `""`, `"standing???unknown"`, `"???"`, `"walking unknown"`, `"  running  "`. |
| 2 | `detect.py:iou_xyxy` (line 631) | Pure float math on 4-tuples. Cases: identical, disjoint, partial overlap, degenerate zero-area boxes. |
| 3 | `behavior.py:_iou` (line 442) | Same signature, separate impl — good to assert parity with #2. |
| 4 | `behavior.py:predict_next` (line 72) | Pure dict lookup against `_TRANSITIONS`. Cases: every `Action.*` key, plus an unknown string falls back to `UNKNOWN`. |
| 5 | `behavior.py:TrackWindow.push` + `TrackWindow.should_alert` (lines 90, 95) | In-memory `deque` / `dict`; `should_alert` cooldown testable by monkeypatching `time.time` or tolerance windows. |
| 6 | `behavior.py:ActionClassifier.classify` (line 110) | Takes a `TrackWindow`. Synthesize bbox histories that trigger FALLEN (aspect > 1.6), RUNNING (speed > 0.2), STANDING (speed ≈ 0). No GPU needed — keypoints optional. |
| 7 | `detect.py:ZoneEngine.update` (line 572) | Only dependency is `cv2.pointPolygonTest` (CPU). Monkeypatch `emit_alert` with a capture-list and assert it fires after `dwell_sec`. |
| 8 | `dashboard_server.py:format_event_for_ui` (line 173) | Pure dict/string transform. Exercise each `kind` branch (`detection_summary`, `detection`, `loitering`, `audio`, `anomaly`, unknown). |
| 9 | `dashboard_server.py:db_insert` + `db_query` (lines 82, 95) | Point `DB_PATH` at a `tempfile.NamedTemporaryFile`. Insert → query-by-kind, by-camera, by-time-range. No MQTT needed. |
| 10 | `build_patchcore.py:extract_patches` (line 26) | Numpy-only. Feed a synthetic `(360, 640, 3) uint8` array; assert output shape `(patches, 768)` and L2-norms ≈ 1.0. |

Suggested layout: `tests/test_detect_helpers.py`, `tests/test_behavior.py`, `tests/test_dashboard.py`, `tests/test_patchcore.py`. Add `pytest` + `pytest-asyncio` (if WS tests follow later) to dev deps. `pytest` itself is not on `requirements.txt` and is safe to install via PyPI (not a Jetson-specific wheel).

---

## 7. OPEN QUESTIONS

1. **`detect_v2.py` disposition.** It's an older copy of `detect.py` minus the `_clean_behavior_label` patch. Delete it, rename to `detect.py.pre_autoclean_20260417`, or preserve as a reference implementation for diffs? CLAUDE.md flags this as "uncertain" but leaving two 900-line near-duplicates invites editing the wrong file.

2. **Tracked `.broken` / `.bak` files violate the agent rules.** `git ls-files` shows:
   - `behavior.py.broken_20260417_125938` (committed, 20 kB)
   - `dashboard_static/index.html.bak2`, `.bak_20260417_120302`, `.bak_20260417_120900`, `.bak_spam_fix`, `.bak_ui_cleanup`
   - `dashboard_static/index.html.bak_20260417_120302` and siblings

   CLAUDE.md rule #1 forbids creating these going forward, but the existing ones are committed. Should I open a cleanup PR that deletes them? `.gitignore` already lists the patterns, but `git rm` is still needed. Not touched in this audit per "do not modify any existing files".

3. **Audio subsystem: intentional or forgotten?** `start_audio_thread` is written in full, but never called, and `CFG` is missing the two keys it needs. Was this cut last-minute for a reason (mic not wired? false-positive rate too high?), or is it simply an incomplete wiring?

4. **`paho-mqtt` version actually installed in `.venv`.** `requirements.txt` says `>=1.6.1`. v2.x breaks the callback signatures. Want me to `pip show paho-mqtt` under the venv and report, or is this already known?

5. **Duplicate "sitting" rule in `behavior.py:167–177`.** Which threshold set is intended — the strict one (lines 168–169) or the loose one (lines 175–177)? The loose block is currently unreachable because the strict one returns first. Also `"sitting"` isn't in the `Action` taxonomy — was the intent to add it, or to fold it into `CROUCHING`?

6. **`face_every_n=999999` AND SCRFD hardcoded to `None` AND main-loop branch commented — triple-gated.** The config sentinel is meaningless while the other two gates exist. When re-enabling face detection (per CLAUDE.md rule #13), do you want one primary toggle (e.g., `cfg["face_enabled"]`) with the other two reduced to config-driven rates?

7. **Canonical pose model.** `cfg["pose_engine"]` is `.pt` in `detect.py` but `export_pose_model.py` produces `.engine` and `models/yolov8n-pose.engine` is tracked. Should I build the TRT engine and switch the config, or is the `.pt` path deliberate (e.g., engine was unreliable on this JetPack)?

8. **Scope of risk-register fixes.** Several items (paho version pin, `weights_only=False`, missing try/except around YOLO) are low-churn and high-value. Want me to send a single bundled PR for all of them, one PR per risk, or wait for a per-item sign-off?
