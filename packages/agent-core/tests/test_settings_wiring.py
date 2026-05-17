"""Tests for from_settings() factories — confirm settings actually drive behavior.

These are intentionally narrow: we trust the per-class tests for behavior;
here we just verify that values from ``AgentSettings`` end up on the right
attributes of the constructed objects. If you forget to plumb a new knob
through, the relevant test here will fail.
"""

from __future__ import annotations

import pytest
from agent_core.actions.digest import DailyDigestBuilder
from agent_core.content_creation.calibration import CalibrationManager
from agent_core.content_creation.synthesis import SyntheticBattery
from agent_core.learning.detector import HeuristicDetector
from agent_core.learning.maintenance import MaintenanceScan
from agent_core.quality.sampling import SamplingPolicy
from agent_core.settings import AgentSettings
from agent_core.state import Database


def _db() -> Database:
    db = Database.sqlite_memory()
    db.create_all()
    return db


# ── Detector ───────────────────────────────────────────────────────────────


def test_detector_from_settings_uses_min_confidence() -> None:
    s = AgentSettings(learning={"detector_min_confidence": 0.42})  # type: ignore[arg-type]
    det = HeuristicDetector.from_settings(s)
    assert det.min_confidence == pytest.approx(0.42)


# ── MaintenanceScan ────────────────────────────────────────────────────────


def test_maintenance_from_settings_uses_thresholds() -> None:
    s = AgentSettings(
        learning={  # type: ignore[arg-type]
            "maintenance_duplicate_threshold": 0.55,
            "maintenance_stale_days": 21,
            "maintenance_compactable_min_cluster": 9,
        }
    )
    scan = MaintenanceScan.from_settings(s, _db())
    assert scan.duplicate_threshold == pytest.approx(0.55)
    assert scan.stale_days == 21
    assert scan.compactable_min_cluster == 9


# ── SamplingPolicy ─────────────────────────────────────────────────────────


def test_sampling_from_settings_uses_quality_knobs() -> None:
    s = AgentSettings(
        quality={  # type: ignore[arg-type]
            "audit_sample_rate": 0.25,
            "low_confidence_audit_threshold": 0.66,
        }
    )
    sp = SamplingPolicy.from_settings(s, _db())
    assert sp.base_rate == pytest.approx(0.25)
    assert sp.low_confidence_threshold == pytest.approx(0.66)


# ── CalibrationManager ────────────────────────────────────────────────────


def test_calibration_from_settings_uses_auto_promote_confidence() -> None:
    s = AgentSettings(learning={"auto_promote_confidence": 0.77})  # type: ignore[arg-type]
    mgr = CalibrationManager.from_settings(s, _db())
    assert mgr.default_threshold == pytest.approx(0.77)


# ── SyntheticBattery ──────────────────────────────────────────────────────


def test_synthesis_from_settings_uses_thresholds() -> None:
    s = AgentSettings(
        learning={  # type: ignore[arg-type]
            "synthetic_min_natural_exemplars": 33,
            "synthetic_min_days_of_data": 11,
            "synthetic_min_correction_themes": 4,
        }
    )
    bat = SyntheticBattery.from_settings(s, _db())
    assert bat.min_natural_exemplars == 33
    assert bat.min_days_of_data == 11
    assert bat.min_correction_themes == 4


# ── DigestGenerator ───────────────────────────────────────────────────────


def test_digest_from_settings_uses_period_hours() -> None:
    s = AgentSettings(notifications={"digest_period_hours": 6})  # type: ignore[arg-type]
    dg = DailyDigestBuilder.from_settings(s, _db())
    assert dg.period_hours == 6


# ── PipelineMonitor ───────────────────────────────────────────────────────


def test_pipeline_monitor_from_settings_uses_work_thresholds() -> None:
    from agent_core.work.pipeline_monitor import PipelineMonitor

    s = AgentSettings(
        work={  # type: ignore[arg-type]
            "pipeline_in_progress_threshold_hours": 8,
            "pipeline_waiting_threshold_hours": 96,
        }
    )
    pm = PipelineMonitor.from_settings(s, _db())
    assert pm.in_progress_threshold_hours == 8
    assert pm.waiting_threshold_hours == 96


# ── QualityAuditor (with stub auditor) ────────────────────────────────────


class _StubAuditor:
    """Minimal AuditorModel for the wiring test — never actually invoked."""

    def evaluate(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError


def test_auditor_from_settings_uses_quality_and_autonomy_knobs() -> None:
    from agent_core.quality.auditor import QualityAuditor

    s = AgentSettings(
        quality={"pass_threshold": 0.71, "last_n_window": 7},  # type: ignore[arg-type]
        autonomy={"auto_undelegate_after_n_failures": 4},  # type: ignore[arg-type]
    )
    aud = QualityAuditor.from_settings(s, _db(), _StubAuditor())  # type: ignore[arg-type]
    assert aud.pass_threshold == pytest.approx(0.71)
    assert aud.last_n_window == 7
    assert aud.undelegation_strikes == 4


# ── AgentLoop ─────────────────────────────────────────────────────────────


def test_agent_loop_from_settings_uses_runtime_max_per_tick() -> None:
    from agent_core.agent.loop import AgentLoop

    class _Stub:
        def __getattr__(self, _: str):
            return lambda *a, **k: None

    s = AgentSettings(runtime={"max_obligations_per_tick": 3})  # type: ignore[arg-type]
    loop = AgentLoop.from_settings(s, _db(), _Stub(), _Stub(), _Stub(), _Stub())  # type: ignore[arg-type]
    assert loop.max_obligations_per_tick == 3
