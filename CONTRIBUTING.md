# Contributing

Thanks for your interest. This project is in pre-release and the API is unstable. Architecture, schema, and skill format will all change before v1.0.0.

## During pre-release

- **Issues welcome.** Bug reports, design feedback, missing-capability requests — open an issue.
- **PRs**: small fixes and docs PRs welcome. Larger architectural changes — please open an issue first to discuss before writing code.
- **Code style**: `ruff` for lint and format. CI runs both.
- **Tests**: `pytest`. PRs that change behavior must include tests.
- **Commit messages**: clear, conventional prefixes welcome (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`).

## Setup

Once Sprint 1 lands:

```bash
git clone https://github.com/ajcrabill/dCoS
cd dCoS
uv sync
uv run pytest
```

## Code of Conduct

Be kind. Disagreements happen — assume good faith. No harassment, slurs, or personal attacks. Maintainers will moderate at their discretion.
