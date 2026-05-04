"""Settings schema — every user-tunable knob, declared in one place.

Each section is a plain Pydantic ``BaseModel`` with conservative defaults.
The root ``AgentSettings`` composes them. ``SettingsManager`` adds env-var
and YAML loading on top.

Adding a new knob (the only file you should need to touch):
    1. Add the field to the relevant section model with a default + description.
    2. If the knob is part of a preset, mention it in ``presets.py``.
    3. The CLI (``settings show / set``) picks it up automatically.

Design rules:
    - Every field has a default. ``agent.yml`` is *purely* an overlay.
    - Use literal types instead of free strings for choice fields so the
      CLI can validate at parse time.
    - Never expose a field whose only sensible value is hardcoded — keep
      configuration honest.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ── Autonomy ────────────────────────────────────────────────────────────────


PolicyKindLiteral = Literal["autonomous", "gated", "forbidden"]
PresetName = Literal["cautious", "balanced", "aggressive"]


class AutonomySettings(BaseModel):
    """Per-install autonomy posture.

    ``default_policy`` selects one of the three named presets; individual
    actions can be overridden via ``per_action_overrides``. The forbidden
    set is honored even if a preset would otherwise promote it — escaping
    requires editing this file directly.
    """

    model_config = ConfigDict(extra="forbid")

    default_policy: PresetName = Field(
        default="balanced",
        description="Named preset that drives unspecified action classes.",
    )
    per_action_overrides: dict[str, PolicyKindLiteral] = Field(
        default_factory=dict,
        description=(
            "Map ActionClass name → PolicyKind. Wins over the preset for the "
            "named action. Example: {'send_email_external': 'autonomous'}."
        ),
    )
    auto_promote_after_n_successes: int = Field(
        default=10,
        ge=1,
        description=(
            "When a gated skill succeeds N times in a row, the agent surfaces "
            "a one-click prompt to promote it to autonomous."
        ),
    )
    auto_undelegate_after_n_failures: int = Field(
        default=2,
        ge=1,
        description=(
            "Auditor pulls a skill back from autonomous after this many "
            "consecutive failures."
        ),
    )
    archive_instead_of_delete: bool = Field(
        default=True,
        description=(
            "L23: when an autonomous action would delete, archive instead. "
            "Reversible for `archive_retention_days`. Recommended at any "
            "autonomy level below 'aggressive'."
        ),
    )
    archive_retention_days: int = Field(
        default=30,
        ge=0,
        description=(
            "Days an archived item stays recoverable before it is hard-deleted. "
            "Set to 0 to keep archived items forever (manual cleanup only)."
        ),
    )
    require_confirm_for_hard_delete: bool = Field(
        default=True,
        description=(
            "Even when `archive_instead_of_delete=False`, prompt before "
            "permanent deletes. Defense in depth — flip off only if you "
            "know what you're doing."
        ),
    )


# ── Learning ────────────────────────────────────────────────────────────────


DetectorStrictness = Literal["loose", "balanced", "strict"]


class LearningSettings(BaseModel):
    """Supervised + agentic feedback learning."""

    model_config = ConfigDict(extra="forbid")

    supervised_capture_enabled: bool = Field(
        default=True,
        description="Auto-capture corrections from any inbound channel.",
    )
    detector_strictness: DetectorStrictness = Field(
        default="balanced",
        description=(
            "loose: catch only obvious 'no, do X instead' patterns. "
            "balanced: also catch implicit corrections ('actually,', "
            "'use X, not Y'). strict: aggressive — may produce candidates "
            "that need human review to discard."
        ),
    )
    auto_promote_confidence: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description=(
            "Candidate confidence at or above this auto-promotes to a rule "
            "without explicit user approval."
        ),
    )
    min_observations_to_promote: int = Field(
        default=3,
        ge=1,
        description="A candidate must be observed at least this many times before auto-promotion.",
    )
    journal_capture_every_n_messages: int = Field(
        default=10,
        ge=1,
        description="Rolling journal write cadence (in user messages).",
    )
    agentic_feedback_enabled: bool = Field(
        default=True,
        description="Enable the auditor → calibration → autonomous-mode loop.",
    )
    synthetic_battery_enabled: bool = Field(
        default=False,
        description=(
            "L21: run synthetic edge-case battery against new skills before "
            "promoting to autonomous. Costs LLM tokens; default off."
        ),
    )
    # ── Detector tuning ────────────────────────────────────────────────────
    detector_min_confidence: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description="Heuristic detector drops candidate corrections below this confidence.",
    )
    # ── Maintenance scan (duplicates / staleness / consolidation) ──────────
    maintenance_duplicate_threshold: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Jaccard similarity above which two rules are flagged as possible duplicates.",
    )
    maintenance_stale_days: int = Field(
        default=90,
        ge=1,
        description="Active rules untouched for this many days are flagged for review.",
    )
    maintenance_compactable_min_cluster: int = Field(
        default=5,
        ge=2,
        description="N rules sharing a tag triggers a 'consider consolidating' suggestion.",
    )
    # ── Weekly review window ───────────────────────────────────────────────
    weekly_review_window_days: int = Field(
        default=7,
        ge=1,
        description="Lookback window for the weekly learning review.",
    )
    # ── Graduation criteria (when a skill exits supervised learning) ───────
    graduation_window_days: int = Field(
        default=7,
        ge=1,
        description="Consecutive days of sustained accuracy required to graduate a skill.",
    )
    graduation_min_accuracy: float = Field(
        default=0.9,
        ge=0.0,
        le=1.0,
        description="Accuracy floor (per-day) for the graduation window to count.",
    )
    graduation_min_observations_per_type: int = Field(
        default=50,
        ge=1,
        description="Minimum AJ-coded items per action type before graduation is even possible.",
    )
    # ── Confidence buckets (display + audit triage) ────────────────────────
    confidence_high: float = Field(
        default=0.80,
        ge=0.0,
        le=1.0,
        description="Scores at or above this render as 'high confidence'.",
    )
    confidence_medium: float = Field(
        default=0.50,
        ge=0.0,
        le=1.0,
        description="Scores at or above this (but below `confidence_high`) render as 'medium'.",
    )
    # ── Synthetic edge-case battery (L21) thresholds ───────────────────────
    synthetic_min_natural_exemplars: int = Field(
        default=15,
        ge=1,
        description="L21 won't generate synthetic items until at least this many natural exemplars exist.",
    )
    synthetic_min_days_of_data: int = Field(
        default=7,
        ge=1,
        description="L21 also requires this many days of natural data span.",
    )
    synthetic_min_correction_themes: int = Field(
        default=3,
        ge=1,
        description="L21 also requires at least this many distinct correction themes.",
    )


# ── Notifications ───────────────────────────────────────────────────────────


NotificationTransport = Literal["ntfy", "none"]
UrgencyLevel = Literal["info", "warn", "critical"]


class NotificationSettings(BaseModel):
    """How (and how aggressively) the agent reaches the user out-of-band.

    Defaults are intentionally quiet: notifications off, urgency floor at
    'critical'. Users who want more push activity dial these knobs up.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description="Master switch. Off → only the daily digest reaches the user.",
    )
    transport: NotificationTransport = Field(
        default="ntfy",
        description="Push transport. 'none' disables all push regardless of `enabled`.",
    )
    ntfy_topic: str | None = Field(
        default=None,
        description=(
            "ntfy.sh topic name (the URL path component). Pick something "
            "unguessable — anyone with the topic can read your notifications."
        ),
    )
    ntfy_server: str = Field(
        default="https://ntfy.sh",
        description="Server base URL. Self-hosted ntfy works; just point this at it.",
    )
    urgency_floor: UrgencyLevel = Field(
        default="critical",
        description=(
            "Only deliver notifications at or above this urgency. "
            "'critical' = quiet (recommended). 'info' = aggressive."
        ),
    )
    daily_digest_enabled: bool = Field(
        default=True,
        description="Send the daily activity digest (independent of push).",
    )
    daily_digest_time: str = Field(
        default="08:00",
        pattern=r"^\d{2}:\d{2}$",
        description="HH:MM (local agent time) for the daily digest.",
    )
    digest_period_hours: float = Field(
        default=24.0,
        gt=0.0,
        description="Window covered by each digest (default = one day).",
    )


