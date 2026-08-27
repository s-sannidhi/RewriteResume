#!/usr/bin/env bash
# Publish a FRESH public copy of this repo with ZERO personal data in history.
#
# Why: older commits on main still contain data/ (profile, rr.db, transcripts). Deleting those
# files in a new commit does NOT remove them from GitHub history. This script builds an orphan
# branch from the current working tree (respecting .gitignore), verifies it is clean, then
# force-pushes it as main.
#
# Your live profile is NEVER touched — it lives in ~/ResumeRewriter, outside this repo.
#
# Usage:
#   ./scripts/make_public_clean.sh              # dry-run: build + verify, no push
#   ./scripts/make_public_clean.sh --push       # rewrite origin/main (DESTRUCTIVE to remote history)
#   ./scripts/make_public_clean.sh --push --public   # also `gh repo edit --visibility public`
#
set -euo pipefail
cd "$(dirname "$0")/.."

PUSH=0
MAKE_PUBLIC=0
for arg in "$@"; do
  case "$arg" in
    --push) PUSH=1 ;;
    --public) MAKE_PUBLIC=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $arg"; exit 1 ;;
  esac
done

echo "==> Checking working tree for personal data patterns…"
python3 scripts/check_no_personal_data.py --install-hook >/dev/null
# Staged data/ deletions are fine; scan tracked + staged adds only.
if ! python3 scripts/check_no_personal_data.py; then
  echo "Fix the hits above before publishing."
  exit 1
fi

if git ls-files | grep -E '^data/|profile\.json$|rr\.db$|skills_verified\.yaml$' >/dev/null; then
  echo "ERROR: personal files are still tracked. Run: git rm -r --cached data/"
  exit 1
fi

BRANCH="public-clean-$$"
CURRENT=$(git rev-parse --abbrev-ref HEAD)
echo "==> Building orphan branch ${BRANCH} from current tree (no personal history)…"

# Stash is NOT used — we commit the index+worktree state onto an orphan.
git checkout --orphan "$BRANCH"
# Clear the index, then re-add everything (gitignore excludes data/).
git reset
git add -A
# Ensure data/ never sneaks in even if someone force-added it.
git rm -r --cached --ignore-unmatch data/ >/dev/null 2>&1 || true

if ! python3 scripts/check_no_personal_data.py; then
  echo "Aborting: clean tree still failed the personal-data check."
  git checkout "$CURRENT"
  git branch -D "$BRANCH" 2>/dev/null || true
  exit 1
fi

git commit -m "$(cat <<'EOF'
Public release: code only — no personal data.

Live profile, skills, application history and documents stay in ~/ResumeRewriter
on each user's machine. This history starts fresh so old private snapshots are gone.
EOF
)"

echo "==> Orphan commit ready: $(git rev-parse --short HEAD)"
echo "    Files in this commit: $(git ls-files | wc -l | tr -d ' ')"

if [ "$PUSH" -ne 1 ]; then
  echo
  echo "Dry-run only. To rewrite the remote and publish:"
  echo "  ./scripts/make_public_clean.sh --push"
  echo "  ./scripts/make_public_clean.sh --push --public"
  echo
  echo "Returning to ${CURRENT} (orphan branch kept as ${BRANCH} for inspection)."
  git checkout "$CURRENT"
  exit 0
fi

echo "==> Force-pushing ${BRANCH} → origin/main (rewrites remote history)…"
git push --force origin "${BRANCH}:main"
git branch -M main
git branch -D "$BRANCH" 2>/dev/null || true
git checkout main 2>/dev/null || true

if [ "$MAKE_PUBLIC" -eq 1 ]; then
  if command -v gh >/dev/null 2>&1; then
    echo "==> Setting GitHub repo visibility to public…"
    gh repo edit --visibility public --accept-visibility-change-consequences
  else
    echo "gh not found — set visibility manually on GitHub."
  fi
fi

echo
echo "Done. Remote main is a clean public history."
echo "Your data is still at: ~/ResumeRewriter"
echo "Day-to-day: edit code → commit → git push  (pre-commit blocks personal files)."
