"""Sprint 7b — openbrain semantic memory tests."""

from __future__ import annotations

import pytest
from agent_core.openbrain import (
    EmbeddingProvider,
    OllamaEmbeddingProvider,
    OpenBrainStore,
    StubEmbeddingProvider,
    capture_thought,
    openbrain_stats,
    recent_thoughts,
    search_thoughts,
)
from agent_core.openbrain.embeddings import SemanticStubProvider
from agent_core.openbrain.store import _cosine, _fingerprint
from agent_core.state import Database, Thought, ThoughtSource
from sqlmodel import select


def _empty_db() -> Database:
    db = Database.sqlite_memory()
    db.create_all()
    return db


def _store() -> OpenBrainStore:
    return OpenBrainStore(_empty_db(), StubEmbeddingProvider())


# ── Helpers ─────────────────────────────────────────────────────────────────


def test_fingerprint_normalizes_whitespace_and_case() -> None:
    assert _fingerprint("Hello World") == _fingerprint("hello   world")
    assert _fingerprint("hello world") == _fingerprint("HELLO WORLD")
    assert _fingerprint("a") != _fingerprint("b")


def test_cosine_identical_vectors_returns_1() -> None:
    v = [0.5, 0.3, -0.2, 0.1]
    assert _cosine(v, v) == pytest.approx(1.0)


def test_cosine_opposite_vectors_returns_minus_1() -> None:
    a = [1.0, 0.0]
    b = [-1.0, 0.0]
    assert _cosine(a, b) == pytest.approx(-1.0)


def test_cosine_orthogonal_returns_0() -> None:
    a = [1.0, 0.0]
    b = [0.0, 1.0]
    assert _cosine(a, b) == pytest.approx(0.0)


def test_cosine_handles_empty_or_mismatched_vectors() -> None:
    assert _cosine([], [1, 2]) == 0.0
    assert _cosine([1, 2, 3], [1, 2]) == 0.0
    assert _cosine([0, 0], [0, 0]) == 0.0  # both zero magnitude


# ── EmbeddingProvider protocol ──────────────────────────────────────────────


def test_stub_embedding_deterministic() -> None:
    p = StubEmbeddingProvider()
    v1 = p.embed("hello world")
    v2 = p.embed("hello world")
    assert v1 == v2


def test_stub_embedding_different_inputs_differ() -> None:
    p = StubEmbeddingProvider()
    assert p.embed("hello") != p.embed("goodbye")


def test_stub_embedding_dimensions() -> None:
    p = StubEmbeddingProvider()
    assert len(p.embed("anything")) == p.dimensions


def test_stub_satisfies_protocol() -> None:
    assert isinstance(StubEmbeddingProvider(), EmbeddingProvider)
    assert isinstance(SemanticStubProvider(), EmbeddingProvider)


def test_semantic_stub_similar_text_gets_similar_vectors() -> None:
    """SemanticStubProvider gives high cosine for shared-vocabulary text."""
    p = SemanticStubProvider()
    a = p.embed("the quick brown fox is jumping")
    b = p.embed("the quick fox is jumping with a")
    c = p.embed("xyz qrs lmn opq jkl")
    assert _cosine(a, b) > _cosine(a, c)


def test_ollama_provider_model_id_format() -> None:
    p = OllamaEmbeddingProvider(model="nomic-embed-text")
    assert p.model_id == "ollama:nomic-embed-text"


# ── Capture ─────────────────────────────────────────────────────────────────


def test_capture_creates_thought_with_embedding() -> None:
    s = _store()
    t = s.capture("a thing worth remembering")
    assert t.id is not None
    assert t.embedding is not None
    assert t.embedding_model == "stub:hash-256"
    assert t.fingerprint is not None


def test_capture_idempotent_on_fingerprint() -> None:
    """Same content twice → same Thought row, single embedding pass."""
    s = _store()
    t1 = s.capture("dup content")
    t2 = s.capture("dup content")
    assert t1.id == t2.id


def test_capture_fingerprint_normalizes_whitespace() -> None:
    s = _store()
    t1 = s.capture("hello world")
    t2 = s.capture("hello    world")
    assert t1.id == t2.id


