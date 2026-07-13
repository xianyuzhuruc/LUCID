#!/bin/bash
# LUCID launcher. Hub uses a project venv; deployed agents can use a bundled runtime.
set -e
cd "$(dirname "$0")"

echo $$ > "$PWD/.lucid-pid"

RUNTIME_DIR="${LUCID_RUNTIME_DIR:-$PWD/.lucid-runtime}"
OLD_RUNTIME_DIR="$PWD/.fleet-runtime"
if [ ! -e "$RUNTIME_DIR" ] && [ -d "$OLD_RUNTIME_DIR" ]; then
    if ! mv "$OLD_RUNTIME_DIR" "$RUNTIME_DIR"; then
        echo "[LUCID] failed to migrate ${OLD_RUNTIME_DIR} to ${RUNTIME_DIR}" >&2
        exit 1
    fi
fi
export LUCID_RUNTIME_DIR="$RUNTIME_DIR"
export PYTHONUTF8="${PYTHONUTF8:-1}"
export PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"
MODE="${LUCID_MODE:-hub}"
if [ -n "${LUCID_RELOAD+x}" ]; then
    RELOAD="$LUCID_RELOAD"
elif [ "${MODE}" = "agent" ]; then
    RELOAD=0
else
    RELOAD=1
fi

if [ "${MODE}" = "agent" ]; then
    export PATH="${RUNTIME_DIR}/env/bin:$PATH"
    if [ "${LUCID_SKIP_SYSTEM_DEPS:-0}" != "1" ]; then
        sh scripts/install-system-deps.sh agent
    fi
fi

PYTHON_CMD="${LUCID_PYTHON:-python3}"
read -r -a PYTHON_BIN <<< "$PYTHON_CMD"
RUNTIME_IMPORT_CHECK="import fastapi, uvicorn"
if [ "${RELOAD}" = "1" ]; then
    RUNTIME_IMPORT_CHECK="${RUNTIME_IMPORT_CHECK}, watchfiles"
fi

if [ "${LUCID_NO_VENV:-0}" = "1" ]; then
    if ! "${PYTHON_BIN[@]}" -c "${RUNTIME_IMPORT_CHECK}" 2>/dev/null; then
        echo "[LUCID] LUCID_NO_VENV=1 but runtime dependencies are missing" >&2
        exit 1
    fi
else
    if [ ! -d .venv ]; then
        echo "[LUCID] creating venv..."
        "${PYTHON_BIN[@]}" -m venv .venv
    fi

    export VIRTUAL_ENV="$PWD/.venv"
    export PATH="$VIRTUAL_ENV/bin:$PATH"
    hash -r 2>/dev/null || true

    if ! python -c "${RUNTIME_IMPORT_CHECK}; import importlib.metadata as metadata; metadata.version('LUCID')" 2>/dev/null; then
        echo "[LUCID] installing deps..."
        python -m pip install -q --upgrade pip
        python -m pip install -q -e .
    fi
fi

PORT="${LUCID_PORT:-21893}"
HOST="${LUCID_HOST:-${LUCID_AGENT_HOST:-0.0.0.0}}"
if [ "${LUCID_NO_VENV:-0}" = "1" ]; then
    "${PYTHON_BIN[@]}" scripts/check_port.py --host "$HOST" --port "$PORT"
else
    python scripts/check_port.py --host "$HOST" --port "$PORT"
fi

echo "[LUCID] mode=${MODE} reload=${RELOAD} starting on http://${HOST}:${PORT}"

UVICORN_RELOAD_ARGS=()
if [ "${RELOAD}" = "1" ]; then
    UVICORN_RELOAD_ARGS=(
        --reload
        --reload-exclude ".venv"
        --reload-exclude ".venv/*"
        --reload-exclude ".lucid-runtime"
        --reload-exclude ".lucid-runtime/*"
        --reload-exclude ".fleet-runtime"
        --reload-exclude ".fleet-runtime/*"
        --reload-exclude ".git"
        --reload-exclude ".git/*"
        --reload-exclude "__pycache__"
        --reload-exclude "__pycache__/*"
    )
fi

if [ "${MODE}" = "agent" ]; then
    if [ "${LUCID_NO_VENV:-0}" = "1" ]; then
        exec "${PYTHON_BIN[@]}" -m uvicorn app:app --host "$HOST" --port "$PORT" --no-use-colors
    fi
    exec python -m uvicorn app:app --host "$HOST" --port "$PORT" --no-use-colors
fi

if [ "${LUCID_NO_VENV:-0}" = "1" ]; then
    exec "${PYTHON_BIN[@]}" -m uvicorn app:app --host "$HOST" --port "$PORT" "${UVICORN_RELOAD_ARGS[@]}" --no-use-colors
fi

exec python -m uvicorn app:app --host "$HOST" --port "$PORT" "${UVICORN_RELOAD_ARGS[@]}" --no-use-colors
