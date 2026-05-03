"""ContextLoader tests.

Covers the five blocks (obligations, general rules, skill rules, intercom,
incidents) plus the ContextBundle rendering and the obligation sort order.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agent_core.agent import (
    ContextBundle,
    ContextLoader,
    ContextScope,
)
from agent_core.state import (
    Database,
    Identity,
    Incident,
    IncidentSeverity,
    IncidentStatus,
    IntercomMessage,
    IntercomState,
    LearningRule,
    Obligation,
    ObligationOwner,
    ObligationStatus,
)


def _empty_db() -> Database:
    db = Database.sqlite_memory()
    db.create_all()
    return db


# ── Empty database ───────────────────────────────────────────────────────────


def test_collect_on_empty_db_yields_all_empty_blocks() -> None:
    loader = ContextLoader(_empty_db())
    bundle = loader.collect()
    assert len(bundle.blocks) == 5  # always 5 named blocks
    assert all(b.is_empty for b in bundle.blocks)
    assert bundle.non_empty() == []
    assert bundle.as_preamble() == ""


def test_collect_with_skill_scope_includes_skill_rules_block() -> None:
    bundle = ContextLoader(_empty_db()).collect(ContextScope(skill="email-triage"))
    skill_block = bundle.by_name("skill_rules")
    assert skill_block is not None
    assert skill_block.title == "Rules for 'email-triage'"
    assert skill_block.is_empty


# ── Obligations block ────────────────────────────────────────────────────────


def test_obligations_block_lists_active_agent_owned() -> None:
    db = _empty_db()
    with db.session() as s:
        s.add(Obligation(title="Reply to Charlotte", status=ObligationStatus.inbox))
        s.add(
            Obligation(
                title="Triage Q3 emails",
                status=ObligationStatus.in_progress,
                priority=2,
            )
        )
        s.add(
            Obligation(
                title="Already done",
                status=ObligationStatus.done,
            )
        )
        s.add(
            Obligation(
                title="Owned by principal",
                owner=ObligationOwner.principal,
            )
        )
        s.commit()

    bundle = ContextLoader(db).collect()
    obs = bundle.by_name("obligations")
    assert obs is not None
    assert not obs.is_empty
    assert obs.meta["count"] == 2
    # in-progress comes first
    assert obs.content.split("\n")[0].startswith("- **Triage Q3 emails**")
    assert "Reply to Charlotte" in obs.content
    assert "Already done" not in obs.content
    assert "Owned by principal" not in obs.content


def test_obligations_block_top_n_capped() -> None:
    db = _empty_db()
    with db.session() as s:
        for i in range(20):
            s.add(Obligation(title=f"task {i}", status=ObligationStatus.in_progress))
        s.commit()
    bundle = ContextLoader(db, top_n_obligations=3).collect()
    assert bundle.by_name("obligations").meta["count"] == 3


def test_obligations_sort_status_then_priority_then_due() -> None:
    db = _empty_db()
    now = datetime.now(UTC)
    with db.session() as s:
        # Status priority test
        s.add(
            Obligation(
                title="A_inbox_p1",
                status=ObligationStatus.inbox,
                priority=1,
            )
        )
        s.add(
            Obligation(
                title="B_waiting_p1",
                status=ObligationStatus.waiting,
                priority=1,
            )
        )
        s.add(
            Obligation(
                title="C_inprog_p0",
                status=ObligationStatus.in_progress,
                priority=0,
            )
        )
        s.add(
            Obligation(
                title="D_inprog_p2_due_today",
                status=ObligationStatus.in_progress,
                priority=2,
                due_at=now + timedelta(hours=2),
            )
        )
        s.commit()
    bundle = ContextLoader(db).collect()
    content = bundle.by_name("obligations").content
    # Order: in-progress (D > C by priority), then waiting (B), then inbox (A)
    titles_in_order = [
        line.split("**")[1] for line in content.split("\n") if line.startswith("- **")
    ]
    assert titles_in_order == [
        "D_inprog_p2_due_today",
        "C_inprog_p0",
        "B_waiting_p1",
        "A_inbox_p1",
    ]


def test_obligations_block_includes_criteria_count() -> None:
    db = _empty_db()
    with db.session() as s:
        s.add(
            Obligation(
                title="t",
                completion_criteria=[
                    {"type": "email_sent"},
                    {"type": "principal_ratification"},
                ],
            )
        )
        s.commit()
    content = ContextLoader(db).collect().by_name("obligations").content
    assert "2 criteria" in content


# ── General rules block ──────────────────────────────────────────────────────


def test_general_rules_block_includes_general_tagged() -> None:
    db = _empty_db()
    with db.session() as s:
        s.add(LearningRule(correction="Be concise.", skill_tags=["general"]))
        s.add(LearningRule(correction="Skill-only rule", skill_tags=["email-triage"]))
        s.commit()
    bundle = ContextLoader(db).collect()
    gen = bundle.by_name("general_rules")
    assert "Be concise." in gen.content
    assert "Skill-only rule" not in gen.content


def test_general_rules_drops_superseded() -> None:
    db = _empty_db()
    with db.session() as s:
        old = LearningRule(correction="OLD rule", skill_tags=["general"])
        s.add(old)
        s.commit()
        new = LearningRule(correction="NEW rule", skill_tags=["general"])
        s.add(new)
        s.commit()
        old.superseded_by = new.id
        s.commit()
    content = ContextLoader(db).collect().by_name("general_rules").content
    assert "OLD rule" not in content
    assert "NEW rule" in content


# ── Skill rules block ────────────────────────────────────────────────────────


def test_skill_rules_block_filtered_by_skill() -> None:
    db = _empty_db()
    with db.session() as s:
        s.add(LearningRule(correction="email rule", skill_tags=["email-triage"]))
        s.add(LearningRule(correction="doc rule", skill_tags=["document-creator"]))
        s.add(
            LearningRule(correction="multi rule", skill_tags=["email-triage", "document-creator"])
        )
        s.commit()
    bundle = ContextLoader(db).collect(ContextScope(skill="email-triage"))
    sk = bundle.by_name("skill_rules")
    assert "email rule" in sk.content
    assert "multi rule" in sk.content
    assert "doc rule" not in sk.content


def test_skill_rules_empty_when_no_skill_scope() -> None:
    db = _empty_db()
    with db.session() as s:
        s.add(LearningRule(correction="x", skill_tags=["whatever"]))
        s.commit()
    bundle = ContextLoader(db).collect()
    assert bundle.by_name("skill_rules").is_empty


# ── Intercom block ───────────────────────────────────────────────────────────


def test_intercom_empty_without_identity() -> None:
    """Fresh-install state: no Identity row → intercom block is empty."""
    db = _empty_db()
    with db.session() as s:
        s.add(IntercomMessage(sender="someone", recipient="me", body="hi"))
        s.commit()
    bundle = ContextLoader(db).collect()
    assert bundle.by_name("intercom").is_empty
    assert bundle.by_name("intercom").meta.get("reason") == "no identity row"


def test_intercom_lists_unread_for_self() -> None:
    db = _empty_db()
    with db.session() as s:
        s.add(Identity(instance_name="Loriah"))
        s.add(IntercomMessage(sender="Esby", recipient="Loriah", body="status update?"))
        s.add(
            IntercomMessage(
                sender="Esby",
                recipient="Loriah",
                body="ack me",
                state=IntercomState.acknowledged,
            )
        )
        s.add(IntercomMessage(sender="Esby", recipient="Other", body="not for me"))
        s.commit()
    content = ContextLoader(db).collect().by_name("intercom").content
    assert "status update?" in content
    assert "ack me" not in content
    assert "not for me" not in content


def test_intercom_truncates_long_body() -> None:
    db = _empty_db()
    long_body = "x" * 500
    with db.session() as s:
        s.add(Identity(instance_name="me"))
        s.add(IntercomMessage(sender="someone", recipient="me", body=long_body))
        s.commit()
    content = ContextLoader(db).collect().by_name("intercom").content
    assert "…" in content
    assert "x" * 500 not in content


# ── Incidents block ──────────────────────────────────────────────────────────


def test_incidents_block_lists_open() -> None:
    db = _empty_db()
    with db.session() as s:
        s.add(Incident(title="Tool failed", source="tool_call"))
        s.add(
            Incident(
                title="Resolved one",
                status=IncidentStatus.resolved,
                source="cron",
            )
        )
        s.add(
            Incident(
                title="Critical thing",
                severity=IncidentSeverity.critical,
                source="cron",
            )
        )
        s.commit()
    bundle = ContextLoader(db).collect()
    inc = bundle.by_name("incidents")
    assert "Tool failed" in inc.content
    assert "Critical thing" in inc.content
    assert "Resolved one" not in inc.content


def test_incidents_block_caps_count() -> None:
    db = _empty_db()
    with db.session() as s:
        for i in range(10):
            s.add(Incident(title=f"inc {i}", source="cron"))
        s.commit()
    bundle = ContextLoader(db, incidents_limit=3).collect()
    assert bundle.by_name("incidents").meta["count"] == 3


# ── Bundle rendering ─────────────────────────────────────────────────────────


def test_bundle_as_preamble_skips_empty_blocks() -> None:
    db = _empty_db()
    with db.session() as s:
        s.add(Obligation(title="solo task"))
        s.commit()
    preamble = ContextLoader(db).collect().as_preamble()
    # Only the obligations block should appear
    assert "## Active obligations" in preamble
    assert "## General learning rules" not in preamble
    assert "## Open incidents" not in preamble
    assert "## Unread intercom" not in preamble


def test_bundle_as_preamble_block_order_stable() -> None:
    """Block order: obligations → general_rules → skill_rules → intercom → incidents."""
    db = _empty_db()
    with db.session() as s:
        s.add(Identity(instance_name="me"))
        s.add(Obligation(title="ob"))
        s.add(LearningRule(correction="general rule", skill_tags=["general"]))
        s.add(LearningRule(correction="skill rule", skill_tags=["s"]))
        s.add(IntercomMessage(sender="x", recipient="me", body="msg"))
        s.add(Incident(title="inc", source="t"))
        s.commit()
    preamble = ContextLoader(db).collect(ContextScope(skill="s")).as_preamble()
    pos_obs = preamble.index("## Active obligations")
    pos_gen = preamble.index("## General learning rules")
    pos_skill = preamble.index("## Rules for 's'")
    pos_inter = preamble.index("## Unread intercom")
    pos_inc = preamble.index("## Open incidents")
    assert pos_obs < pos_gen < pos_skill < pos_inter < pos_inc


def test_bundle_by_name_returns_block_or_none() -> None:
    bundle = ContextBundle(
        blocks=[
            type(
                "B",
                (),
                {"name": "x", "is_empty": False, "title": "X", "content": "", "meta": {}},
            )()
        ]
    )
    assert bundle.by_name("x").name == "x"
    assert bundle.by_name("missing") is None


# ── Smoke / integration ──────────────────────────────────────────────────────


def test_full_realistic_scenario() -> None:
    """Realistic mix: 2 obligations, 1 general rule, 1 skill rule, 1 intercom,
    1 incident — all blocks live, preamble contains everything in order."""
    db = _empty_db()
    with db.session() as s:
        s.add(Identity(instance_name="Loriah"))
        s.add(
            Obligation(
                title="Reply to charlotte",
                status=ObligationStatus.in_progress,
                priority=2,
                completion_criteria=[{"type": "email_sent", "to": "c@x"}],
            )
        )
        s.add(Obligation(title="Triage week", status=ObligationStatus.inbox))
        s.add(
            LearningRule(
                correction="Be concise. No filler.",
                skill_tags=["general"],
            )
        )
        s.add(
            LearningRule(
                correction="BD emails: lead with the prospect's recent post.",
                skill_tags=["email-composer"],
            )
        )
        s.add(
            IntercomMessage(
                sender="Esby",
                recipient="Loriah",
                body="Q3 metrics ready",
            )
        )
        s.add(
            Incident(
                title="Gmail OAuth refresh failed",
                severity=IncidentSeverity.high,
                source="cron",
            )
        )
        s.commit()
    bundle = ContextLoader(db).collect(ContextScope(skill="email-composer"))
    preamble = bundle.as_preamble()
    # Every block surfaces something
    for needle in (
        "Reply to charlotte",
        "Be concise.",
        "lead with the prospect",
        "Q3 metrics ready",
        "Gmail OAuth refresh failed",
    ):
        assert needle in preamble, f"missing in preamble: {needle!r}"
    assert len(bundle.non_empty()) == 5
