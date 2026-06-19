#!/usr/bin/env bash
#
# TigerLiteCode — GitHub upload helper.
#
# Creates the GitHub repo if needed, commits this project, and pushes to:
#   https://github.com/esterzollar/tigercli
#
set -euo pipefail

# --- config ----------------------------------------------------------------
GH_USER="esterzollar"
GH_REPO="tigercli"
GH_BRANCH="main"
TOKEN_FILE=".github_token"

GIT_NAME="Esterzollar"
GIT_EMAIL="esterzollar@github.com"

REPO_PRIVATE="false"
REPO_DESCRIPTION="TigerCLI / TigerLiteCode terminal tool"

# --- colors ----------------------------------------------------------------
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
      echo "Usage:"
      echo "  ./github.sh"
      echo "  ./github.sh -m \"commit message\""
      echo "  ./github.sh --reset-token"
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

command -v git >/dev/null 2>&1 || die "git is not installed."
command -v curl >/dev/null 2>&1 || die "curl is not installed."

printf "%s🐅 Upload to github.com/%s/%s%s\n\n" "$ORANGE$BOLD" "$GH_USER" "$GH_REPO" "$RESET"

# --- protect token ----------------------------------------------------------
if [ ! -f .gitignore ] || ! grep -qxF "$TOKEN_FILE" .gitignore 2>/dev/null; then
  printf "\n# Local GitHub token used by github.sh. Never commit this file.\n%s\n" "$TOKEN_FILE" >> .gitignore
  ok "Added $TOKEN_FILE to .gitignore"
fi

# --- token -----------------------------------------------------------------
if [ -f "$TOKEN_FILE" ]; then
  GH_TOKEN="$(cat "$TOKEN_FILE")"
  ok "Using saved token from $TOKEN_FILE"
else
  info "A GitHub Personal Access Token is required."
  echo "    Classic token: needs repo scope"
  echo "    Fine-grained token: needs repository creation permission and Contents: Read and write"
  echo "    Create token at: https://github.com/settings/tokens"
  printf "    Token input hidden: "

  read -rs GH_TOKEN
  printf "\n"

  [ -n "$GH_TOKEN" ] || die "No token entered."

  ( umask 077; printf "%s" "$GH_TOKEN" > "$TOKEN_FILE" )
  ok "Token saved to $TOKEN_FILE"
fi

[ -n "$GH_TOKEN" ] || die "Empty token."

# --- init git ---------------------------------------------------------------
if [ ! -d .git ]; then
  info "Initializing git repository"
  git init -q
fi

git branch -M "$GH_BRANCH" 2>/dev/null || true

# --- set git identity -------------------------------------------------------
info "Setting Git identity"

git config user.name "$GIT_NAME"
git config user.email "$GIT_EMAIL"

ok "Git identity: $(git config user.name) <$(git config user.email)>"

# --- create GitHub repo if needed ------------------------------------------
REMOTE_PLAIN="https://github.com/${GH_USER}/${GH_REPO}.git"
REMOTE_AUTH="https://${GH_USER}:${GH_TOKEN}@github.com/${GH_USER}/${GH_REPO}.git"

info "Checking GitHub repository"

HTTP_CODE="$(
  curl -sS -o /tmp/tigercli_repo_check.json -w "%{http_code}" \
    -H "Authorization: Bearer ${GH_TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/${GH_USER}/${GH_REPO}"
)"

if [ "$HTTP_CODE" = "200" ]; then
  ok "Repository already exists: $REMOTE_PLAIN"
elif [ "$HTTP_CODE" = "404" ]; then
  info "Repository does not exist. Creating: ${GH_USER}/${GH_REPO}"

  CREATE_CODE="$(
    curl -sS -o /tmp/tigercli_repo_create.json -w "%{http_code}" \
      -X POST \
      -H "Authorization: Bearer ${GH_TOKEN}" \
      -H "Accept: application/vnd.github+json" \
      "https://api.github.com/user/repos" \
      -d "{\"name\":\"${GH_REPO}\",\"description\":\"${REPO_DESCRIPTION}\",\"private\":${REPO_PRIVATE},\"auto_init\":false}"
  )"

  if [ "$CREATE_CODE" = "201" ]; then
    ok "Created repository: $REMOTE_PLAIN"
  else
    echo
    echo "GitHub API response:"
    cat /tmp/tigercli_repo_create.json
    echo
    die "Failed to create repository. HTTP status: $CREATE_CODE"
  fi
else
  echo
  echo "GitHub API response:"
  cat /tmp/tigercli_repo_check.json
  echo
  die "Could not check repository. HTTP status: $HTTP_CODE"
fi

# --- stage and commit -------------------------------------------------------
info "Staging files"
git add -A

if git diff --cached --quiet; then
  ok "Nothing new to commit."
else
  git commit -m "$COMMIT_MSG"
  ok "Committed: $COMMIT_MSG"
fi

# --- remote ----------------------------------------------------------------
if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$REMOTE_PLAIN"
else
  git remote add origin "$REMOTE_PLAIN"
fi

ok "Remote 'origin' -> $REMOTE_PLAIN"

# --- push ------------------------------------------------------------------
info "Pushing to $GH_BRANCH"

git push "$REMOTE_AUTH" "HEAD:${GH_BRANCH}"

ok "Pushed successfully."

printf "\n%s✓ Done!%s View it at https://github.com/%s/%s\n" "$GREEN$BOLD" "$RESET" "$GH_USER" "$GH_REPO"
