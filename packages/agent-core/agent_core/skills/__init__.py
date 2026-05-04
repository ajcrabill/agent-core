"""agent_core.skills — Skill framework + registry.

A *skill* is a named capability the agent can invoke. Each skill bundles:

  - identity (``name``, ``description``, ``tags``)
  - input/output schemas (``input_schema``, ``output_schema`` — Pydantic models)
  - execution (``execute(input, context) -> SkillResult``)
  - optional pre-seeded learning rules (``seed_rules`` — installed when a
    user enables the skill so it has working defaults out of the box)

The Skill *Protocol* is the platform contract; the *SkillRegistry* lets
products (dcos-agent, ikb-agent) register their own skills and the agent
loop discover them by name or tag.

Skill execution is intentionally decoupled from the agent loop's
``StepExecutor`` — a step in a plan can invoke a skill, but skills don't
have to fire from a plan. (The CLI can call them directly:
``dcos skills run email-triage --input ...``.)

LLM access goes through ``LanguageModel`` (a small Protocol). Real Hermes
satisfies it; tests pass a stub. Skills don't need to know which.

Skill design rules:
    - A skill is for ONE thing. ``email-triage`` classifies; it doesn't
      also draft. Drafting belongs in ``email-composer``.
    - A skill that calls an LLM also returns a ``confidence`` so the
      calibration system can decide if it should run autonomously.
    - A skill that consults sources includes them in ``references`` so the
      caller can show provenance.
"""

from agent_core.skills.chat import (
    DEFAULT_SYSTEM_PROMPT,
    ChatMessage,
    ChatSession,
    build_context_prompt,
    run_turn,
)
from agent_core.skills.context import SkillContext, SkillResult
from agent_core.skills.openai_compat import (
    LanguageModelError,
    OpenAICompatLanguageModel,
    language_model_from_settings,
)
from agent_core.skills.protocol import LanguageModel, SeedRule, Skill
from agent_core.skills.registry import SkillRegistry, default_registry
from agent_core.skills.runner import (
    RunOutcome,
    SkillInputError,
    SkillNotFoundError,
    SkillOutputError,
    SkillRunner,
)
from agent_core.skills.stubs import StubLanguageModel
from agent_core.skills.tools import (
    CompletionResponse,
    ToolCall,
    ToolContext,
    ToolDefinition,
    default_read_tools,
    execute_tool_call,
    run_tool_loop,
)

__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "ChatMessage",
    "ChatSession",
    "CompletionResponse",
    "LanguageModel",
    "LanguageModelError",
    "OpenAICompatLanguageModel",
    "RunOutcome",
    "SeedRule",
    "Skill",
    "SkillContext",
    "SkillInputError",
    "SkillNotFoundError",
    "SkillOutputError",
    "SkillRegistry",
    "SkillResult",
    "SkillRunner",
    "StubLanguageModel",
    "ToolCall",
    "ToolContext",
    "ToolDefinition",
    "build_context_prompt",
    "default_read_tools",
    "default_registry",
    "execute_tool_call",
    "language_model_from_settings",
    "run_tool_loop",
    "run_turn",
]
