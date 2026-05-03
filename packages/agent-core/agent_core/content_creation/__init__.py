"""agent_core.content_creation — the killer-feature primitives.

Per L15 (locked): supervised learning for document creation + email creation
is the most differentiating feature. The pattern: point at exemplars +
template, iterate from raw input with corrections, eventually deliver
reliably.

Per L21 (locked): post-onboarding, threshold-gated synthetic edge-case
battery generates hard-to-classify training items from accumulated
exemplars to accelerate supervised learning.

This package ships:

  exemplars.py  — ExemplarStore (canonical "good outputs" per skill)
  iterations.py — Iterations (raw → attempts → corrections → final cycle)
  calibration.py — CalibrationManager (per-skill confidence + autonomous-mode gate)
  diff_extractor.py — Protocol for LLM-extracting rules from correction diffs
  synthesis.py — SyntheticBattery (L21 eligibility + generator hookup)
"""

from agent_core.content_creation.calibration import CalibrationManager
from agent_core.content_creation.diff_extractor import (
    DiffExtractor,
    ProposedRule,
)
from agent_core.content_creation.exemplars import ExemplarStore
from agent_core.content_creation.iterations import Iterations
from agent_core.content_creation.synthesis import (
    BatteryEligibility,
    BatteryGenerator,
    SyntheticBattery,
)

__all__ = [
    "BatteryEligibility",
    "BatteryGenerator",
    "CalibrationManager",
    "DiffExtractor",
    "ExemplarStore",
    "Iterations",
    "ProposedRule",
    "SyntheticBattery",
]
