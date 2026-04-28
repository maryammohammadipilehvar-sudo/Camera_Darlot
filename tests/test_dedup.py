"""Unit tests for the severity-aware dedup + track-loss re-fire helpers.

These exercise the internal dedup module of detect.py directly. They do
not subprocess-launch the pipeline, so they are fast (<1s total) and
require no GPU, MQTT broker, camera, or model weights.
"""

from __future__ import annotations

import sys
import time
import types
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _import_detect_minimal():
    """Import detect.py without firing its heavy startup paths.

    detect.py is import-clean — no top-level model loads, no thread
    starts. This wrapper exists so any future startup-time side effects
    can be stubbed in one place.
    """
    import detect  # noqa: F401  — exposes the module for the tests below
    return detect


@pytest.fixture(autouse=True)
def _isolate_dedup_state():
    """Clear all dedup module state before each test.

    The state is intentionally module-global in production (one pipeline
    process per host); under pytest we own the process so we reset
    between tests rather than refactor the production module.
    """
    detect = _import_detect_minimal()
    detect._dedup_track.clear()
    detect._dedup_zone.clear()
    detect._dedup_burst.clear()
    detect._dedup_known_tracks.clear()
    detect._last_dedup_prune = 0.0
    yield
    detect._dedup_track.clear()
    detect._dedup_zone.clear()
    detect._dedup_burst.clear()
    detect._dedup_known_tracks.clear()


def _cfg(
    *,
    track_window_s: float = 300.0,
    zone_window_s: float = 120.0,
    burst_window_s: float = 30.0,
    burst_threshold: int = 3,
    track_window_by_severity_s: "dict | None" = None,
    zone_window_by_severity_s: "dict | None" = None,
    track_idle_s: float = 5.0,
    clear_zone_on_track_loss: bool = True,
) -> dict:
    """Build a minimal cfg dict with just the dedup branch the tests need."""
    return {
        "dedup": {
            "track_window_s":  track_window_s,
            "zone_window_s":   zone_window_s,
            "burst_window_s":  burst_window_s,
            "burst_threshold": burst_threshold,
            "track_window_by_severity_s": track_window_by_severity_s,
            "zone_window_by_severity_s":  zone_window_by_severity_s,
            "track_idle_s":             track_idle_s,
            "clear_zone_on_track_loss": clear_zone_on_track_loss,
        }
    }


def test_window_for_severity_falls_back_to_base_when_map_absent():
    detect = _import_detect_minimal()
    assert detect._window_for_severity(300.0, "CRITICAL", None) == 300.0
    assert detect._window_for_severity(300.0, None, {"CRITICAL": 30}) == 300.0


def test_window_for_severity_uses_map_when_present():
    detect = _import_detect_minimal()
    m = {"CRITICAL": 30, "HIGH": 60, "MEDIUM": 180}
    assert detect._window_for_severity(300.0, "critical", m) == 30.0  # case-insensitive
    assert detect._window_for_severity(300.0, "HIGH", m) == 60.0
    assert detect._window_for_severity(300.0, "LOW", m) == 300.0      # missing → fallback


def test_window_for_severity_handles_bad_value():
    detect = _import_detect_minimal()
    m = {"CRITICAL": "not-a-number"}
    assert detect._window_for_severity(300.0, "CRITICAL", m) == 300.0


def test_dedup_filter_severity_aware_track_window(monkeypatch):
    """A CRITICAL alert should re-fire faster than the flat 300s base.

    Both per-severity maps are provided so the zone window doesn't shadow
    what we're trying to assert about the track window.
    """
    detect = _import_detect_minimal()
    cfg = _cfg(
        track_window_by_severity_s={"CRITICAL": 30, "LOW": 300},
        zone_window_by_severity_s={"CRITICAL": 20, "LOW": 120},
    )

    fake_now = [1000.0]
    monkeypatch.setattr(detect.time, "time", lambda: fake_now[0])

    # First fire — passes.
    res, _ = detect._dedup_filter(
        "cam_01", "intrusion", {"track_id": 7}, "main", cfg, severity="CRITICAL",
    )
    assert res is None

    # 25s later — still inside the 30s CRITICAL track window → suppressed.
    fake_now[0] += 25
    res, _ = detect._dedup_filter(
        "cam_01", "intrusion", {"track_id": 7}, "main", cfg, severity="CRITICAL",
    )
    assert res == "suppressed_dedup_track"

    # 31s after first fire — past both the 30s track window and the 20s
    # zone window for CRITICAL → re-fires.
    fake_now[0] = 1031.0
    res, _ = detect._dedup_filter(
        "cam_01", "intrusion", {"track_id": 7}, "main", cfg, severity="CRITICAL",
    )
    assert res is None


