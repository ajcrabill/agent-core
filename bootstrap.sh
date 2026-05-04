#!/usr/bin/env bash
# bootstrap.sh — one-shot installer for agent-core (dcos-agent / ikb-agent).
#
# Idempotent. Detects what's missing, installs only what's needed,
# walks the user through LLM provider choice, and lands them in a chat
# REPL. Designed for the cases:
#
#   1. Fresh macOS user with nothing installed except a shell
#   2. Fresh Linux VPS (Ubuntu/Debian/RHEL) with nothing installed
#   3. Existing user with uv/git/etc. already in place
#
# Run after `git clone`:
#
#     git clone https://github.com/ajcrabill/agent-core.git
#     cd agent-core
#     ./bootstrap.sh
#
# Flags:
#     --product dcos|ikb         (default: dcos)
#     --llm-provider stub|openai_compat|ollama (interactive prompt if omitted)
#     --no-chat                  Don't drop into chat at the end
#     --force-reinstall          Re-run uv sync even if .venv exists
#
# Exit codes:
#     0  success — agent installed and (optionally) chat REPL ran
#     1  prereq missing the script can't auto-install (e.g., no Python 3.11+)
#     2  user aborted at a prompt
#     3  install step failed mid-flight; details printed

set -euo pipefail

# ── pretty output ──────────────────────────────────────────────────────────

if [ -t 1 ]; then
    BOLD=$'\033[1m'
    DIM=$'\033[2m'
    RED=$'\033[31m'
    GREEN=$'\033[32m'
    YELLOW=$'\033[33m'
    CYAN=$'\033[36m'
    RESET=$'\033[0m'
else
    BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; CYAN=""; RESET=""
fi

say()  { printf "%s\n" "${BOLD}$*${RESET}"; }
note() { printf "%s\n" "${DIM}$*${RESET}"; }
ok()   { printf "%s ✓ %s\n" "${GREEN}" "${RESET}$*"; }
warn() { printf "%s !  %s\n" "${YELLOW}" "${RESET}$*" >&2; }
fail() { printf "%s ✗  %s\n" "${RED}" "${RESET}$*" >&2; exit 3; }

# ── argument parsing ──────────────────────────────────────────────────────

PRODUCT="dcos"
LLM_PROVIDER=""
RUN_CHAT="yes"
FORCE_REINSTALL=""

while [ $# -gt 0 ]; do
    case "$1" in
        --product)
            PRODUCT="$2"; shift 2 ;;
        --llm-provider)
            LLM_PROVIDER="$2"; shift 2 ;;
        --no-chat)
            RUN_CHAT="no"; shift ;;
        --force-reinstall)
            FORCE_REINSTALL="yes"; shift ;;
        -h|--help)
            sed -n '/^# bootstrap.sh/,/^set -euo/p' "$0" | sed 's/^# \?//; /^set -euo/d'
            exit 0 ;;
        *)
            fail "unknown flag: $1 (use --help)" ;;
    esac
done

case "$PRODUCT" in
    dcos|ikb) ;;
    *) fail "--product must be 'dcos' or 'ikb' (got '$PRODUCT')" ;;
esac

# ── prereq detection + install ────────────────────────────────────────────

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_ROOT"

if [ ! -f "pyproject.toml" ]; then
    fail "no pyproject.toml here ($REPO_ROOT). run from inside the cloned agent-core repo."
fi

say "agent-core bootstrap"
note "product=${PRODUCT}  repo=${REPO_ROOT}"
echo

# uv — installed first because it can manage Python itself.
if ! command -v uv >/dev/null 2>&1; then
    if [ -x "$HOME/.local/bin/uv" ]; then
        export PATH="$HOME/.local/bin:$PATH"
        ok "found uv at \$HOME/.local/bin/uv (added to PATH for this run)"
    elif [ -x "/opt/homebrew/bin/uv" ]; then
        # Apple Silicon brew prefix isn't always on the SSH non-login PATH.
        export PATH="/opt/homebrew/bin:$PATH"
        ok "found uv at /opt/homebrew/bin/uv (added to PATH for this run)"
    else
        say "installing uv (one-time)…"
        if ! curl -LsSf https://astral.sh/uv/install.sh | sh; then
            fail "uv install failed. install manually: https://docs.astral.sh/uv/"
        fi
        export PATH="$HOME/.local/bin:$PATH"
        ok "uv installed"
    fi
else
    ok "uv $(uv --version 2>&1 | awk '{print $2}')"
fi

# Python 3.11+ — soft check. uv manages its own Python automatically:
# if the system python3 is too old (or missing), `uv sync` downloads a
# compatible interpreter into ~/.local/share/uv/python/. So we just note
# the situation rather than failing.
PYTHON_VERSION="$(python3 --version 2>&1 | awk '{print $2}')" || PYTHON_VERSION=""
if [ -z "$PYTHON_VERSION" ]; then
    note "python3 not on PATH — uv will download a managed Python interpreter"
