"""Alembic migrations bundled with agent-core.

Programmatic usage (preferred):
    from agent_core.state import Database
    db = Database.sqlite()
    db.upgrade()                      # → 'head'

CLI usage (dev convenience):
    cd packages/agent-core && uv run alembic upgrade head
"""
