"""Tests for agent_core.notifications — transports + urgency-gated dispatch.

We don't hit the real ntfy.sh in CI; an in-memory test transport captures
calls so we can assert on what would have been sent.
"""

from __future__ import annotations

import pytest
from agent_core.notifications import (
    Notification,
    NotificationDispatcher,
    NotificationFilteredError,
    NtfyTransport,
    Urgency,
)
from agent_core.notifications.transports import NoopTransport, NotificationTransport
from agent_core.settings import AgentSettings

# ── In-memory transport for assertions ──────────────────────────────────────


class _RecordingTransport:
    name = "recording"

    def __init__(self, *, succeed: bool = True) -> None:
        self.calls: list[dict] = []
        self.succeed = succeed

    def send(self, title, body, *, priority=3, tags=None) -> bool:
        self.calls.append({"title": title, "body": body, "priority": priority, "tags": tags or []})
        return self.succeed


# ── Urgency ─────────────────────────────────────────────────────────────────


def test_urgency_ordering() -> None:
    assert Urgency.info < Urgency.warn < Urgency.critical


def test_urgency_from_string_round_trips() -> None:
    for name in ("info", "warn", "critical"):
        assert Urgency.from_string(name).name == name


def test_urgency_from_string_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        Urgency.from_string("emergency")


# ── Dispatcher: filtering ──────────────────────────────────────────────────


def test_disabled_dispatcher_drops_everything() -> None:
    rec = _RecordingTransport()
    d = NotificationDispatcher(rec, enabled=False, urgency_floor=Urgency.info)
    result = d.notify(Notification(title="x", body="y", urgency=Urgency.critical))
    assert result.dropped
    assert result.reason == "disabled"
    assert rec.calls == []


def test_below_floor_dropped() -> None:
    rec = _RecordingTransport()
    d = NotificationDispatcher(rec, enabled=True, urgency_floor=Urgency.critical)
    result = d.notify(Notification(title="x", body="y", urgency=Urgency.warn))
    assert result.dropped
    assert result.reason == "below_floor"
    assert rec.calls == []


def test_at_floor_delivers() -> None:
    rec = _RecordingTransport()
    d = NotificationDispatcher(rec, enabled=True, urgency_floor=Urgency.warn)
    result = d.notify(Notification(title="x", body="y", urgency=Urgency.warn))
    assert result.delivered
    assert result.reason == "sent"
    assert len(rec.calls) == 1


def test_above_floor_delivers() -> None:
    rec = _RecordingTransport()
    d = NotificationDispatcher(rec, enabled=True, urgency_floor=Urgency.info)
    result = d.notify(Notification(title="x", body="y", urgency=Urgency.critical))
    assert result.delivered
    assert rec.calls[0]["priority"] == int(Urgency.critical)


def test_transport_failure_surfaced_in_result() -> None:
    rec = _RecordingTransport(succeed=False)
    d = NotificationDispatcher(rec, enabled=True, urgency_floor=Urgency.info)
    result = d.notify(Notification(title="x", body="y", urgency=Urgency.critical))
    assert result.dropped
    assert result.reason == "transport_failed"


def test_notify_or_raise_raises_when_dropped() -> None:
    d = NotificationDispatcher(_RecordingTransport(), enabled=False)
    with pytest.raises(NotificationFilteredError):
        d.notify_or_raise(Notification(title="x", body="y", urgency=Urgency.critical))


def test_notify_passes_tags_through() -> None:
    rec = _RecordingTransport()
    d = NotificationDispatcher(rec, enabled=True, urgency_floor=Urgency.info)
    d.notify(Notification(title="x", body="y", urgency=Urgency.warn, tags=["fire", "warning"]))
    assert rec.calls[0]["tags"] == ["fire", "warning"]


# ── Dispatcher: from_settings ──────────────────────────────────────────────


def test_from_settings_picks_noop_when_disabled() -> None:
    s = AgentSettings()  # defaults: notifications.enabled=False
    d = NotificationDispatcher.from_settings(s)
    assert isinstance(d.transport, NoopTransport)
    assert d.enabled is False


