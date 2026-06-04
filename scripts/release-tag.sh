#!/usr/bin/env bash
# Cut an internal release: validate state, create annotated tag, push to GitHub.
# The release-docker.yml workflow takes over from there.
#
# Usage: scripts/release-tag.sh v1.83.10-internal.N

set -euo pipefail

VERSION="${1:-}"
if [[ -z "${VERSION}" ]]; then
  echo "Usage: $0 v1.83.10-internal.N" >&2
  exit 2
fi

# Tag format: v<MAJOR>.<MINOR>.<PATCH>-internal.<N>
if ! [[ "${VERSION}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+-internal\.[0-9]+$ ]]; then
  echo "Error: tag must match vX.Y.Z-internal.N (got: ${VERSION})" >&2
  exit 2
fi

BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [[ "${BRANCH}" != ship/* ]]; then
  echo "Error: must release from a ship/* branch (currently on: ${BRANCH})" >&2
  exit 2
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Error: worktree is dirty. Commit or stash first." >&2
  exit 2
fi

# Ensure local branch is up to date with origin
git fetch origin "${BRANCH}"
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/${BRANCH}")
if [[ "${LOCAL}" != "${REMOTE}" ]]; then
  echo "Error: local ${BRANCH} is not in sync with origin (local=${LOCAL} remote=${REMOTE})" >&2
  echo "Run: git pull --ff-only origin ${BRANCH}" >&2
  exit 2
fi

# Refuse to overwrite an existing tag
if git rev-parse "refs/tags/${VERSION}" >/dev/null 2>&1; then
  echo "Error: tag ${VERSION} already exists locally." >&2
  exit 2
fi
if git ls-remote --tags origin "${VERSION}" | grep -q "${VERSION}"; then
  echo "Error: tag ${VERSION} already exists on origin." >&2
  exit 2
fi

# Auto-generate changelog body from commits since the last internal tag
BASE_VERSION="${VERSION%-internal.*}"
LAST_TAG=$(git tag --list "${BASE_VERSION}-internal.*" --sort=-v:refname | head -n1 || true)
if [[ -n "${LAST_TAG}" ]]; then
  RANGE="${LAST_TAG}..HEAD"
  echo "Generating changelog from ${LAST_TAG}..HEAD"
else
  RANGE="HEAD~10..HEAD"
  echo "No previous internal tag found, using last 10 commits"
fi

CHANGELOG=$(git log --pretty=format:'- %s (%h)' "${RANGE}" --no-merges | head -n 30)

cat <<EOF

============================================================
About to create tag: ${VERSION}
On commit:           ${LOCAL}
Branch:              ${BRANCH}

Changelog:
${CHANGELOG}
============================================================
EOF

read -rp "Proceed? [y/N] " CONFIRM
if [[ "${CONFIRM}" != "y" && "${CONFIRM}" != "Y" ]]; then
  echo "Aborted."
  exit 1
fi

# Create annotated tag with changelog as message
git tag -a "${VERSION}" -m "Internal release ${VERSION}

${CHANGELOG}
"

git push origin "${VERSION}"

cat <<EOF

Tag ${VERSION} pushed to origin.

Watch the build at:
  https://github.com/${GITHUB_REPOSITORY:-GhishaDev/litellm}/actions/workflows/release-docker.yml

Once green, the following images will be available:
  docker pull <your-dockerhub-user>/litellm:${VERSION}
  docker pull <your-dockerhub-user>/litellm:${BASE_VERSION}-stable
EOF
