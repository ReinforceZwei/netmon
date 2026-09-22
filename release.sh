#!/usr/bin/env bash
# Usage: ./release.sh [patch|minor|major] [--push|-p]
# Bumps the version in netmon/__init__.py (single source of truth — pyproject
# reads it dynamically), creates a git commit, and tags it.
# With --push (or -p), also pushes the commit and tag to origin, which triggers
# the "Build and Push Docker Image" workflow.
set -euo pipefail

BUMP="patch"
PUSH=false

for arg in "$@"; do
  case "$arg" in
    patch|minor|major) BUMP="$arg" ;;
    --push|-p)         PUSH=true ;;
    *)
      echo "Usage: ./release.sh [patch|minor|major] [--push|-p]"
      echo ""
      echo "  patch  (default) — bug fix:           v1.0.0 → v1.0.1"
      echo "  minor            — new feature:        v1.0.0 → v1.1.0"
      echo "  major            — breaking change:    v1.0.0 → v2.0.0"
      echo "  --push, -p       — also run: git push && git push --tags"
      exit 1
      ;;
  esac
done

# Ensure we are at the repo root (where this script lives)
cd "$(dirname "$0")"

# Check for uncommitted changes before doing anything
# (git status --porcelain also catches untracked files)
if [[ -n "$(git status --porcelain)" ]]; then
  echo "Error: working tree has uncommitted changes. Commit or stash them first."
  exit 1
fi

VERSION_FILE="netmon/__init__.py"

# Bump __version__ in place and echo the new version
NEW_VERSION=$(python3 - "$VERSION_FILE" "$BUMP" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
bump = sys.argv[2]
text = path.read_text()
match = re.search(r'^__version__ = "(\d+)\.(\d+)\.(\d+)"', text, re.M)
if not match:
    sys.exit(f"could not find __version__ in {path}")
major, minor, patch = (int(g) for g in match.groups())
if bump == "major":
    major, minor, patch = major + 1, 0, 0
elif bump == "minor":
    minor, patch = minor + 1, 0
else:
    patch += 1
new = f"{major}.{minor}.{patch}"
path.write_text(text[: match.start()] + f'__version__ = "{new}"' + text[match.end() :])
print(new)
PY
)

TAG="v${NEW_VERSION}"

echo "Releasing ${TAG}..."

git add "$VERSION_FILE"
git commit -m "chore: release ${TAG}"
git tag "${TAG}"

if $PUSH; then
  echo "Pushing ${TAG}..."
  git push
  git push --tags
else
  echo ""
  echo "Done. Run the following to publish:"
  echo ""
  echo "  git push && git push --tags"
fi
