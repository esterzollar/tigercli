#!/usr/bin/env bash
#
# TigerLiteCode — smart setup script for Linux & macOS.
#
# Verifies the toolchain, installs the Python engine, and builds the
# terminal UI. Re-runnable and safe: it skips steps that are already done
# unless you pass --force.
#
# Usage:
#   ./build.sh            # full setup
#   ./build.sh --force    # rebuild everything from scratch
#   ./build.sh --help
#
set -euo pipefail

# --- pretty output ---------------------------------------------------------
if [ -t 1 ]; then
  BOLD="$(printf '\033[1m')"; DIM="$(printf '\033[2m')"; RESET="$(printf '\033[0m')"
  ORANGE="$(printf '\033[38;5;208m')"; RED="$(printf '\033[31m')"; GREEN="$(printf '\033[32m')"
else
  BOLD=""; DIM=""; RESET=""; ORANGE=""; RED=""; GREEN=""
fi
info()  { printf "%s==>%s %s\n" "$ORANGE$BOLD" "$RESET" "$*"; }
ok()    { printf "%s  ok%s %s\n" "$GREEN" "$RESET" "$*"; }
warn()  { printf "%s warn%s %s\n" "$ORANGE" "$RESET" "$*"; }
die()   { printf "%serror%s %s\n" "$RED$BOLD" "$RESET" "$*" >&2; exit 1; }

FORCE=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    -h|--help)
      sed -n '3,16p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) die "unknown option: $arg (try --help)" ;;
  esac
done

cd "$(dirname "$0")"

printf "%s🐅 TigerLiteCode setup%s\n\n" "$ORANGE$BOLD" "$RESET"

# --- detect Python 3.11+ ---------------------------------------------------
PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,11) else 1)' 2>/dev/null; then
      PY="$cand"; break
    fi
  fi
done
[ -n "$PY" ] || die "Python 3.11+ is required but was not found. Install it from https://www.python.org/downloads/"
ok "Python: $("$PY" --version 2>&1)"

# --- detect Node 18+ -------------------------------------------------------
command -v node >/dev/null 2>&1 || die "Node.js 18+ is required but was not found. Install it from https://nodejs.org/"
NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
[ "$NODE_MAJOR" -ge 18 ] || die "Node.js 18+ is required (found $(node --version))."
ok "Node.js: $(node --version)"

command -v npm >/dev/null 2>&1 || die "npm is required but was not found (it ships with Node.js)."
ok "npm: $(npm --version)"

echo

# --- install the Python engine ---------------------------------------------
info "Installing the Python engine (pip install -e .)"
"$PY" -m pip install -e . || die "pip install failed. See the output above."
ok "Engine installed."

echo

# --- build the terminal UI -------------------------------------------------
if [ ! -d tui-ts ]; then
  die "tui-ts/ directory not found — are you in the repo root?"
fi

if [ "$FORCE" -eq 1 ] || [ ! -d tui-ts/node_modules ]; then
  info "Installing UI dependencies (npm install)"
  ( cd tui-ts && npm install ) || die "npm install failed."
else
  ok "UI dependencies already installed (use --force to reinstall)."
fi

info "Building the terminal UI (npm run build)"
( cd tui-ts && npm run build ) || die "UI build failed."
ok "Terminal UI built."

echo
printf "%s✓ Done!%s TigerLiteCode is ready.\n\n" "$GREEN$BOLD" "$RESET"
printf "Next:\n"
printf "  1. Set an API key, e.g. %sexport DEEPSEEK_API_TIGER_KEY=\"sk-...\"%s\n" "$DIM" "$RESET"
printf "  2. Run %stigerlitecode%s\n" "$BOLD" "$RESET"