def test_from_settings_picks_noop_when_transport_none() -> None:
    s = AgentSettings(
        notifications={"enabled": True, "transport": "none"}  # type: ignore[arg-type]
    )
    d = NotificationDispatcher.from_settings(s)
    assert isinstance(d.transport, NoopTransport)


def test_from_settings_picks_ntfy_with_topic_and_server() -> None:
    s = AgentSettings(
        notifications={  # type: ignore[arg-type]
            "enabled": True,
            "transport": "ntfy",
            "ntfy_topic": "test-topic-xyz",
            "ntfy_server": "https://ntfy.example",
        }
    )
    d = NotificationDispatcher.from_settings(s)
    assert isinstance(d.transport, NtfyTransport)
    assert d.transport.topic == "test-topic-xyz"
    assert d.transport.server == "https://ntfy.example"
    assert d.transport.endpoint == "https://ntfy.example/test-topic-xyz"


def test_from_settings_ntfy_requires_topic() -> None:
    s = AgentSettings(
        notifications={  # type: ignore[arg-type]
            "enabled": True,
            "transport": "ntfy",
            "ntfy_topic": None,
        }
    )
    with pytest.raises(ValueError, match="ntfy_topic"):
        NotificationDispatcher.from_settings(s)


def test_from_settings_uses_urgency_floor() -> None:
    _RecordingTransport()
    s = AgentSettings(
        notifications={  # type: ignore[arg-type]
            "enabled": True,
            "transport": "ntfy",
            "ntfy_topic": "private-topic",
            "urgency_floor": "info",
        }
    )
    d = NotificationDispatcher.from_settings(s)
    assert d.urgency_floor == Urgency.info


def test_default_settings_keep_user_quiet() -> None:
    """Regression test: defaults must NOT push notifications out of the box.

    AJ's strong preference: quiet by default, opt-in to push."""
    s = AgentSettings()
    d = NotificationDispatcher.from_settings(s)
    assert isinstance(d.transport, NoopTransport)
    # Even a critical notification produces a 'disabled' drop.
    result = d.notify(Notification(title="anything", body="anything", urgency=Urgency.critical))
    assert result.dropped
    assert result.reason == "disabled"


# ── NtfyTransport: construction validation (no real HTTP) ──────────────────


def test_ntfy_transport_requires_topic() -> None:
    with pytest.raises(ValueError):
        NtfyTransport(topic="")


def test_ntfy_transport_strips_trailing_slash_from_server() -> None:
    t = NtfyTransport(topic="t", server="https://ntfy.sh/")
    assert t.server == "https://ntfy.sh"
    assert t.endpoint == "https://ntfy.sh/t"


def test_ntfy_send_rejects_invalid_priority() -> None:
    t = NtfyTransport(topic="t")
    with pytest.raises(ValueError):
        t.send("title", "body", priority=99)


def test_ntfy_send_rejects_comma_in_tag() -> None:
    t = NtfyTransport(topic="t")
    with pytest.raises(ValueError):
        t.send("title", "body", tags=["fine", "not, fine"])


def test_ntfy_satisfies_transport_protocol() -> None:
    t = NtfyTransport(topic="t")
    assert isinstance(t, NotificationTransport)


def test_noop_satisfies_transport_protocol() -> None:
    assert isinstance(NoopTransport(), NotificationTransport)


# ── NtfyTransport: HTTP behavior (mocked at urlopen level) ─────────────────


def test_ntfy_send_handles_network_error_gracefully(monkeypatch) -> None:
    import urllib.error

    def boom(*args, **kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    t = NtfyTransport(topic="t")
    # Should NOT raise — should return False so callers can route to digest.
    assert t.send("title", "body") is False


def test_ntfy_send_returns_true_on_2xx(monkeypatch) -> None:
    class _FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _FakeResp())
    t = NtfyTransport(topic="t")
    assert t.send("hi", "there", priority=2, tags=["bell"]) is True
