"""Pre-seeded learning rule packs.

Per L13: pre-seeded rule packs are derived from existing learnings and easily
toggleable on/off during install. Each pack is a YAML file in this directory.

A pack is just a thin wrapper over LearningStore.add() — there's no special
machinery. ``load_pack(name, store)`` reads the YAML and adds each rule with
``source='pre-seed:<pack-name>'`` so users can later identify (and bulk-
remove) seeded rules.

Bundled packs:
  - professional.yaml   — neutral writing/communication defaults

Custom packs: drop a YAML file in this directory; it'll be discoverable via
``list_packs()``. Users can also point ``load_pack()`` at an arbitrary path.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from agent_core.learning.store import LearningStore
from agent_core.state.models import LearningRule


def list_packs() -> list[str]:
    """Names of bundled packs (without .yaml extension)."""
    pkg_dir = Path(__file__).parent
    return sorted(p.stem for p in pkg_dir.glob("*.yaml"))


def _resolve_pack_path(name_or_path: str) -> Path:
    """Resolve ``name_or_path`` to a YAML file:
    - if it ends in .yaml or .yml, treat as a path
    - otherwise look for ``<name>.yaml`` in this package's seed_packs dir
    """
    candidate = Path(name_or_path)
    if candidate.suffix in (".yaml", ".yml"):
        return candidate.expanduser().resolve()
    pkg_dir = Path(__file__).parent
    bundled = pkg_dir / f"{name_or_path}.yaml"
    if not bundled.exists():
        raise FileNotFoundError(
            f"seed pack {name_or_path!r} not found (looked in {pkg_dir} and as a literal path)"
        )
    return bundled


def load_pack(
    name_or_path: str,
    store: LearningStore,
    *,
    source_marker: str | None = None,
) -> list[LearningRule]:
    """Load a YAML pack into the store. Returns the rules added.

    Each rule's `source` is set to ``pre-seed:<name_or_path stem>`` (or
    ``source_marker`` if provided) so seeded rules are easy to identify and
    bulk-remove later.

    Pack YAML schema:
      name: <pack name, e.g. "Professional defaults">
      description: <one-line description>
      rules:
        - correction: <rule text>
          skill_tags: [<tag>, ...]   # default: ['general']
          context: <optional context>
        - ...
    """
    path = _resolve_pack_path(name_or_path)
    with open(path, encoding="utf-8") as f:
        pack = yaml.safe_load(f) or {}

    rules_data = pack.get("rules", []) or []
    if not isinstance(rules_data, list):
        raise ValueError(f"pack {path}: 'rules' must be a list")

    marker = source_marker or f"pre-seed:{path.stem}"
    added: list[LearningRule] = []
    for entry in rules_data:
        if not isinstance(entry, dict):
            continue
        correction = entry.get("correction")
        if not correction:
            continue
        rule = store.add(
            correction=correction,
            skill_tags=entry.get("skill_tags") or ["general"],
            source=marker,
            context=entry.get("context", "") or "",
            notes=entry.get("notes", "") or "",
        )
        added.append(rule)
    return added


def pack_metadata(name_or_path: str) -> dict:
    """Read just the top-level pack metadata (name, description, rule count)
    without inserting anything. Useful for the wizard's pack picker."""
    path = _resolve_pack_path(name_or_path)
    with open(path, encoding="utf-8") as f:
        pack = yaml.safe_load(f) or {}
    return {
        "name": pack.get("name", path.stem),
        "description": pack.get("description", ""),
        "rule_count": len(pack.get("rules", []) or []),
        "path": str(path),
    }


__all__ = [
    "list_packs",
    "load_pack",
    "pack_metadata",
]
