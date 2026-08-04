#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
#  dev_launch.sh — start SReTo from a SOURCE CHECKOUT, without installing it.
#
#      ./scripts/dev_launch.sh              start the GUI
#      ./scripts/dev_launch.sh --check      import + self-tests, no window
#
#  If you have installed the package (`pip install -e .`) you do not need this
#  script — just run `sreto`. It exists for the case this project started in:
#  a conda env ('sdrr') that already has tkinter and the analysis chain, where
#  the fastest path is to put src/ on PYTHONPATH and go.
#
#  The science repository is separate. Set SRETO_REPO_ROOT, or run
#  `sreto --set-repo /path/to/sdr_r` once to persist it.
# ═══════════════════════════════════════════════════════════════════════════

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_NAME="${SRETO_ENV:-${SDRR_ENV:-sdrr}}"

# Prefer the env's interpreter directly — this avoids needing `conda activate`
# inside a non-interactive shell, which requires conda's shell hook.
PY=""
if [ -n "${CONDA_PREFIX:-}" ] && [ "$(basename "$CONDA_PREFIX")" = "$ENV_NAME" ]; then
  PY="$CONDA_PREFIX/bin/python"
elif command -v conda >/dev/null 2>&1; then
  ENV_PATH=$(conda env list | awk -v e="$ENV_NAME" '$1==e {print $NF}' | head -1)
  [ -n "$ENV_PATH" ] && [ -x "$ENV_PATH/bin/python" ] && PY="$ENV_PATH/bin/python"
fi
[ -z "$PY" ] && PY="$(command -v python3 || command -v python || true)"

if [ -z "$PY" ]; then
  echo "ERROR: no python found."
  exit 1
fi

if ! "$PY" -c "import tkinter" >/dev/null 2>&1; then
  echo "ERROR: $PY has no tkinter, and tkinter cannot be pip-installed."
  echo "       macOS (homebrew) : brew install python-tk"
  echo "       Debian/Ubuntu    : sudo apt install python3-tk"
  echo "       conda            : conda install -n $ENV_NAME tk"
  exit 1
fi

export PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

echo "python  : $PY"
echo "project : $PROJECT_DIR"
[ -n "${SRETO_REPO_ROOT:-}" ] && echo "repo    : $SRETO_REPO_ROOT"

cd "$PROJECT_DIR"
exec "$PY" -m sreto "$@"
