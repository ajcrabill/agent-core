"""Tests for agent_core.web — auth + four routers + app factory.

Uses FastAPI's TestClient for synchronous in-process testing — no port to
manage, no async fixtures needed.

File-backed sqlite throughout: FastAPI's per-request connections each see
a fresh sqlite ``:memory:`` (it's per-connection), so tests that hit the
HTTP layer must use a real file. Same trap caught us in mesh-http and the
state watcher previously.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_core.openbrain import OpenBrainStore, StubEmbeddingProvider
from agent_core.settings import AgentSettings, SettingsManager
from agent_core.state import Database
from agent_core.state.models import (
    Obligation,
    ObligationOwner,
    ObligationSource,
    ObligationStatus,
)
from agent_core.web import create_app
from agent_core.web.auth import TokenStore
from fastapi.testclient import TestClient

TOKEN = "test-token-7x9k"


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database.sqlite(tmp_path / "test.db")
    d.create_all()
    return d


@pytest.fixture
def app_factory(db: Database):
    """Returns a function that builds a TestClient with the right db.

    Tests that need to inject extra obligations or custom settings call
    this with kwargs; tests that don't take ``client`` directly."""

    def _build(
        *,
        settings: AgentSettings | None = None,
        extra_obligations: int = 0,
        target_db: Database | None = None,
    ) -> TestClient:
        d = target_db or db
        s = settings or AgentSettings()
        ob_store = OpenBrainStore(d, StubEmbeddingProvider())
        if extra_obligations:
            with d.session() as ses:
                for i in range(extra_obligations):
                    ses.add(
                        Obligation(
                            title=f"Test #{i}",
                            source=ObligationSource.manual,
                            priority=i,
                        )
                    )
                ses.commit()
        app = create_app(d, s, api_tokens={TOKEN}, openbrain=ob_store)
        return TestClient(app)

    return _build


@pytest.fixture
def client(app_factory) -> TestClient:
    return app_factory()


def _hdr() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


# ── App factory ────────────────────────────────────────────────────────────


def test_create_app_requires_at_least_one_token(db: Database) -> None:
    with pytest.raises(ValueError, match="api_tokens"):
        create_app(db, AgentSettings(), api_tokens=set())


def test_create_app_builds_openbrain_when_not_provided(db: Database) -> None:
    s = AgentSettings(openbrain={"embedding_provider": "stub"})  # type: ignore[arg-type]
    app = create_app(db, s, api_tokens={"x"})
    assert isinstance(app.state.openbrain, OpenBrainStore)


# ── Auth ───────────────────────────────────────────────────────────────────


def test_unauthenticated_request_rejected(client: TestClient) -> None:
    r = client.get("/obligations")
    assert r.status_code == 401
    assert "Authorization" in r.json().get("detail", "")