# ── OpenBrain ───────────────────────────────────────────────────────────────


EmbeddingProviderName = Literal["ollama", "stub", "stub-semantic"]


class OpenBrainSettings(BaseModel):
    """Semantic memory layer."""

    model_config = ConfigDict(extra="forbid")

    embedding_provider: EmbeddingProviderName = Field(
        default="ollama",
        description="'ollama' for production, 'stub' / 'stub-semantic' for tests/offline.",
    )
    embedding_model: str = Field(
        default="nomic-embed-text",
        description="Model name passed to the provider.",
    )
    ollama_base_url: str = Field(
        default="http://localhost:11434",
        description="Only used when embedding_provider='ollama'.",
    )
    search_default_threshold: float = Field(
        default=0.0,
        ge=-1.0,
        le=1.0,
        description="Default cosine-similarity floor for openbrain.search().",
    )
    search_default_limit: int = Field(
        default=5,
        ge=1,
        description="Default top-K for openbrain.search().",
    )


# ── Mesh ────────────────────────────────────────────────────────────────────


MeshTransport = Literal["in_process", "http"]


class MeshSettings(BaseModel):
    """Cross-agent communication."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False, description="Master switch for the mesh layer.")
    transport: MeshTransport = Field(
        default="in_process",
        description="'http' for cross-machine; 'in_process' for tests + single-host.",
    )
    listen_addr: str | None = Field(
        default=None,
        description="HTTP transport: bind address (e.g. '0.0.0.0:8765'). None = no inbound.",
    )
    peer_registry_path: str | None = Field(
        default=None,
        description="Path to the YAML registry of trusted peers.",
    )


# ── Quality ─────────────────────────────────────────────────────────────────


class QualitySettings(BaseModel):
    """Two-tier quality auditor."""

    model_config = ConfigDict(extra="forbid")

    auditor_enabled: bool = Field(default=True, description="Enable the quality auditor.")
    audit_sample_rate: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Fraction of skill outputs to audit. 1.0 = audit everything.",
    )
    pass_threshold: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description="Score at or above this counts as a pass.",
    )
    fail_threshold: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Score at or below this counts as a fail (between fail/pass = warn).",
    )
    last_n_window: int = Field(
        default=10,
        ge=1,
        description="Rolling window size for the `last_n_avg` audit stat.",
    )
    low_confidence_audit_threshold: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Always audit outputs below this confidence regardless of sample rate.",
    )


# ── Storage ─────────────────────────────────────────────────────────────────


StorageBackend = Literal["sqlite", "postgres"]


class StorageSettings(BaseModel):
    """Database + vault locations."""

    model_config = ConfigDict(extra="forbid")

    backend: StorageBackend = Field(
        default="sqlite",
        description="'sqlite' for personal (dCoS); 'postgres' for team (iKB).",
    )
    url: str = Field(
        default="sqlite:///./agent.db",
        description=(
            "SQLAlchemy URL. SQLite: 'sqlite:///<path>'. "
            "Postgres: 'postgresql+psycopg://user:pass@host/db'."
        ),
    )
    vault_path: str | None = Field(
        default=None,
        description="Optional: path to the Obsidian-style vault for round-tripping.",
    )


# ── Work (pipeline / inbound) ───────────────────────────────────────────────


class WorkSettings(BaseModel):
    """Pipeline monitor + inbound capture tunables."""

    model_config = ConfigDict(extra="forbid")

    pipeline_in_progress_threshold_hours: float = Field(
        default=24.0,
        gt=0.0,
        description="An obligation in 'in_progress' longer than this is flagged as stalled.",
    )
    pipeline_waiting_threshold_hours: float = Field(
        default=168.0,
        gt=0.0,
        description="An obligation in 'waiting' longer than this is flagged as stalled.",
    )
    pipeline_critical_age_days: int = Field(
        default=14,
        ge=1,
        description="Stalled items older than this escalate to critical urgency.",
    )


# ── Runtime (agent loop limits) ─────────────────────────────────────────────


class RuntimeSettings(BaseModel):
    """Agent loop safety caps. Mostly leave alone — touch when scaling up."""

    model_config = ConfigDict(extra="forbid")

    max_obligations_per_tick: int = Field(
        default=5,
        ge=1,
        description="Max obligations the agent loop pulls per tick (keeps batches reviewable).",
    )
    max_ticks_safety_cap: int = Field(
        default=100,
        ge=1,
        description="Hard cap on agent.run() iterations — runaway-loop circuit breaker.",
    )


# ── LLM (skills + plan + step + audit) ─────────────────────────────────────


LLMProvider = Literal["stub", "openai_compat", "ollama"]


class LLMSettings(BaseModel):
    """Where the agent's intelligence comes from.

    Two providers ship today:
      * **openai_compat** — any OpenAI-compatible chat-completions endpoint
        (OpenAI, OpenRouter, DeepSeek, Mistral, Together, Groq, Fireworks,
        local llama.cpp servers, Anthropic via OpenRouter). The default
        choice for production.
      * **ollama** — local Ollama instance. Same wire format as openai_compat
        but with Ollama-friendly defaults (base_url, no api_key, smaller
        models).
      * **stub** — deterministic canned responses. Default until the user
        configures a real provider; what tests use.

    Real API keys live in the secrets store, not here. ``api_key_secret_key``
    is the secrets-store *key name* under namespace ``llm``. So::

        llm:
          provider: openai_compat
          base_url: https://api.openai.com/v1
          model: gpt-4o-mini
          api_key_secret_key: openai_api_key

    means "look up agent_core.secrets[``llm``][``openai_api_key``] for the
    actual ``Authorization: Bearer …`` value". Set it via::

        agent settings llm api-key set <provider>      # interactive prompt

    or env var ``AGENTCORE_LLM_OPENAI_API_KEY=sk-...``.
    """

    model_config = ConfigDict(extra="forbid")

    provider: LLMProvider = Field(
        default="stub",
        description="Which LanguageModel implementation to wire into SkillContext.",
    )
    base_url: str = Field(
        default="https://api.openai.com/v1",
        description=(
            "OpenAI-compatible endpoint. Common values: "
            "https://api.openai.com/v1, "
            "https://openrouter.ai/api/v1, "
            "https://api.deepseek.com/v1, "
            "http://localhost:11434/v1 (Ollama)."
        ),
    )
    model: str = Field(
        default="gpt-4o-mini",
        description="Model name passed to /chat/completions.",
    )
    api_key_secret_key: str = Field(
        default="openai_api_key",
        description=(
            "Key under secrets namespace 'llm' that holds the bearer token. "
            "Leave default for OpenAI/OpenRouter; switch to 'deepseek_api_key' "
            "etc. when configuring multiple providers."
        ),
    )
    max_tokens: int = Field(
        default=2048,
        ge=1,
        description="Default ceiling for model output tokens.",
    )
    temperature: float = Field(
        default=0.0,
        ge=0.0,
        le=2.0,
        description="Default sampling temperature.",
    )
    timeout_seconds: float = Field(
        default=60.0,
        gt=0.0,
        description="HTTP timeout for LLM calls.",
    )


# ── Email (inbound IMAP) ────────────────────────────────────────────────────


class EmailIMAPSettings(BaseModel):
    """IMAP fetch configuration. Used by the email-fetch path that turns
    inbound mail into obligations the triage step then classifies.

    Auth: a password/app-password held in the secrets store under namespace
    ``email`` keyed by ``password_secret_key`` (default ``imap_password``).
    Set it via ``dcos secrets set email.imap_password=<value>`` or env var
    ``AGENTCORE_EMAIL_IMAP_PASSWORD=<value>``.

    Gmail users: enable 2FA, generate an "app password" at
    https://myaccount.google.com/apppasswords, and use that as the password
    (Gmail blocks plain-password IMAP login). Host=imap.gmail.com,
    port=993, ssl=true.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description="Master switch. While False, no fetch ever runs.",
    )
    host: str = Field(
        default="",
        description="IMAP server hostname (e.g., imap.gmail.com, imap.fastmail.com).",
    )
    port: int = Field(
        default=993,
        ge=1,
        le=65535,
        description="IMAP port. 993 for SSL, 143 for STARTTLS / plaintext.",
    )
    ssl: bool = Field(
        default=True,
        description="Use IMAPS (SSL) on connect. Set False for STARTTLS / plaintext (rare).",
    )
    username: str = Field(
        default="",
        description="IMAP username, usually the full email address.",
    )
    password_secret_key: str = Field(
        default="imap_password",
        description=(
            "Key under secrets namespace 'email' that holds the IMAP "
            "password / app-password. Look up the value, don't store it here."
        ),
    )
    folder: str = Field(
        default="INBOX",
        description="IMAP folder to fetch from. Most users want INBOX.",
    )
    mark_read: bool = Field(
        default=False,
        description=(
            "Mark fetched messages as \\Seen on the server. Default False "
            "so the user can still see new mail in their phone client; "
            "set True if the agent should be the source of truth."
        ),
    )
    fetch_limit: int = Field(
        default=50,
        ge=1,
        description="Max messages to fetch per call (back-pressure).",
    )
    timeout_seconds: float = Field(
        default=30.0,
        gt=0.0,
        description="Per-IMAP-operation timeout.",
    )


