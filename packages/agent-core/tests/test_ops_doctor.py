"""Tests for agent_core.ops.doctor — health check battery + report aggregation."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.ops import CheckResult, CheckStatus, Doctor, DoctorReport, HealthCheck
from agent_core.ops.doctor import (
    DoctorContext,
    IdentityCheck,
    NotificationsConfiguredCheck,
    OllamaReachableCheck,
    SettingsValidCheck,
    StorageReachableCheck,
    VaultPathCheck,
)
from agent_core.settings import AgentSettings, SettingsManager
from agent_core.state import Database


# ── Fixtures ────────────────────────────────────────────────────────────────


def _db() -> Database:
    db = Database.sqlite_memory()
    db.create_all()
    return db


def _ctx(settings: object | None = None, db=None) -> DoctorContext:
    return DoctorContext(settings=settings or AgentSettings(), db=db)


# ── Result types ────────────────────────────────────────────────────────────


def test_check_status_values() -> None:
    assert CheckStatus.ok != CheckStatus.fail
    assert CheckStatus.skipped != CheckStatus.warn


def test_doctor_report_ok_when_only_passes() -> None:
    r = DoctorReport(
        results=[
            CheckResult(name="a", status=CheckStatus.ok, message=""),
            CheckResult(name="b", status=CheckStatus.skipped, message=""),
        ]
    )
    assert r.ok is True
    assert r.has_warnings is False


def test_doctor_report_ok_when_warns_present() -> None:
    r = DoctorReport(
        results=[
            CheckResult(name="a", status=CheckStatus.ok, message=""),
            CheckResult(name="b", status=CheckStatus.warn, message=""),
        ]
    )
    assert r.ok is True  # warns don't break ok
    assert r.has_warnings is True


def test_doctor_report_not_ok_with_any_fail() -> None:
    r = DoctorReport(
        results=[
            CheckResult(name="a", status=CheckStatus.ok, message=""),
            CheckResult(name="b", status=CheckStatus.fail, message=""),
        ]
    )
    assert r.ok is False


def test_doctor_report_by_status_counts() -> None:
    r = DoctorReport(
        results=[
            CheckResult(name="a", status=CheckStatus.ok, message=""),
            CheckResult(name="b", status=CheckStatus.ok, message=""),
            CheckResult(name="c", status=CheckStatus.skipped, message=""),
        ]
    )
    counts = r.by_status()
    assert counts[CheckStatus.ok] == 2
    assert counts[CheckStatus.skipped] == 1
    assert counts[CheckStatus.fail] == 0


# ── Settings check ──────────────────────────────────────────────────────────


def test_settings_check_passes_with_valid_manager(tmp_path: Path) -> None:
    mgr = SettingsManager(path=tmp_path / "agent.yml", env={})
    result = SettingsValidCheck().run(_ctx(settings=mgr))
    assert result.status == CheckStatus.ok


def test_settings_check_passes_with_bare_settings() -> None:
    """A bare AgentSettings (no manager) trivially passes — its existence
    means it already validated."""
    result = SettingsValidCheck().run(_ctx())
    assert result.status == CheckStatus.ok


# ── Storage check ──────────────────────────────────────────────────────────


def test_storage_check_skipped_when_no_db() -> None:
    result = StorageReachableCheck().run(_ctx())
    assert result.status == CheckStatus.skipped


def test_storage_check_passes_with_real_db() -> None:
    result = StorageReachableCheck().run(_ctx(db=_db()))
    assert result.status == CheckStatus.ok
    assert "reachable" in result.message


def test_storage_check_fails_on_broken_db() -> None:
    db = Database.sqlite_memory()
    # Don't call create_all — querying Obligation will fail.
    result = StorageReachableCheck().run(_ctx(db=db))
    assert result.status == CheckStatus.fail


# ── Vault check ────────────────────────────────────────────────────────────


def test_vault_check_skipped_when_no_vault_configured() -> None:
    result = VaultPathCheck().run(_ctx(settings=AgentSettings()))
    assert result.status == CheckStatus.skipped


def test_vault_check_passes_when_path_exists(tmp_path: Path) -> None:
    s = AgentSettings(storage={"vault_path": str(tmp_path)})  # type: ignore[arg-type]
    result = VaultPathCheck().run(_ctx(settings=s))
    assert result.status == CheckStatus.ok


def test_vault_check_fails_when_path_missing() -> None:
    s = AgentSettings(storage={"vault_path": "/nonexistent/path/here"})  # type: ignore[arg-type]
    result = VaultPathCheck().run(_ctx(settings=s))
    assert result.status == CheckStatus.fail


def test_vault_check_fails_when_path_is_file(tmp_path: Path) -> None:
    f = tmp_path / "not-a-dir.txt"
    f.write_text("hi")
    s = AgentSettings(storage={"vault_path": str(f)})  # type: ignore[arg-type]
    result = VaultPathCheck().run(_ctx(settings=s))
    assert result.status == CheckStatus.fail


# ── Ollama check ───────────────────────────────────────────────────────────


def test_ollama_check_skipped_when_provider_is_stub() -> None:
    s = AgentSettings(openbrain={"embedding_provider": "stub"})  # type: ignore[arg-type]
    result = OllamaReachableCheck().run(_ctx(settings=s))
    assert result.status == CheckStatus.skipped


def test_ollama_check_fails_on_unreachable_url(monkeypatch) -> None:
    import urllib.error

    def boom(*a, **k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    s = AgentSettings(
        openbrain={  # type: ignore[arg-type]
            "embedding_provider": "ollama",
            "ollama_base_url": "http://localhost:9999",
        }
    )
    result = OllamaReachableCheck().run(_ctx(settings=s))
    assert result.status == CheckStatus.fail
    assert "unreachable" in result.message


def test_ollama_check_passes_when_endpoint_responds(monkeypatch) -> None:
    class _FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _FakeResp())
    s = AgentSettings(
        openbrain={  # type: ignore[arg-type]
            "embedding_provider": "ollama",
            "ollama_base_url": "http://localhost:11434",
        }
    )
    result = OllamaReachableCheck().run(_ctx(settings=s))
    assert result.status == CheckStatus.ok


# ── Notifications check ────────────────────────────────────────────────────


def test_notifications_check_skipped_when_disabled() -> None:
    result = NotificationsConfiguredCheck().run(_ctx())
    assert result.status == CheckStatus.skipped


def test_notifications_check_fails_when_ntfy_topic_missing() -> None:
    s = AgentSettings(
        notifications={  # type: ignore[arg-type]
            "enabled": True,
            "transport": "ntfy",
            "ntfy_topic": None,
        }
    )
    result = NotificationsConfiguredCheck().run(_ctx(settings=s))
    assert result.status == CheckStatus.fail


def test_notifications_check_passes_when_configured() -> None:
    s = AgentSettings(
        notifications={  # type: ignore[arg-type]
            "enabled": True,
            "transport": "ntfy",
            "ntfy_topic": "private-7x9k",
        }
    )
    result = NotificationsConfiguredCheck().run(_ctx(settings=s))
    assert result.status == CheckStatus.ok


# ── Identity check ─────────────────────────────────────────────────────────


def test_identity_check_passes() -> None:
    result = IdentityCheck().run(_ctx())
    assert result.status == CheckStatus.ok


# ── Doctor aggregation ────────────────────────────────────────────────────


def test_doctor_runs_all_default_checks() -> None:
    d = Doctor()
    report = d.run(_ctx())
    names = {r.name for r in report.results}
    assert names == {
        "settings",
        "storage",
        "migrations",
        "vault",
        "ollama",
        "notifications",
        "identity",
    }


def test_doctor_buggy_check_doesnt_crash_others() -> None:
    class _Boom:
        name = "boom"

        def run(self, ctx):
            raise RuntimeError("intentional")

    d = Doctor(checks=[_Boom(), SettingsValidCheck()])
    report = d.run(_ctx())
    by_name = {r.name: r for r in report.results}
    assert by_name["boom"].status == CheckStatus.fail
    assert "intentional" in by_name["boom"].message
    assert by_name["settings"].status == CheckStatus.ok


def test_doctor_default_install_is_ok_or_warns_not_fails() -> None:
    """Regression: a freshly-defaulted install (no db, no vault, no ollama,
    notifications off) should report no failures — only oks and skipped/warns.

    This is the "I just installed and ran doctor" experience."""
    report = Doctor().run(_ctx())
    failures = [r for r in report.results if r.status == CheckStatus.fail]
    assert failures == [], failures


def test_add_check_extension_point() -> None:
    class _Custom:
        name = "custom"

        def run(self, ctx):
            return CheckResult(name=self.name, status=CheckStatus.ok, message="hi")

    d = Doctor()
    d.add_check(_Custom())
    report = d.run(_ctx())
    assert any(r.name == "custom" and r.status == CheckStatus.ok for r in report.results)


def test_health_check_protocol_satisfaction() -> None:
    assert isinstance(SettingsValidCheck(), HealthCheck)
    assert isinstance(VaultPathCheck(), HealthCheck)
    assert isinstance(OllamaReachableCheck(), HealthCheck)
