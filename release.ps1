# Usage: .\release.ps1 [-Bump patch|minor|major] [-Push]
# Bumps the version in netmon/__init__.py (single source of truth — pyproject
# reads it dynamically), creates a git commit, and tags it.
# With -Push, also pushes the commit and tag to origin, which triggers the
# "Build and Push Docker Image" workflow.
param(
    [ValidateSet("patch", "minor", "major")]
    [string]$Bump = "patch",
    [switch]$Push
)

$ErrorActionPreference = "Stop"

# Ensure we are at the repo root (where this script lives)
Set-Location $PSScriptRoot

# Check for uncommitted changes before doing anything
$status = git status --porcelain
if ($status) {
    Write-Error "Working tree has uncommitted changes. Commit or stash them first."
    exit 1
}

$VersionFile = "netmon/__init__.py"

# Bump __version__ in place and read the new version
$NewVersion = python - "$VersionFile" "$Bump" @'
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
'@
$NewVersion = $NewVersion.Trim()
$Tag = "v$NewVersion"

Write-Host "Releasing $Tag..."

git add $VersionFile
git commit -m "chore: release $Tag"
git tag $Tag

if ($Push) {
    Write-Host "Pushing $Tag..."
    git push
    git push --tags
} else {
    Write-Host ""
    Write-Host "Done. Run the following to publish:"
    Write-Host ""
    Write-Host "  git push; git push --tags"
}
