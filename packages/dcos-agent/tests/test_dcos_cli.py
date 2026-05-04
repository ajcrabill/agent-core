"""Smoke tests for dcos-agent CLI + defaults."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from dcos_agent import (
    INSTANCE_NAME,
    config_dir,
    default_db_path,
    default_db_url,
    default_settings_path,
    state_dir,
)
from dcos_agent.cli import cli


# ── Defaults ───────────────────────────────────────────────────────────────


def test_instance_name_is_dcos_agent() -> None:
    assert INSTANCE_NAME == "dcos-agent"


def test_default_paths_under_xdg_dirs(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert state_dir() == tmp_path / "state" / "dcos-agent"
    assert config_dir() == tmp_path / "config" / "dcos-agent"
    assert default_db_path() == tmp_path / "state" / "dcos-agent" / "agent.db"
    assert default_settings_path() == tmp_path / "config" / "dcos-agent" / "agent.yml"


def test_default_db_url_is_sqlite_for_default_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    url = default_db_url()
    assert url.startswith("sqlite:///")
    assert "dcos-agent" in url


# ── CLI surface ────────────────────────────────────────────────────────────


def test_cli_help_includes_dcos_brand() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "dcos-agent" in result.output
    assert "chief of staff" in result.output


def test_cli_lists_expected_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    for cmd in ("settings", "doctor", "backup", "restore", "setup", "info"):
        assert cmd in result.output


def test_cli_version_works() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    # Click version output: "dcos, version X.Y.Z"
    assert "0.0.1" in result.output


def test_info_command(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    runner = CliRunner()
    result = runner.invoke(cli, ["info"])
    assert result.exit_code == 0
    assert "dcos-agent" in result.output
    # Rich may wrap long paths — assert the key labels rather than full paths.
    assert "version" in result.output
    assert "config" in result.output
    assert "db" in result.output


def test_doctor_runs_with_dcos_defaults(monkeypatch, tmp_path: Path) -> None:
    """`dcos doctor` should run cleanly even with no install (everything skipped)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    runner = CliRunner()
    result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == 0  # default install with no db should pass
    assert "settings" in result.output