def test_dedup_filter_low_severity_keeps_long_window(monkeypatch):
    """A LOW alert under the same map keeps the 300s default window."""
    detect = _import_detect_minimal()
    cfg = _cfg(
        track_window_by_severity_s={"CRITICAL": 30, "LOW": 300},
    )

    fake_now = [2000.0]
    monkeypatch.setattr(detect.time, "time", lambda: fake_now[0])

    res, _ = detect._dedup_filter(
        "cam_01", "noise", {"track_id": 1}, "main", cfg, severity="LOW",
    )
    assert res is None

    # 200s later — still inside the 300s LOW window.
    fake_now[0] += 200
    res, _ = detect._dedup_filter(
        "cam_01", "noise", {"track_id": 1}, "main", cfg, severity="LOW",
    )
    assert res == "suppressed_dedup_track"


def test_retire_track_clears_track_and_zone_entries(monkeypatch):
    detect = _import_detect_minimal()
    cfg = _cfg()

    fake_now = [3000.0]
    monkeypatch.setattr(detect.time, "time", lambda: fake_now[0])

    detect._dedup_filter(
        "cam_01", "intrusion", {"track_id": 5}, "main", cfg, severity="CRITICAL",
    )
    assert ("cam_01", 5, "intrusion") in detect._dedup_track
    assert ("cam_01", "main", "intrusion") in detect._dedup_zone

    detect._dedup_retire_track("cam_01", 5, clear_zone=True)
    assert ("cam_01", 5, "intrusion") not in detect._dedup_track
    assert ("cam_01", "main", "intrusion") not in detect._dedup_zone


def test_retire_track_preserves_zone_when_flag_off(monkeypatch):
    detect = _import_detect_minimal()
    cfg = _cfg()

    fake_now = [4000.0]
    monkeypatch.setattr(detect.time, "time", lambda: fake_now[0])

    detect._dedup_filter(
        "cam_01", "intrusion", {"track_id": 9}, "main", cfg, severity="HIGH",
    )
    detect._dedup_retire_track("cam_01", 9, clear_zone=False)
    assert ("cam_01", 9, "intrusion") not in detect._dedup_track
    assert ("cam_01", "main", "intrusion") in detect._dedup_zone


def test_observe_tracks_retires_idle_tracks(monkeypatch):
    """A track absent for >track_idle_s gets retired automatically."""
    detect = _import_detect_minimal()
    cfg = _cfg(track_idle_s=5.0)

    fake_now = [5000.0]
    monkeypatch.setattr(detect.time, "time", lambda: fake_now[0])

    # Fire from track 11.
    detect._dedup_filter(
        "cam_01", "intrusion", {"track_id": 11}, "main", cfg, severity="HIGH",
    )
    detect._dedup_observe_tracks("cam_01", {11}, fake_now[0], cfg)
    assert ("cam_01", 11) in detect._dedup_known_tracks

    # 10s later, no tracks present — idle exceeded → track retired.
    fake_now[0] += 10
    detect._dedup_observe_tracks("cam_01", set(), fake_now[0], cfg)
    assert ("cam_01", 11) not in detect._dedup_known_tracks
    assert ("cam_01", 11, "intrusion") not in detect._dedup_track