def test_capture_records_source_when_provided() -> None:
    db = _empty_db()
    s = OpenBrainStore(db, StubEmbeddingProvider())
    s.capture(
        "A piece of governance reading",
        source_kind="vault",
        source_uri="vault/governance/q3-rec.md",
        source_title="Q3 Recommendations",
        authority="canonical",
    )
    with db.session() as ses:
        sources = list(ses.exec(select(ThoughtSource)).all())
    assert len(sources) == 1
    assert sources[0].source_kind == "vault"
    assert sources[0].authority == "canonical"


def test_capture_dedup_appends_additional_source() -> None:
    """Same content seen in two places: one Thought, two ThoughtSource rows."""
    db = _empty_db()
    s = OpenBrainStore(db, StubEmbeddingProvider())
    t1 = s.capture("text", source_kind="vault", source_uri="path1")
    t2 = s.capture("text", source_kind="gmail", source_uri="thread/123")
    assert t1.id == t2.id
    with db.session() as ses:
        srcs = list(ses.exec(select(ThoughtSource)).all())
    assert len(srcs) == 2
    assert {s.source_kind for s in srcs} == {"vault", "gmail"}


def test_capture_metadata_persists() -> None:
    s = _store()
    t = s.capture("x", metadata={"author": "AJ", "topic": "governance"})
    fetched = s.db.session().__enter__().get(Thought, t.id)
    assert fetched.metadata_json == {"author": "AJ", "topic": "governance"}


# ── Reindex ─────────────────────────────────────────────────────────────────


def test_reindex_updates_embedding_with_current_provider() -> None:
    db = _empty_db()
    s1 = OpenBrainStore(db, StubEmbeddingProvider())
    t = s1.capture("some content")
    old_embedding = list(t.embedding or [])
    old_model = t.embedding_model

    # Switch to a different provider
    s2 = OpenBrainStore(db, SemanticStubProvider())
    s2.reindex(t.id)
    refreshed = s2.recent()[0]
    assert refreshed.embedding != old_embedding
    assert refreshed.embedding_model == "stub:semantic-256"
    assert refreshed.embedding_model != old_model


def test_reindex_unknown_id_raises() -> None:
    s = _store()
    with pytest.raises(ValueError, match="not found"):
        s.reindex("nope")


# ── Search ──────────────────────────────────────────────────────────────────


def test_search_returns_self_with_max_similarity() -> None:
    """Searching for the exact same content as a stored thought should return
    that thought first with similarity ≈ 1.0."""
    s = _store()
    s.capture("the quick brown fox")
    s.capture("a totally different sentence")
    hits = s.search("the quick brown fox", limit=5)
    assert len(hits) >= 1
    assert hits[0].thought.content == "the quick brown fox"
    assert hits[0].similarity > 0.99


def test_search_orders_by_similarity() -> None:
    """SemanticStub: similar content scores higher than dissimilar."""
    db = _empty_db()
    s = OpenBrainStore(db, SemanticStubProvider())
    s.capture("the quick brown fox jumps over the lazy dog")
    s.capture("the fox is quick and brown")
    s.capture("zyxwvut nothing in common with anything")

    hits = s.search("a quick fox", limit=3)
    contents_by_score = [h.thought.content for h in hits]
    # The two fox sentences should be first; the gibberish last
    assert "fox" in contents_by_score[0].lower()
    assert "fox" in contents_by_score[1].lower()
    assert "zyxwvut" in contents_by_score[2]


def test_search_respects_threshold() -> None:
    db = _empty_db()
    s = OpenBrainStore(db, StubEmbeddingProvider())
    s.capture("a")
    s.capture("b")
    # Threshold > 1.0 → nothing matches
    assert s.search("c", limit=5, threshold=2.0) == []


def test_search_respects_limit() -> None:
    s = _store()
    for i in range(20):
        s.capture(f"thought number {i}")
    assert len(s.search("query", limit=3)) == 3


def test_search_filters_by_source_kind() -> None:
    db = _empty_db()
    s = OpenBrainStore(db, StubEmbeddingProvider())
    s.capture("from vault", source_kind="vault")
    s.capture("from gmail", source_kind="gmail")
    s.capture("from drive", source_kind="drive")

    vault_hits = s.search("query", limit=10, source_filter="vault")
    contents = [h.thought.content for h in vault_hits]
    assert "from vault" in contents
    assert "from gmail" not in contents
    assert "from drive" not in contents


