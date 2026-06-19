#!/usr/bin/env bash
#
# TigerLiteCode — GitHub upload helper.
#
# Initializes git if needed, commits the repo, and pushes it to:
#   https://github.com/esterzollar/tigercli
#
# Authentication:
#   This script asks for a GitHub Personal Access Token the first time it runs
#   and saves it to a LOCAL, GIT-IGNORED file named .github_token.
#
# Usage:
#   ./github.sh
#   ./github.sh -m "your commit message"
#   ./github.sh --reset-token
#
set -euo pipefail

# --- config ----------------------------------------------------------------
GH_USER="esterzollar"
GH_REPO="tigercli"
GH_BRANCH="main"
TOKEN_FILE=".github_token"

DEFAULT_GIT_NAME="Esterzollar"
DEFAULT_GIT_EMAIL="aungthamardi009@gmail.com"

# --- pretty output ---------------------------------------------------------
if [ -t 1 ]; then
  BOLD="$(printf '\033[1m')"
  RESET="$(printf '\033[0m')"
  ORANGE="$(printf '\033[38;5;208m')"
  RED="$(printf '\033[31m')"
  GREEN="$(printf '\033[32m')"
  YELLOW="$(printf '\033[33m')"
else
  BOLD=""
  RESET=""
  ORANGE=""
  RED=""
  GREEN=""
  YELLOW=""
fi

info() { printf "%s==>%s %s\n" "$ORANGE$BOLD" "$RESET" "$*"; }
ok()   { printf "%s  ok%s %s\n" "$GREEN" "$RESET" "$*"; }
warn() { printf "%s  warn%s %s\n" "$YELLOW" "$RESET" "$*"; }
die()  { printf "%serror%s %s\n" "$RED$BOLD" "$RESET" "$*" >&2; exit 1; }

# Move into the folder where this script lives
cd "$(dirname "$0")"

COMMIT_MSG="Upload TigerLiteCode"

while [ $# -gt 0 ]; do
  case "$1" in
    -m|--message)
      COMMIT_MSG="${2:-}"
      [ -n "$COMMIT_MSG" ] || die "Commit message cannot be empty."
      shift 2
      ;;
    --reset-token)
      rm -f "$TOKEN_FILE"
      ok "Saved token cleared."
      shift
      ;;
    -h|--help)
      sed -n '3,20p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      die "unknown option: $1 (try --help)"
      ;;
  esac
done

command -v git >/dev/null 2>&1 || die "git is not installed."

printf "%s🐅 Upload to github.com/%s/%s%s\n\n" "$ORANGE$BOLD" "$GH_USER" "$GH_REPO" "$RESET"

# --- make sure the token file can never be committed -----------------------
if [ ! -f .gitignore ] || ! grep -qxF "$TOKEN_FILE" .gitignore 2>/dev/null; then
  printf "\n# Local GitHub token used by github.sh. Never commit this file.\n%s\n" "$TOKEN_FILE" >> .gitignore
  ok "Added $TOKEN_FILE to .gitignore"
fi

# --- get the token from file or ask ----------------------------------------
if [ -f "$TOKEN_FILE" ]; then
  GH_TOKEN="$(cat "$TOKEN_FILE")"
  ok "Using saved token from $TOKEN_FILE"
else
  info "A GitHub Personal Access Token is required."
  printf "    Required scope: repo\n"
  printf "    Create one at: https://github.com/settings/tokens\n"
  printf "    Token input is hidden.\n"
  printf "    Token: "

  read -rs GH_TOKEN
  printf "\n"

  [ -n "$GH_TOKEN" ] || die "No token entered."

  # Save token with permission 600
  ( umask 077; printf "%s" "$GH_TOKEN" > "$TOKEN_FILE" )

  ok "Token saved to $TOKEN_FILE."
fi

[ -n "$GH_TOKEN" ] || die "Empty token."

# --- initialize git repo if needed -----------------------------------------
if [ ! -d .git ]; then
  info "Initializing git repository"
  git init -q
  git branch -M "$GH_BRANCH" 2>/dev/null || true
fi

# --- ensure git author identity exists -------------------------------------
ensure_git_identity() {
  CURRENT_NAME="$(git config user.name || true)"
  CURRENT_EMAIL="$(git config user.email || true)"

  if [ -n "$CURRENT_NAME" ] && [ -n "$CURRENT_EMAIL" ]; then
    ok "Git identity: $CURRENT_NAME <$CURRENT_EMAIL>"
    return
  fi

  warn "Git user.name or user.email is not configured."

  if [ -z "$CURRENT_NAME" ]; then
    printf "    Git user.name [%s]: " "$DEFAULT_GIT_NAME"
    read -r INPUT_NAME
    INPUT_NAME="${INPUT_NAME:-$DEFAULT_GIT_NAME}"
    git config user.name "$INPUT_NAME"
    ok "Set repo git user.name to: $INPUT_NAME"
  fi

  if [ -z "$CURRENT_EMAIL" ]; then
    printf "    Git user.email [%s]: " "$DEFAULT_GIT_EMAIL"
    read -r INPUT_EMAIL
    INPUT_EMAIL="${INPUT_EMAIL:-$DEFAULT_GIT_EMAIL}"
    git config user.email "$INPUT_EMAIL"
    ok "Set repo git user.email to: $INPUT_EMAIL"
  fi
}

ensure_git_identity

# --- stage and commit -------------------------------------------------------
info "Staging files"
git add -A

if git diff --cached --quiet; then
  ok "Nothing new to commit."
else
  git commit -q -m "$COMMIT_MSG"
  ok "Committed: $COMMIT_MSG"
fi

# --- configure remote -------------------------------------------------------
REMOTE_PLAIN="https://github.com/${GH_USER}/${GH_REPO}.git"
REMOTE_AUTH="https://${GH_USER}:${GH_TOKEN}@github.com/${GH_USER}/${GH_REPO}.git"

if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$REMOTE_PLAIN"
else
  git remote add origin "$REMOTE_PLAIN"
fi

ok "Remote 'origin' -> $REMOTE_PLAIN"

# --- push ------------------------------------------------------------------
info "Pushing to $GH_BRANCH"

if git push "$REMOTE_AUTH" "HEAD:${GH_BRANCH}" >/dev/null 2>&1; then
  ok "Pushed successfully."
else
  die "Push failed. Check that the token has repo scope and that the repository exists at $REMOTE_PLAIN"
fi

printf "\n%s✓ Done!%s View it at https://github.com/%s/%s\n" "$GREEN$BOLD" "$RESET" "$GH_USER" "$GH_REPO"
