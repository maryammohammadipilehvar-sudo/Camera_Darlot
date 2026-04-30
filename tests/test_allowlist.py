"""Unit tests for the per-kind alert allowlist (Phase 0b).

Covers _resolve_alert_kind_allowlist (legacy translation + conflict
detection) and the emit_alert gate behaviour. The emit_alert tests
stub the audit writer + notify dispatch so no DB or Telegram is hit.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _import_detect():
    import detect  # noqa: F401
    return detect


# ──────────────────── _resolve_alert_kind_allowlist ───────────────────────────


def test_resolve_neither_key_returns_none():
    detect = _import_detect()
    assert detect._resolve_alert_kind_allowlist({}) is None


def test_resolve_new_key_only():
    detect = _import_detect()
    cfg = {"alert_kind_allowlist": ["forbidden_zone", "audio"]}
    assert detect._resolve_alert_kind_allowlist(cfg) == ["forbidden_zone", "audio"]


def test_resolve_new_key_explicit_none_disables_gate():
    detect = _import_detect()
    cfg = {"alert_kind_allowlist": None}
    assert detect._resolve_alert_kind_allowlist(cfg) is None


def test_resolve_empty_list_silences_everything():
    detect = _import_detect()
    cfg = {"alert_kind_allowlist": []}
    assert detect._resolve_alert_kind_allowlist(cfg) == []


def test_resolve_legacy_true_translates_and_warns(caplog):
    detect = _import_detect()
    cfg = {"alerts_only_forbidden_zone": True}
    with caplog.at_level("WARNING", logger="pipeline"):
        out = detect._resolve_alert_kind_allowlist(cfg)
    assert out == ["forbidden_zone"]
    assert any("DEPRECATED" in r.message for r in caplog.records)


def test_resolve_legacy_false_disables_gate():
    detect = _import_detect()
    cfg = {"alerts_only_forbidden_zone": False}
    assert detect._resolve_alert_kind_allowlist(cfg) is None


def test_resolve_legacy_and_matching_new_key_warns_redundant(caplog):
    detect = _import_detect()
    cfg = {
        "alerts_only_forbidden_zone": True,
        "alert_kind_allowlist": ["forbidden_zone"],
    }
    with caplog.at_level("WARNING", logger="pipeline"):
        out = detect._resolve_alert_kind_allowlist(cfg)
    assert out == ["forbidden_zone"]
    assert any("redundantly duplicates" in r.message for r in caplog.records)


def test_resolve_legacy_and_conflicting_new_key_raises():
    detect = _import_detect()
    cfg = {
        "alerts_only_forbidden_zone": True,
        "alert_kind_allowlist": ["audio"],
    }
    with pytest.raises(ValueError, match="disagree"):
        detect._resolve_alert_kind_allowlist(cfg)


# ──────────────────── emit_alert gate behaviour ───────────────────────────────


@pytest.fixture
def emit_alert_recorder(monkeypatch):
    """Stub _audit_write + dispatch so emit_alert is observable in-process.

    Yields a list that captures every audit write the call would have
    performed, ordered by call. Each entry is a kwargs dict.
    """
    detect = _import_detect()
    captured: list = []

    def fake_audit(*args, **kwargs):
        # Normalise positional → kwargs for assertion convenience.
        sig = (
            "event_id", "camera_id", "kind", "computed_severity",
            "mode", "decision", "reason_detail", "zone",
        )
        merged = dict(zip(sig, args))
        merged.update(kwargs)
        captured.append(merged)

    def noop(*a, **kw):
        return None

    monkeypatch.setattr(detect, "_audit_write", fake_audit)
    monkeypatch.setattr(detect, "_dispatch_notify", noop)
    monkeypatch.setattr(detect, "_snapshot_enqueue", lambda eid: None)
    # Resolve_mode hits a real datetime path; force OCCUPIED for predictability.
    monkeypatch.setattr(detect, "resolve_mode", lambda cfg, now=None: "OCCUPIED")
    # Drain alerts to nowhere.
    import queue
    monkeypatch.setattr(detect, "_alert_q", queue.Queue(maxsize=1))
    yield captured


def test_emit_alert_blocks_kind_outside_allowlist(monkeypatch, emit_alert_recorder):
    detect = _import_detect()
    monkeypatch.setattr(detect, "_alert_kind_allowlist", ["forbidden_zone"])

    detect.emit_alert("cam_01", "detection", {"label": "person"})

    assert len(emit_alert_recorder) == 1
    row = emit_alert_recorder[0]
    assert row["decision"] == "suppressed_threshold"
    assert row["reason_detail"] == "not_in_allowlist"
    assert row["kind"] == "detection"
    assert row["event_id"] is None


def test_emit_alert_passes_kind_inside_allowlist(monkeypatch, emit_alert_recorder):
    detect = _import_detect()
    monkeypatch.setattr(detect, "_alert_kind_allowlist", ["forbidden_zone"])

    detect.emit_alert("cam_01", "forbidden_zone", {
        "track_id": 1, "zone": "test", "label": "Person in forbidden zone",
    })

    # forbidden_zone is CRITICAL × OCCUPIED → telegram route → 1 audit row
    # (fired_telegram). The gate did NOT short-circuit, so reason_detail
    # should never be 'not_in_allowlist'.
    assert all(
        r.get("reason_detail") != "not_in_allowlist"
        for r in emit_alert_recorder
    ), emit_alert_recorder


def test_emit_alert_no_gate_when_allowlist_none(monkeypatch, emit_alert_recorder):
    detect = _import_detect()
    monkeypatch.setattr(detect, "_alert_kind_allowlist", None)

    detect.emit_alert("cam_01", "detection", {"label": "person"})

    # With no gate, the severity table decides. detection × OCCUPIED is
    # MEDIUM × telegram per the default CFG. The dedup filter may suppress
    # subsequent calls, but on a fresh module state the FIRST call fires.
    # Either way, we must not see the 'not_in_allowlist' reason.
    assert all(
        r.get("reason_detail") != "not_in_allowlist"
        for r in emit_alert_recorder
    ), emit_alert_recorder


def test_emit_alert_empty_allowlist_silences_everything(
    monkeypatch, emit_alert_recorder,
):
    detect = _import_detect()
    monkeypatch.setattr(detect, "_alert_kind_allowlist", [])

    detect.emit_alert("cam_01", "forbidden_zone", {
        "track_id": 1, "zone": "test", "label": "blocked",
    })

    assert len(emit_alert_recorder) == 1
    assert emit_alert_recorder[0]["reason_detail"] == "not_in_allowlist"