def test_settings_subcommand_works(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    runner = CliRunner()
    cfg = tmp_path / "agent.yml"
    result = runner.invoke(cli, ["settings", "--config", str(cfg), "show", "autonomy"])
    assert result.exit_code == 0
    assert "autonomy.default_policy" in result.output


def test_setup_creates_config_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    runner = CliRunner()
    # Tier 1: 3 returns (preset default, empty name, sqlite default).
    # --no-init avoids touching the real keychain in CI.
    result = runner.invoke(
        cli,
        ["setup", "--tier", "1", "--no-init"],
        input="\n\n\n",
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "config" / "dcos-agent").exists()


def test_setup_runs_init_and_doctor_by_default(monkeypatch, tmp_path: Path) -> None:
    """Verify the new chained behavior: setup → init → doctor when no flags."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    # Patch the secret store so we don't touch the real keychain in CI.
    from agent_core.secrets import MemorySecretStore

    monkeypatch.setattr("agent_core.secrets.default_store", lambda: MemorySecretStore())

    runner = CliRunner()
    result = runner.invoke(cli, ["setup", "--tier", "1"], input="\n\n\n")
    assert result.exit_code == 0, result.output
    # Init ran: schema-at-head message + token printed
    assert "schema at head" in result.output
    assert "API token" in result.output
    # Doctor ran: at least one of its checks shows up
    assert "agent doctor" in result.output


def test_setup_no_init_skips_chained_steps(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    runner = CliRunner()
    result = runner.invoke(
        cli, ["setup", "--tier", "1", "--no-init"], input="\n\n\n"
    )
    assert result.exit_code == 0, result.output
    assert "schema at head" not in result.output
    assert "agent doctor" not in result.output


# ── Pathing quirk: `dcos settings set` resolves to dcos config dir ─────────


def test_dcos_main_sets_agent_data_dir_to_dcos_config_dir(monkeypatch, tmp_path):
    """Regression: previously `dcos settings set foo=bar` (no --config)
    wrote to ``cwd/agent.yml`` because agent-core's _default_path() falls
    back to cwd. dcos main() now sets AGENT_DATA_DIR so the borrowed
    settings group resolves to ``~/.config/dcos-agent/agent.yml``."""
    import os

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    # Make sure we don't inherit a previous AGENT_DATA_DIR
    monkeypatch.delenv("AGENT_DATA_DIR", raising=False)

    from dcos_agent.cli import main

    # Patch cli() so we don't actually invoke a Click command — we just
    # want to confirm AGENT_DATA_DIR ends up pointing at dcos's config dir.
    called: dict = {}

    def fake_cli():
        called["AGENT_DATA_DIR"] = os.environ.get("AGENT_DATA_DIR")

    monkeypatch.setattr("dcos_agent.cli.cli", fake_cli)
    main()

    expected = tmp_path / "config" / "dcos-agent"
    assert called["AGENT_DATA_DIR"] == str(expected)


def test_dcos_main_respects_pre_existing_agent_data_dir(monkeypatch, tmp_path):
    """Power users who set AGENT_DATA_DIR explicitly should keep their override."""
    import os

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path / "custom-dir"))

    from dcos_agent.cli import main

    called: dict = {}

    def fake_cli():
        called["AGENT_DATA_DIR"] = os.environ.get("AGENT_DATA_DIR")

    monkeypatch.setattr("dcos_agent.cli.cli", fake_cli)
    main()

    # setdefault() should NOT clobber the pre-existing value
    assert called["AGENT_DATA_DIR"] == str(tmp_path / "custom-dir")


def test_dcos_settings_set_writes_to_dcos_config_dir(monkeypatch, tmp_path):
    """End-to-end: ``dcos settings set foo=bar`` lands in the dcos config
    file, not cwd/agent.yml. This is the user-visible repro of the original
    quirk: the user runs `dcos settings set ...` from anywhere on disk and
    expects it to show up in `dcos info`'s settings file."""
    import os

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("AGENT_DATA_DIR", raising=False)
    monkeypatch.chdir(tmp_path / "elsewhere" if False else tmp_path)

    # Mirror what dcos's main() does — set AGENT_DATA_DIR then dispatch.
    # We invoke the cli subcommand directly via CliRunner; main() itself
    # is tested above.
    os.environ.setdefault(
        "AGENT_DATA_DIR", str(tmp_path / "config" / "dcos-agent")
    )

    runner = CliRunner()
    result = runner.invoke(
        cli, ["settings", "set", "autonomy.default_policy=cautious"]
    )
    assert result.exit_code == 0, result.output

    settings_file = tmp_path / "config" / "dcos-agent" / "agent.yml"
    cwd_file = tmp_path / "agent.yml"
    assert settings_file.exists(), f"expected dcos config to be written; got: {result.output}"
    assert not cwd_file.exists(), "should NOT write to cwd"

    body = settings_file.read_text()
    assert "default_policy" in body
    assert "cautious" in body


# ── dcos secrets group ─────────────────────────────────────────────────────


class _FakeStore:
    """In-memory secret store for testing the CLI without touching keychain."""

    def __init__(self):
        self._data: dict[tuple[str, str], str] = {}

    def get(self, ns, key):
        return self._data.get((ns, key))

    def set(self, ns, key, value):
        self._data[(ns, key)] = value

    def delete(self, ns, key):
        self._data.pop((ns, key), None)

    def list(self, ns):
        return [k for (n, k) in self._data if n == ns]


def test_secrets_set_one_shot_assignment(monkeypatch):
    fake = _FakeStore()
    monkeypatch.setattr("agent_core.secrets.default_store", lambda: fake)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["secrets", "set", "llm.openai_api_key=sk-abc123"]
    )
    assert result.exit_code == 0, result.output
    assert fake.get("llm", "openai_api_key") == "sk-abc123"


def test_secrets_set_rejects_missing_dot():
    runner = CliRunner()
    result = runner.invoke(cli, ["secrets", "set", "no_dot=value"])
    assert result.exit_code != 0
    assert "namespace" in result.output.lower()


def test_secrets_set_from_stdin(monkeypatch):
    fake = _FakeStore()
    monkeypatch.setattr("agent_core.secrets.default_store", lambda: fake)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["secrets", "set", "--from-stdin", "email.imap_password"],
        input="my-app-pw\n",
    )
    assert result.exit_code == 0, result.output
    assert fake.get("email", "imap_password") == "my-app-pw"


def test_secrets_get_redacts_by_default(monkeypatch):
    fake = _FakeStore()
    fake.set("llm", "openai_api_key", "sk-secret-value")
    monkeypatch.setattr("agent_core.secrets.default_store", lambda: fake)
    runner = CliRunner()
    result = runner.invoke(cli, ["secrets", "get", "llm.openai_api_key"])
    assert result.exit_code == 0
    assert "sk-secret-value" not in result.output
    assert "REDACTED" in result.output


def test_secrets_get_show_reveals_value(monkeypatch):
    fake = _FakeStore()
    fake.set("llm", "openai_api_key", "sk-real-value")
    monkeypatch.setattr("agent_core.secrets.default_store", lambda: fake)
    runner = CliRunner()
    result = runner.invoke(cli, ["secrets", "get", "llm.openai_api_key", "--show"])
    assert result.exit_code == 0
    assert "sk-real-value" in result.output


def test_secrets_get_unset_returns_nonzero(monkeypatch):
    fake = _FakeStore()
    monkeypatch.setattr("agent_core.secrets.default_store", lambda: fake)
    runner = CliRunner()
    result = runner.invoke(cli, ["secrets", "get", "llm.openai_api_key"])
    assert result.exit_code == 2
    assert "not set" in result.output.lower()