def test_observe_tracks_keeps_active_tracks(monkeypatch):
    """A track that keeps being reported never expires."""
    detect = _import_detect_minimal()
    cfg = _cfg(track_idle_s=5.0)

    fake_now = [6000.0]
    monkeypatch.setattr(detect.time, "time", lambda: fake_now[0])

    detect._dedup_filter(
        "cam_01", "intrusion", {"track_id": 21}, "main", cfg, severity="HIGH",
    )

    for _ in range(20):
        detect._dedup_observe_tracks("cam_01", {21}, fake_now[0], cfg)
        fake_now[0] += 1.0

    assert ("cam_01", 21) in detect._dedup_known_tracks
    assert ("cam_01", 21, "intrusion") in detect._dedup_track


def test_observe_tracks_zero_idle_disables_retirement(monkeypatch):
    detect = _import_detect_minimal()
    cfg = _cfg(track_idle_s=0.0)

    fake_now = [7000.0]
    monkeypatch.setattr(detect.time, "time", lambda: fake_now[0])

    detect._dedup_filter(
        "cam_01", "intrusion", {"track_id": 33}, "main", cfg, severity="HIGH",
    )
    detect._dedup_observe_tracks("cam_01", set(), fake_now[0] + 1000, cfg)
    # Track entry survives because the ledger is disabled.
    assert ("cam_01", 33, "intrusion") in detect._dedup_track


def test_track_loss_then_new_track_re_fires(monkeypatch):
    """End-to-end: track 5 fires, leaves, track 6 enters and re-fires."""
    detect = _import_detect_minimal()
    cfg = _cfg(
        track_window_s=300.0,
        zone_window_s=120.0,
        track_window_by_severity_s={"CRITICAL": 30, "HIGH": 60},
        zone_window_by_severity_s={"CRITICAL": 20, "HIGH": 60},
        track_idle_s=2.0,
        clear_zone_on_track_loss=True,
    )

    fake_now = [8000.0]
    monkeypatch.setattr(detect.time, "time", lambda: fake_now[0])

    # Frame 1 — track 5 enters and fires.
    detect._dedup_observe_tracks("cam_01", {5}, fake_now[0], cfg)
    res, _ = detect._dedup_filter(
        "cam_01", "intrusion", {"track_id": 5}, "main", cfg, severity="CRITICAL",
    )
    assert res is None

    # Frames 2-N — track 5 still present, would-be repeats suppressed.
    for _ in range(5):
        fake_now[0] += 0.5
        detect._dedup_observe_tracks("cam_01", {5}, fake_now[0], cfg)
        res, _ = detect._dedup_filter(
            "cam_01", "intrusion", {"track_id": 5}, "main", cfg, severity="CRITICAL",
        )
        assert res == "suppressed_dedup_track"

    # Track 5 leaves; 3s of empty frames → idle threshold tripped.
    fake_now[0] += 3.0
    detect._dedup_observe_tracks("cam_01", set(), fake_now[0], cfg)

    # Track 6 (new id, same logical situation) appears and fires fresh
    # despite both windows being well within the legacy 300s/120s defaults.
    fake_now[0] += 0.1
    detect._dedup_observe_tracks("cam_01", {6}, fake_now[0], cfg)
    res, _ = detect._dedup_filter(
        "cam_01", "intrusion", {"track_id": 6}, "main", cfg, severity="CRITICAL",
    )
    assert res is None, "new track should re-fire after track-loss retirement"


def test_no_severity_falls_back_to_legacy_behavior(monkeypatch):
    """Calls without severity arg behave exactly like the pre-Phase-0a filter."""
    detect = _import_detect_minimal()
    cfg = _cfg(track_window_s=300.0, track_window_by_severity_s={"CRITICAL": 30})

    fake_now = [9000.0]
    monkeypatch.setattr(detect.time, "time", lambda: fake_now[0])

    res, _ = detect._dedup_filter(
        "cam_01", "intrusion", {"track_id": 50}, "main", cfg,
    )
    assert res is None

    fake_now[0] += 50  # past CRITICAL's 30s, well within base 300s.
    res, _ = detect._dedup_filter(
        "cam_01", "intrusion", {"track_id": 50}, "main", cfg,
    )
    assert res == "suppressed_dedup_track", (
        "no severity → flat 300s base wins, NOT the CRITICAL override"
    )
