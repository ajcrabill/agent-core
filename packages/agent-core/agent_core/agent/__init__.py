"""agent_core.agent — context-loader hooks + (Sprint 2.5) goal-directed loop.

This module is the part of agent-core that talks to the language model. The
context loader is what makes "state lives in code, not in instructions" real
(see README design principle #3): every model invocation is preceded by a
deterministic context-collection step that injects:

  - Top-N pending agent-owned obligations
  - Applicable learning rules (general always; skill-scoped when a skill runs)
  - Unread intercom messages (mesh peers)
  - Open incidents (failures the agent must consult before claiming completion)

The model never decides whether to load these — they show up in the prompt.
This is the architectural antidote to the two reliability bugs found in the
phase-A audit (forgotten obligations, ignored learning rules).

Sprint 2.5 adds the goal-directed agent loop on top, which uses the context
loader to drive the "while there are active obligations, plan or execute"
cycle from L20.
"""

from agent_core.agent.context import (
    ContextBlock,
    ContextBundle,
    ContextLoader,
    ContextScope,
)
from agent_core.agent.loop import AgentLoop, TickOutcome
from agent_core.agent.protocols import (
    PlanDeveloper,
    PlanProposal,
    StepExecutor,
    StepResult,
)
from agent_core.agent.verify import (
    CheckResult,
    CompletionVerifier,
    VerifyOutcome,
)

__all__ = [
    # Context
    "ContextBlock",
    "ContextBundle",
    "ContextLoader",
    "ContextScope",
    # Loop
    "AgentLoop",
    "TickOutcome",
    # Protocols
    "PlanDeveloper",
    "PlanProposal",
    "StepExecutor",
    "StepResult",
    # Verifiers
    "CheckResult",
    "CompletionVerifier",
    "VerifyOutcome",
]
