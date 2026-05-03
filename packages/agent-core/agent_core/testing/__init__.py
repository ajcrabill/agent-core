"""agent_core.testing — reusable test harness for agent-core and downstream packages.

Three pieces:

  - **stubs**: standard implementations of every injectable Protocol
    (PlanDeveloper, StepExecutor, AuditorModel, DiffExtractor, etc.). Lifted
    from the per-module test files so dcos-agent / ikb-agent / skill packages
    don't reinvent them.

  - **AgentTestBed**: a fully-wired, in-memory agent. One line to construct;
    every component (db, settings, openbrain, dispatcher, calibration,
    auditor, agent loop) is built via ``from_settings`` and reachable as a
    property. Tests interact with the bed the way real callers will.

  - **scenarios**: high-level helpers for common flows — ``receive_inbound``,
    ``capture_correction``, ``audit_skill_run``. Each returns the persisted
    domain objects so tests can assert against state, not stubs.

Use it like this::

    from agent_core.testing import AgentTestBed, scenarios

    bed = AgentTestBed.create()                              # all defaults
    bed_a = AgentTestBed.create(preset="aggressive")         # named preset
    bed_b = AgentTestBed.create(settings={"learning":         # raw overrides
                                          {"detector_strictness": "strict"}})

    obligation = scenarios.receive_inbound(bed, channel="chat",
                                           body="please fix the typo")
    assert obligation.status == ObligationStatus.inbox
"""

from agent_core.testing import scenarios
from agent_core.testing.agentbed import AgentTestBed
from agent_core.testing.stubs import (
    StubAuditorModel,
    StubCompletionVerifier,
    StubDiffExtractor,
    StubPlanDeveloper,
    StubStepExecutor,
)

__all__ = [
    "AgentTestBed",
    "StubAuditorModel",
    "StubCompletionVerifier",
    "StubDiffExtractor",
    "StubPlanDeveloper",
    "StubStepExecutor",
    "scenarios",
]