def test_search_skips_thoughts_with_different_embedding_model() -> None:
    """Cross-model comparison would be meaningless; skip silently."""
    db = _empty_db()
    s_stub = OpenBrainStore(db, StubEmbeddingProvider())
    s_stub.capture("indexed by stub")
    # Pretend we switched models — search via a different provider sees no results
    s_semantic = OpenBrainStore(db, SemanticStubProvider())
    hits = s_semantic.search("anything", limit=5)
    assert hits == []


def test_search_attaches_source_provenance() -> None:
    db = _empty_db()
    s = OpenBrainStore(db, StubEmbeddingProvider())
    s.capture(
        "content with provenance",
        source_kind="vault",
        source_uri="path/to/doc.md",
        source_title="Doc Title",
    )
    hits = s.search("content with provenance", limit=1)
    assert len(hits) == 1
    assert len(hits[0].sources) == 1
    assert hits[0].sources[0].source_kind == "vault"
    assert hits[0].sources[0].source_title == "Doc Title"


# ── Recent ──────────────────────────────────────────────────────────────────


def test_recent_returns_newest_first() -> None:
    s = _store()
    s.capture("first")
    s.capture("second")
    s.capture("third")
    rows = s.recent()
    assert rows[0].content == "third"
    assert rows[1].content == "second"


def test_recent_respects_limit() -> None:
    s = _store()
    for i in range(20):
        s.capture(f"x{i}")
    assert len(s.recent(limit=5)) == 5


def test_recent_filters_by_source_kind() -> None:
    db = _empty_db()
    s = OpenBrainStore(db, StubEmbeddingProvider())
    s.capture("vault one", source_kind="vault")
    s.capture("gmail one", source_kind="gmail")
    s.capture("vault two", source_kind="vault")
    rows = s.recent(source_filter="vault")
    contents = {r.content for r in rows}
    assert contents == {"vault one", "vault two"}


# ── Stats ───────────────────────────────────────────────────────────────────


def test_stats_reports_basic_counts() -> None:
    db = _empty_db()
    s = OpenBrainStore(db, StubEmbeddingProvider())
    s.capture("a", source_kind="vault")
    s.capture("b", source_kind="vault")
    s.capture("c", source_kind="gmail")
    st = s.stats()
    assert st["thoughts"] == 3
    assert st["embedded"] == 3
    assert st["embedding_model"] == "stub:hash-256"
    assert st["sources_total"] == 3
    assert st["sources_by_kind"] == {"vault": 2, "gmail": 1}


def test_stats_handles_empty_store() -> None:
    s = _store()
    st = s.stats()
    assert st["thoughts"] == 0
    assert st["embedded"] == 0


# ── MCP-tool wrappers ──────────────────────────────────────────────────────


def test_capture_thought_returns_dict_with_was_existing() -> None:
    s = _store()
    r1 = capture_thought(s, content="thing")
    assert r1["was_existing"] is False
    r2 = capture_thought(s, content="thing")
    assert r2["was_existing"] is True
    assert r1["id"] == r2["id"]


def test_search_thoughts_returns_dicts_with_similarity() -> None:
    s = _store()
    s.capture("first")
    s.capture("second")
    out = search_thoughts(s, query="first", limit=2)
    assert len(out) == 2
    assert "similarity" in out[0]
    assert "content" in out[0]


def test_search_thoughts_includes_source_provenance() -> None:
    db = _empty_db()
    s = OpenBrainStore(db, StubEmbeddingProvider())
    s.capture(
        "x",
        source_kind="vault",
        source_uri="vault/x.md",
    )
    out = search_thoughts(s, query="x", limit=1)
    assert out[0]["sources"][0]["source_kind"] == "vault"
    assert out[0]["sources"][0]["source_uri"] == "vault/x.md"


def test_recent_thoughts_dict_shape() -> None:
    s = _store()
    s.capture("recent one")
    out = recent_thoughts(s, limit=1)
    assert out[0]["content"] == "recent one"
    assert "created_at" in out[0]


