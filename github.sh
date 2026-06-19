#!/usr/bin/env bash
#
# TigerLiteCode — GitHub upload helper.
#
# Initializes git (if needed), commits the repo, and pushes it to
#   https://github.com/esterzollar/tigercli
#
# Authentication: this script asks for a GitHub Personal Access Token (PAT)
# the first time it runs and saves it to a LOCAL, GIT-IGNORED file
# (.github_token) so you don't have to retype it. The token is NEVER
# committed and never printed.
#
# Usage:
#   ./github.sh                       # commit + push (prompts for token once)
#   ./github.sh -m "your message"     # custom commit message
#   ./github.sh --reset-token         # forget the saved token and ask again
#
set -euo pipefail

# --- config ----------------------------------------------------------------
GH_USER="esterzollar"
GH_REPO="tigercli"
GH_BRANCH="main"
TOKEN_FILE=".github_token"

# --- pretty output ---------------------------------------------------------
if [ -t 1 ]; then
  BOLD="$(printf '\033[1m')"; RESET="$(printf '\033[0m')"
  ORANGE="$(printf '\033[38;5;208m')"; RED="$(printf '\033[31m')"; GREEN="$(printf '\033[32m')"
else
  BOLD=""; RESET=""; ORANGE=""; RED=""; GREEN=""
fi
info() { printf "%s==>%s %s\n" "$ORANGE$BOLD" "$RESET" "$*"; }
ok()   { printf "%s  ok%s %s\n" "$GREEN" "$RESET" "$*"; }
die()  { printf "%serror%s %s\n" "$RED$BOLD" "$RESET" "$*" >&2; exit 1; }

cd "$(dirname "$0")"

COMMIT_MSG="Upload TigerLiteCode"
while [ $# -gt 0 ]; do
  case "$1" in
    -m|--message) COMMIT_MSG="${2:-}"; shift 2 ;;
    --reset-token) rm -f "$TOKEN_FILE"; ok "Saved token cleared."; shift ;;
    -h|--help) sed -n '3,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done

command -v git >/dev/null 2>&1 || die "git is not installed."

printf "%s🐅 Upload to github.com/%s/%s%s\n\n" "$ORANGE$BOLD" "$GH_USER" "$GH_REPO" "$RESET"

# --- make sure the token file can never be committed -----------------------
if [ ! -f .gitignore ] || ! grep -qxF "$TOKEN_FILE" .gitignore 2>/dev/null; then
  printf "\n# Local GitHub token used by github.sh (never commit)\n%s\n" "$TOKEN_FILE" >> .gitignore
  ok "Added $TOKEN_FILE to .gitignore"
fi

# --- get the token (from file, or ask) -------------------------------------
if [ -f "$TOKEN_FILE" ]; then
  GH_TOKEN="$(cat "$TOKEN_FILE")"
  ok "Using saved token from $TOKEN_FILE"
else
  info "A GitHub Personal Access Token is required (scope: repo)."
  printf "    Create one at: https://github.com/settings/tokens\n"
  printf "    Token (input hidden): "
  read -rs GH_TOKEN
  printf "\n"
  [ -n "$GH_TOKEN" ] || die "No token entered."
  ( umask 077; printf "%s" "$GH_TOKEN" > "$TOKEN_FILE" )
  ok "Token saved to $TOKEN_FILE (git-ignored, permissions 600)."
fi
[ -n "$GH_TOKEN" ] || die "Empty token."

# --- init repo if needed ---------------------------------------------------
if [ ! -d .git ]; then
  info "Initializing git repository"
  git init -q
  git branch -M "$GH_BRANCH" 2>/dev/null || true
fi

# --- stage and commit ------------------------------------------------------
info "Staging files"
git add -A

if git diff --cached --quiet; then
  ok "Nothing new to commit."
else
  git commit -q -m "$COMMIT_MSG"
  ok "Committed: $COMMIT_MSG"
fi

# --- configure the authenticated remote (token kept out of git config) -----
# We pass the token only on the push command line, not in a stored remote URL.
REMOTE_PLAIN="https://github.com/${GH_USER}/${GH_REPO}.git"
REMOTE_AUTH="https://${GH_USER}:${GH_TOKEN}@github.com/${GH_USER}/${GH_REPO}.git"

if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$REMOTE_PLAIN"
else
  git remote add origin "$REMOTE_PLAIN"
fi
ok "Remote 'origin' -> $REMOTE_PLAIN"

# --- push (token used inline, never persisted) -----------------------------
info "Pushing to $GH_BRANCH"
if git push "$REMOTE_AUTH" "HEAD:${GH_BRANCH}" >/dev/null 2>&1; then
  ok "Pushed successfully."
else
  die "Push failed. Check the token scope (needs 'repo') and that the repo exists at $REMOTE_PLAIN"
fi

printf "\n%s✓ Done!%s View it at https://github.com/%s/%s\n" "$GREEN$BOLD" "$RESET" "$GH_USER" "$GH_REPO"
