"""dcos-agent default skills.

Three reference skills ship out of the box:

  - ``email-triage``     — classifies an inbound email into one of six
                            actions (flag / archive / hold / draft /
                            track-relationship / task) per the taxonomy
                            documented in
                            Admin/Loriah Skills/learning-log/learning-log-data.md.

  - ``document-creator`` — drafts a document from a brief, optionally
                            grounded in semantic context from openbrain.

  - ``email-composer``   — drafts an email response, salutation-matched to
                            the recipient and grounded in any prior thread
                            context the caller provides.

Each skill is a thin orchestration layer around a LanguageModel call. When
``dcos_agent.skills`` is imported, the three are auto-registered into
``agent_core.skills.default_registry`` — products that want to start clean
build their own SkillRegistry instead.
"""

from agent_core.skills import default_registry

from dcos_agent.skills.document_creator import DocumentCreator
from dcos_agent.skills.email_composer import EmailComposer
from dcos_agent.skills.email_triage import EmailTriage


def register_defaults(registry=None):
    """Register the dcos-default skills on ``registry`` (default: process-wide).

    Idempotent — re-registering raises in the underlying registry, but this
    helper catches the duplicate and keeps the existing instance. Useful in
    tests where multiple modules import ``dcos_agent.skills``.
    """
    # Explicit None check — an empty SkillRegistry is falsy (len == 0) and
    # would silently fall through to the global default if we used `or`.
    target = default_registry if registry is None else registry
    for skill_cls in (EmailTriage, DocumentCreator, EmailComposer):
        skill = skill_cls()
        if skill.name in target:
            continue
        target.register(skill)


# Auto-register into the process-wide default at import time.
register_defaults()


__all__ = [
    "DocumentCreator",
    "EmailComposer",
    "EmailTriage",
    "register_defaults",
]