def test_secrets_delete_removes_value(monkeypatch):
    fake = _FakeStore()
    fake.set("email", "imap_password", "hunter2")
    monkeypatch.setattr("agent_core.secrets.default_store", lambda: fake)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["secrets", "delete", "email.imap_password", "--yes"]
    )
    assert result.exit_code == 0
    assert fake.get("email", "imap_password") is None


def test_secrets_list_namespace(monkeypatch):
    fake = _FakeStore()
    fake.set("llm", "openai_api_key", "x")
    fake.set("llm", "deepseek_api_key", "y")
    fake.set("email", "imap_password", "z")
    monkeypatch.setattr("agent_core.secrets.default_store", lambda: fake)
    runner = CliRunner()
    result = runner.invoke(cli, ["secrets", "list", "llm"])
    assert result.exit_code == 0
    assert "openai_api_key" in result.output
    assert "deepseek_api_key" in result.output
    assert "imap_password" not in result.output


# ── Sprint 19: chat slash-command helpers ──────────────────────────────────


def test_capture_inline_email_form_creates_inbound_email_obligation():
    from agent_core.state.db import Database
    from agent_core.state.models import (
        Obligation,
        ObligationSource,
        ObligationStatus,
    )
    from sqlmodel import select

    from dcos_agent.cli import _capture_inline

    db = Database.sqlite_memory()
    db.create_all()
    raw = "Email from boss@example.com: Q2 sign-off\nBody line 1\nBody line 2"
    ob_id = _capture_inline(db=db, raw=raw)

    with db.session() as s:
        ob = s.exec(select(Obligation).where(Obligation.id == ob_id)).one()
        assert ob.source == ObligationSource.inbound_email
        assert ob.status == ObligationStatus.inbox
        assert ob.title == "Email from boss@example.com: Q2 sign-off"
        assert "Body line 1" in (ob.body or "")


def test_capture_inline_plain_text_creates_manual_obligation():
    from agent_core.state.db import Database
    from agent_core.state.models import (
        Obligation,
        ObligationSource,
        ObligationStatus,
    )
    from sqlmodel import select

    from dcos_agent.cli import _capture_inline

    db = Database.sqlite_memory()
    db.create_all()
    ob_id = _capture_inline(db=db, raw="follow up with charlotte tomorrow")

    with db.session() as s:
        ob = s.exec(select(Obligation).where(Obligation.id == ob_id)).one()
        assert ob.source == ObligationSource.manual
        assert ob.status == ObligationStatus.inbox
        assert ob.title == "follow up with charlotte tomorrow"


def test_run_triage_inline_classifies_inbox_email_obligation(capsys):
    """End-to-end: /capture + /triage in one shot. The captured email
    obligation should get auto-triaged via the inline helper."""
    import json

    from agent_core.settings import SettingsManager
    from agent_core.skills import StubLanguageModel
    from agent_core.state.db import Database
    from agent_core.state.models import Obligation, ObligationStatus
    from sqlmodel import select

    from dcos_agent.cli import _capture_inline, _run_triage_inline

    db = Database.sqlite_memory()
    db.create_all()
    settings = SettingsManager()
    lm = StubLanguageModel(default=json.dumps({
        "action": "draft",
        "score": 0.95,
        "reasoning": "looks important",
    }))

    _capture_inline(db=db, raw="Email from x@y.com: hello\nplease respond")
    _run_triage_inline(db=db, settings=settings, language_model=lm)

    with db.session() as s:
        ob = s.exec(select(Obligation)).one()
        # 'draft' action moves status to in_progress
        assert ob.status == ObligationStatus.in_progress

    out = capsys.readouterr().out
    assert "1 candidates" in out
    assert "1 classified" in out


def test_show_digest_inline_renders_markdown(capsys):
    from agent_core.state.db import Database
    from agent_core.state.models import Obligation, ObligationStatus, utcnow
    from dcos_agent.cli import _show_digest_inline

    db = Database.sqlite_memory()
    db.create_all()
    with db.session() as s:
        s.add(
            Obligation(
                title="closed thing",
                status=ObligationStatus.done,
                completed_at=utcnow(),
            )
        )
        s.commit()

    _show_digest_inline(db=db, hours=24)
    out = capsys.readouterr().out
    assert "Daily digest" in out
    assert "closed thing" in out


# ── Re-exports from agent-core ─────────────────────────────────────────────


def test_top_level_imports_work() -> None:
    """The dcos_agent namespace re-exports the most common agent-core types."""
    from dcos_agent import (
        AgentSettings,
        Database,
        Notification,
        NotificationDispatcher,
        OpenBrainStore,
        SettingsManager,
        Urgency,
    )

    s = AgentSettings()
    assert s.autonomy.default_policy == "balanced"
    assert Notification(title="x", body="y", urgency=Urgency.warn).title == "x"
