"""Backward-compat shim: the three reference skills now ship in agent-core.

Pre-Sprint-25, ``email-triage``, ``email-composer``, and ``document-creator``
lived under ``dcos_agent.skills``. They were always product-agnostic, so
they moved into ``agent_core.skills`` where both dcos-agent and ikb-agent
get them out of the box.

This module preserves the old import paths for code that hasn't yet
caught up:

    from dcos_agent.skills import EmailTriage   # still works
    import dcos_agent.skills                    # still triggers registration

The skills are auto-registered when ``agent_core.skills`` is imported, so
this shim is mainly for explicit imports — registration already happens.
"""

from agent_core.skills import (
    DocumentCreator,
    EmailComposer,
    EmailTriage,
    register_default_skills,
)


def register_defaults(registry=None):
    """Backward-compat alias for ``agent_core.skills.register_default_skills``."""
    register_default_skills(registry)


__all__ = [
    "DocumentCreator",
    "EmailComposer",
    "EmailTriage",
    "register_defaults",
]