class EmailSMTPSettings(BaseModel):
    """SMTP send configuration. Used to ship drafted replies back out
    after the user approves them.

    Auth: a password/app-password held in the secrets store under namespace
    ``email`` keyed by ``password_secret_key`` (default ``smtp_password``).
    Set it via ``dcos secrets set email.smtp_password=<value>``.

    Gmail users: the same app password works for SMTP. Host=smtp.gmail.com,
    port=587, starttls=true (or port=465, ssl=true). Set
    ``from_address`` to your Gmail address.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description="Master switch. While False, no send ever happens.",
    )
    host: str = Field(
        default="",
        description="SMTP server hostname (e.g., smtp.gmail.com).",
    )
    port: int = Field(
        default=587,
        ge=1,
        le=65535,
        description="SMTP port. 587 for STARTTLS, 465 for SMTPS, 25 for plaintext (rare).",
    )
    ssl: bool = Field(
        default=False,
        description="Use SMTPS (SSL) on connect (port 465). Mutually exclusive with starttls.",
    )
    starttls: bool = Field(
        default=True,
        description="Upgrade to TLS via STARTTLS after connect (port 587). Most common.",
    )
    username: str = Field(
        default="",
        description="SMTP username, usually the same as your IMAP username.",
    )
    password_secret_key: str = Field(
        default="smtp_password",
        description=(
            "Key under secrets namespace 'email' that holds the SMTP "
            "password / app-password. For Gmail, the same app password "
            "works for both IMAP and SMTP — feel free to point this at "
            "'imap_password'."
        ),
    )
    from_address: str = Field(
        default="",
        description="Address that appears in the From: header (usually your IMAP username).",
    )
    from_name: str = Field(
        default="",
        description="Optional display name (e.g., 'AJ Crabill'). Empty = bare address.",
    )
    timeout_seconds: float = Field(
        default=30.0,
        gt=0.0,
        description="Per-SMTP-operation timeout.",
    )


class EmailSettings(BaseModel):
    """Email integration root: IMAP inbound + SMTP outbound."""

    model_config = ConfigDict(extra="forbid")

    imap: EmailIMAPSettings = Field(default_factory=EmailIMAPSettings)
    smtp: EmailSMTPSettings = Field(default_factory=EmailSMTPSettings)
    auto_compose: bool = Field(
        default=False,
        description=(
            "When True, the autonomous tick runs email-composer on every "
            "triaged-as-draft email obligation. Drafts wait for approval — "
            "sending always requires explicit user action (CLI or chat). "
            "Default False so the agent doesn't burn LLM tokens drafting "
            "replies the user might never want."
        ),
    )


# ── Root ────────────────────────────────────────────────────────────────────


class AgentSettings(BaseModel):
    """Top-level container. Composed of all section models above.

    Use ``SettingsManager`` to load/save this — don't instantiate directly
    unless you know you want defaults-only.
    """

    model_config = ConfigDict(extra="forbid")

    autonomy: AutonomySettings = Field(default_factory=AutonomySettings)
    learning: LearningSettings = Field(default_factory=LearningSettings)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    openbrain: OpenBrainSettings = Field(default_factory=OpenBrainSettings)
    mesh: MeshSettings = Field(default_factory=MeshSettings)
    quality: QualitySettings = Field(default_factory=QualitySettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    work: WorkSettings = Field(default_factory=WorkSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    email: EmailSettings = Field(default_factory=EmailSettings)


__all__ = [
    "AgentSettings",
    "AutonomySettings",
    "DetectorStrictness",
    "EmbeddingProviderName",
    "LLMProvider",
    "LLMSettings",
    "LearningSettings",
    "MeshSettings",
    "MeshTransport",
    "NotificationSettings",
    "NotificationTransport",
    "OpenBrainSettings",
    "PolicyKindLiteral",
    "PresetName",
    "QualitySettings",
    "RuntimeSettings",
    "StorageBackend",
    "StorageSettings",
    "UrgencyLevel",
    "WorkSettings",
]