def test_invalid_token_rejected(client: TestClient) -> None:
    r = client.get("/obligations", headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 401
    assert r.json().get("detail") == "invalid token"


def test_valid_token_accepted(client: TestClient) -> None:
    r = client.get("/obligations", headers=_hdr())
    assert r.status_code == 200


def test_wrong_scheme_rejected(client: TestClient) -> None:
    r = client.get("/obligations", headers={"Authorization": f"Basic {TOKEN}"})
    assert r.status_code == 401


def test_token_store_constant_time_comparison() -> None:
    store = TokenStore({"abcdef"})
    assert store.is_valid("abcdef") is True
    assert store.is_valid("abcdez") is False
    assert store.is_valid("") is False


def test_token_store_revoke_invalidates() -> None:
    store = TokenStore({"a", "b"})
    assert store.is_valid("a")
    store.revoke("a")
    assert not store.is_valid("a")
    assert store.is_valid("b")


def test_app_with_zero_tokens_post_install_rejects_all(db: Database) -> None:
    """Defense in depth: if someone manually empties the TokenStore at
    runtime, every request must 401 (not silently succeed)."""
    app = create_app(db, AgentSettings(), api_tokens={"will-be-revoked"})
    app.state.token_store.revoke("will-be-revoked")
    c = TestClient(app)
    r = c.get("/obligations", headers={"Authorization": "Bearer anything"})
    assert r.status_code == 401


# ── Health ─────────────────────────────────────────────────────────────────


def test_health_no_auth_required(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readiness_reports_db_and_settings(client: TestClient) -> None:
    r = client.get("/health/ready")
    assert r.status_code == 200
    payload = r.json()
    assert payload["status"] == "ok"
    assert payload["db"] is True
    assert payload["settings"] is True


# ── Obligations: list ─────────────────────────────────────────────────────


def test_list_obligations_empty(client: TestClient) -> None:
    r = client.get("/obligations", headers=_hdr())
    assert r.status_code == 200
    assert r.json() == []


def test_list_obligations_returns_rows(app_factory) -> None:
    client = app_factory(extra_obligations=3)
    r = client.get("/obligations", headers=_hdr())
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 3
    assert all("title" in row for row in rows)


def test_list_obligations_filters_by_status(db: Database, app_factory) -> None:
    with db.session() as s:
        s.add(
            Obligation(
                title="inbox-one", source=ObligationSource.manual, status=ObligationStatus.inbox
            )
        )
        s.add(
            Obligation(
                title="done-one", source=ObligationSource.manual, status=ObligationStatus.done
            )
        )
        s.commit()
    client = app_factory()
    r = client.get("/obligations?status=done", headers=_hdr())
    assert r.status_code == 200
    titles = [row["title"] for row in r.json()]
    assert titles == ["done-one"]


def test_list_obligations_filters_by_owner(db: Database, app_factory) -> None:
    with db.session() as s:
        s.add(
            Obligation(
                title="agent-owned", source=ObligationSource.manual, owner=ObligationOwner.agent
            )
        )
        s.add(
            Obligation(
                title="principal-owned",
                source=ObligationSource.manual,
                owner=ObligationOwner.principal,
            )
        )
        s.commit()
    client = app_factory()
    r = client.get("/obligations?owner=principal", headers=_hdr())
    assert {row["title"] for row in r.json()} == {"principal-owned"}


def test_list_obligations_respects_limit(app_factory) -> None:
    client = app_factory(extra_obligations=10)
    r = client.get("/obligations?limit=3", headers=_hdr())
    assert len(r.json()) == 3


def test_list_obligations_rejects_bad_limit(client: TestClient) -> None:
    r = client.get("/obligations?limit=0", headers=_hdr())
    assert r.status_code == 422


# ── Obligations: detail / create / patch / delete ─────────────────────────


def test_get_obligation_404_for_missing(client: TestClient) -> None:
    r = client.get("/obligations/does-not-exist", headers=_hdr())
    assert r.status_code == 404


def test_create_obligation(client: TestClient) -> None:
    r = client.post(
        "/obligations",
        headers=_hdr(),
        json={"title": "Manual task", "body": "details", "priority": 5},
    )
    assert r.status_code == 201, r.json()
    payload = r.json()
    assert payload["title"] == "Manual task"
    assert payload["priority"] == 5
    assert payload["source"] == "manual"
    assert payload["owner"] == "principal"
    assert payload["status"] == "inbox"
    assert payload["completion_criteria"] == []


def test_create_obligation_requires_title(client: TestClient) -> None:
    r = client.post("/obligations", headers=_hdr(), json={"title": ""})
    assert r.status_code == 422


def test_patch_obligation_status(db: Database, app_factory) -> None:
    with db.session() as s:
        ob = Obligation(title="will-progress", source=ObligationSource.manual)
        s.add(ob)
        s.commit()
        s.refresh(ob)
        ob_id = ob.id
    client = app_factory()
    r = client.patch(
        f"/obligations/{ob_id}",
        headers=_hdr(),
        json={"status": "in-progress"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "in-progress"
    assert r.json()["started_at"] is not None  # side-effect on the transition


def test_patch_obligation_to_done_stamps_completed_at(db: Database, app_factory) -> None:
    with db.session() as s:
        ob = Obligation(title="finish", source=ObligationSource.manual)
        s.add(ob)
        s.commit()
        s.refresh(ob)
        ob_id = ob.id
    client = app_factory()
    r = client.patch(f"/obligations/{ob_id}", headers=_hdr(), json={"status": "done"})
    assert r.status_code == 200
    assert r.json()["completed_at"] is not None


def test_patch_obligation_404_for_missing(client: TestClient) -> None:
    r = client.patch("/obligations/nope", headers=_hdr(), json={"priority": 9})
    assert r.status_code == 404


def test_delete_soft_archives_by_default(db: Database, app_factory) -> None:
    with db.session() as s:
        ob = Obligation(title="will-archive", source=ObligationSource.manual)
        s.add(ob)
        s.commit()
        s.refresh(ob)
        ob_id = ob.id
    client = app_factory()
    r = client.delete(f"/obligations/{ob_id}", headers=_hdr())
    assert r.status_code == 204
    # Soft archive: row still exists, status=done
    r2 = client.get(f"/obligations/{ob_id}", headers=_hdr())
    assert r2.status_code == 200
    assert r2.json()["status"] == "done"


def test_delete_hard_refused_when_archive_only(db: Database, app_factory) -> None:
    """Default settings (archive_instead_of_delete=True) → hard delete refused."""
    with db.session() as s:
        ob = Obligation(title="protect-me", source=ObligationSource.manual)
        s.add(ob)
        s.commit()
        s.refresh(ob)
        ob_id = ob.id
    client = app_factory()
    r = client.delete(f"/obligations/{ob_id}?hard=true", headers=_hdr())
    assert r.status_code == 409
    assert "archive_instead_of_delete" in r.json()["detail"]


def test_delete_hard_succeeds_when_settings_allow(db: Database, app_factory) -> None:
    with db.session() as s:
        ob = Obligation(title="zap-me", source=ObligationSource.manual)
        s.add(ob)
        s.commit()
        s.refresh(ob)
        ob_id = ob.id
    s_aggressive = AgentSettings(autonomy={"archive_instead_of_delete": False})  # type: ignore[arg-type]
    client = app_factory(settings=s_aggressive)
    r = client.delete(f"/obligations/{ob_id}?hard=true", headers=_hdr())
    assert r.status_code == 204
    # Hard delete: row really gone
    r2 = client.get(f"/obligations/{ob_id}", headers=_hdr())
    assert r2.status_code == 404


# ── OpenBrain router ──────────────────────────────────────────────────────


def test_openbrain_capture_then_search(client: TestClient) -> None:
    r1 = client.post(
        "/openbrain/capture",
        headers=_hdr(),
        json={"content": "the quick brown fox jumps over", "source_kind": "vault"},
    )
    assert r1.status_code == 200, r1.json()
    assert r1.json()["was_existing"] is False

    r2 = client.post(
        "/openbrain/search",
        headers=_hdr(),
        json={"query": "the quick brown fox jumps over", "limit": 1, "threshold": 0.0},
    )
    assert r2.status_code == 200
    hits = r2.json()
    assert len(hits) == 1
    assert "fox" in hits[0]["content"]
    assert hits[0]["sources"][0]["source_kind"] == "vault"


def test_openbrain_capture_dedup_marks_existing(client: TestClient) -> None:
    client.post("/openbrain/capture", headers=_hdr(), json={"content": "duplicate"})
    r2 = client.post("/openbrain/capture", headers=_hdr(), json={"content": "duplicate"})
    assert r2.json()["was_existing"] is True


def test_openbrain_recent(client: TestClient) -> None:
    client.post("/openbrain/capture", headers=_hdr(), json={"content": "first"})
    client.post("/openbrain/capture", headers=_hdr(), json={"content": "second"})
    r = client.get("/openbrain/recent?limit=10", headers=_hdr())
    assert r.status_code == 200
    contents = [t["content"] for t in r.json()]
    assert set(contents) == {"first", "second"}


def test_openbrain_stats(client: TestClient) -> None:
    client.post("/openbrain/capture", headers=_hdr(), json={"content": "hi"})
    r = client.get("/openbrain/stats", headers=_hdr())
    assert r.status_code == 200
    payload = r.json()
    assert payload["thoughts"] >= 1


# ── Settings router ────────────────────────────────────────────────────────


def test_get_settings_returns_resolved_dict(client: TestClient) -> None:
    r = client.get("/settings", headers=_hdr())
    assert r.status_code == 200
    payload = r.json()
    assert payload["autonomy"]["default_policy"] == "balanced"


def test_get_settings_with_manager_includes_sources(tmp_path: Path, db: Database) -> None:
    p = tmp_path / "agent.yml"
    p.write_text("autonomy:\n  default_policy: cautious\n")
    mgr = SettingsManager(path=p, env={})
    app = create_app(db, mgr, api_tokens={TOKEN})
    c = TestClient(app)
    r = c.get("/settings", headers=_hdr())
    assert r.status_code == 200
    payload = r.json()
    assert payload["autonomy"]["default_policy"] == "cautious"
    assert "__sources__" in payload
    assert payload["__sources__"]["autonomy.default_policy"] == "file"


def test_get_presets_lists_three_named(client: TestClient) -> None:
    r = client.get("/settings/presets", headers=_hdr())
    assert r.status_code == 200
    assert set(r.json()) == {"cautious", "balanced", "aggressive"}