def test_openbrain_stats_returns_dict() -> None:
    s = _store()
    s.capture("x")
    st = openbrain_stats(s)
    assert st["thoughts"] == 1


# ── End-to-end: realistic skill-using scenario ──────────────────────────────


def test_e2e_capture_then_search_for_relevant_context() -> None:
    """Realistic: a skill captures notes during an iteration, then later
    searches OpenBrain for relevant past context to inform the next answer."""
    db = _empty_db()
    s = OpenBrainStore(db, SemanticStubProvider())

    s.capture(
        "Q3 board meeting: discussed the budget gap and the rightsizing plan.",
        source_kind="vault",
        source_uri="vault/Q3-board-mtg.md",
        source_title="Q3 board meeting notes",
    )
    s.capture(
        "Charlotte mentioned the Q3 budget gap on a call last Tuesday.",
        source_kind="gmail",
        source_uri="gmail/thread/123",
    )
    s.capture(
        "Random unrelated thought about lunch.",
        source_kind="other",
    )

    hits = s.search("budget gap", limit=2)
    assert len(hits) == 2
    contents = [h.thought.content.lower() for h in hits]
    assert any("budget gap" in c for c in contents)
    # Both budget-related hits surface; lunch-thought is excluded
    assert not any("lunch" in c for c in contents)


# ── Settings wiring (Sprint 7.5b) ───────────────────────────────────────────


def test_from_settings_picks_stub_provider() -> None:
    from agent_core.settings import AgentSettings

    s = AgentSettings(openbrain={"embedding_provider": "stub"})  # type: ignore[arg-type]
    store = OpenBrainStore.from_settings(s, _empty_db())
    assert isinstance(store.embeddings, StubEmbeddingProvider)
    assert store.embeddings.model_id == "stub:hash-256"


def test_from_settings_picks_semantic_stub() -> None:
    from agent_core.settings import AgentSettings

    s = AgentSettings(openbrain={"embedding_provider": "stub-semantic"})  # type: ignore[arg-type]
    store = OpenBrainStore.from_settings(s, _empty_db())
    assert isinstance(store.embeddings, SemanticStubProvider)


def test_from_settings_picks_ollama_with_configured_model() -> None:
    from agent_core.settings import AgentSettings

    s = AgentSettings(
        openbrain={  # type: ignore[arg-type]
            "embedding_provider": "ollama",
            "embedding_model": "custom-model",
            "ollama_base_url": "http://example:9999",
        }
    )
    store = OpenBrainStore.from_settings(s, _empty_db())
    assert isinstance(store.embeddings, OllamaEmbeddingProvider)
    assert store.embeddings.model == "custom-model"
    assert store.embeddings.base_url == "http://example:9999"
    assert store.embeddings.model_id == "ollama:custom-model"


def test_from_settings_propagates_search_defaults() -> None:
    from agent_core.settings import AgentSettings

    s = AgentSettings(
        openbrain={  # type: ignore[arg-type]
            "embedding_provider": "stub",
            "search_default_limit": 17,
            "search_default_threshold": 0.42,
        }
    )
    store = OpenBrainStore.from_settings(s, _empty_db())
    assert store.search_default_limit == 17
    assert store.search_default_threshold == pytest.approx(0.42)


def test_search_uses_settings_default_limit_when_unspecified() -> None:
    """End-to-end: bumping search_default_limit in settings widens results
    without callers passing limit=."""
    db = _empty_db()
    store = OpenBrainStore(
        db,
        StubEmbeddingProvider(),
        search_default_limit=2,
        search_default_threshold=0.0,
    )
    for i in range(10):
        store.capture(f"thought {i}")
    assert len(store.search("query")) == 2  # honors store default

    store.search_default_limit = 7
    assert len(store.search("query")) == 7

    # Explicit limit= still wins
    assert len(store.search("query", limit=3)) == 3


def test_from_settings_rejects_unknown_provider() -> None:
    from agent_core.settings import AgentSettings

    # Bypass the schema literal validation by constructing through model_construct
    s = AgentSettings()
    object.__setattr__(s.openbrain, "embedding_provider", "imaginary")
    with pytest.raises(ValueError, match="unknown openbrain.embedding_provider"):
        OpenBrainStore.from_settings(s, _empty_db())