else
    PY_MAJOR="$(echo "$PYTHON_VERSION" | cut -d. -f1)"
    PY_MINOR="$(echo "$PYTHON_VERSION" | cut -d. -f2)"
    if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
        note "system python ${PYTHON_VERSION} is too old (need 3.11+); uv will download a managed Python"
    else
        ok "python ${PYTHON_VERSION} (system)"
    fi
fi

# git submodule (OpenWebUI fork) — needed for `git submodule update`
if [ -f .gitmodules ] && [ ! -d packages/open-webui-fork/.git ]; then
    note "fetching git submodules (OpenWebUI fork; optional but cheap)…"
    git submodule update --init --recursive 2>&1 | tail -5 || warn "submodule fetch failed; continuing"
fi
ok "submodules"

# ── install (uv sync) ─────────────────────────────────────────────────────

if [ -d ".venv" ] && [ -z "$FORCE_REINSTALL" ]; then
    note "venv already exists; skipping uv sync (use --force-reinstall to redo)"
else
    say "installing python dependencies…"
    uv sync || fail "uv sync failed"
fi
ok "venv ready (.venv/)"

# ── LLM provider selection ────────────────────────────────────────────────

if [ -z "$LLM_PROVIDER" ]; then
    echo
    say "configure your LLM"
    note "the agent needs a model to talk to. choose:"
    echo
    echo "  1) ${CYAN}openai${RESET}   — OpenAI (gpt-4o-mini default; needs API key)"
    echo "  2) ${CYAN}ollama${RESET}   — local Ollama (free, private, slower)"
    echo "  3) ${CYAN}stub${RESET}     — canned responses (no LLM; for testing)"
    echo
    while true; do
        read -r -p "choice [1-3]: " choice
        case "$choice" in
            1) LLM_PROVIDER="openai_compat"; break ;;
            2) LLM_PROVIDER="ollama"; break ;;
            3) LLM_PROVIDER="stub"; break ;;
            "") continue ;;
            *) warn "enter 1, 2, or 3" ;;
        esac
    done
fi

LLM_API_KEY=""
case "$LLM_PROVIDER" in
    openai_compat)
        # Try environment first
        LLM_API_KEY="${OPENAI_API_KEY:-}"
        if [ -z "$LLM_API_KEY" ]; then
            echo
            note "paste your OpenAI API key (or any OpenAI-compatible provider's). starts with 'sk-'."
            note "key is stored in your OS keychain (or a 0600 file on Linux)."
            read -rs -p "api key: " LLM_API_KEY
            echo
            if [ -z "$LLM_API_KEY" ]; then
                warn "no key provided; you can re-run init with --llm-api-key later"
            fi
        else
            ok "using OPENAI_API_KEY from environment"
        fi
        ;;
    ollama)
        if ! curl -s -o /dev/null -m 2 http://localhost:11434/api/tags; then
            warn "ollama not reachable at http://localhost:11434."
            note "  install: ${CYAN}brew install ollama${RESET} (mac) or ${CYAN}curl -fsSL https://ollama.com/install.sh | sh${RESET} (linux)"
            note "  pull a model: ${CYAN}ollama pull llama3.2${RESET}"
            note "  (continuing — agent will fail to chat until ollama is up)"
        else
            ok "ollama reachable"
        fi
        ;;
    stub)
        note "using stub LLM — agent returns canned responses, no real intelligence"
        ;;
esac

# ── run setup + init ──────────────────────────────────────────────────────

CMD="uv run ${PRODUCT}"

echo
say "running setup wizard (3 questions; defaults fine)…"
echo

# Pipe defaults to handle non-interactive cases gracefully
$CMD setup --tier 1 --no-init <<< $'\n\n\n' || fail "setup failed"

echo
say "initializing schema + token + LLM config…"

INIT_ARGS=("--llm-provider" "$LLM_PROVIDER")
if [ -n "$LLM_API_KEY" ]; then
    INIT_ARGS+=("--llm-api-key" "$LLM_API_KEY")
fi

$CMD init "${INIT_ARGS[@]}" || fail "init failed"

echo
say "running doctor…"
$CMD doctor || warn "doctor reported issues (continuing)"

# ── all done ──────────────────────────────────────────────────────────────

echo
ok "agent-core install complete"
echo
note "next:"
note "  ${CYAN}${CMD} chat${RESET}              — talk to your agent in the terminal"
note "  ${CYAN}${CMD} serve${RESET}             — start the HTTP API + browser chat at http://127.0.0.1:8765/chat"
note "  ${CYAN}${CMD} skills list${RESET}       — see what your agent can do"

if [ "$RUN_CHAT" = "yes" ] && [ "$LLM_PROVIDER" != "stub" -o -n "${FORCE_CHAT:-}" ]; then
    echo
    note "(starting chat now — Ctrl-C to exit)"
    echo
    exec $CMD chat
fi
