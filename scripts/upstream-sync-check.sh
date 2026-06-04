#!/usr/bin/env bash
# upstream-sync-check.sh — list upstream commits we haven't ingested.
#
# Reads the current pin from CLAUDE.md "Current pinning" block, fetches
# the upstream BerriAI/litellm remote, and reports:
#   1. Missed backports on our minor line (.patch.N + newer vX.Y.W)
#   2. Newer minor lines stabilized since our pin
#   3. Commit volume on upstream/main since our pin
#   4. Security-flagged commit subjects since our pin
#
# Read-only: never modifies branches, tags, or remote state.
#
# Usage:
#   scripts/upstream-sync-check.sh            # fetch then check
#   scripts/upstream-sync-check.sh --no-fetch # skip fetch (offline)
#
# Run monthly per CLAUDE.md → "Upstream sync cadence".

set -euo pipefail

# ---------- Args ----------

NO_FETCH=0
for arg in "$@"; do
  case "${arg}" in
    --no-fetch) NO_FETCH=1 ;;
    -h|--help)
      sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *)
      echo "Unknown argument: ${arg}" >&2
      exit 2 ;;
  esac
done

# ---------- Locate repo ----------

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "Error: not inside a git repository." >&2
  exit 2
}
cd "${REPO_ROOT}"

CLAUDE_MD="${REPO_ROOT}/CLAUDE.md"
UPSTREAM_REMOTE="upstream"
EXPECTED_UPSTREAM_FRAGMENT="BerriAI/litellm"
SECURITY_RE='(\[SECURITY\]|fix\(auth\)|fix\(security\)|CVE-[0-9]|SSRF|IDOR|bypass|injection|sandbox|hijack|traversal|disclosure|VERIA-)'

# ---------- Output helpers ----------

if [[ -t 1 ]]; then
  C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
  C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
  C_RED=$'\033[31m'; C_BLUE=$'\033[34m'
  C_RESET=$'\033[0m'
else
  C_BOLD=''; C_DIM=''; C_GREEN=''; C_YELLOW=''
  C_RED=''; C_BLUE=''; C_RESET=''
fi

section() { printf '\n%s== %s ==%s\n' "${C_BOLD}${C_BLUE}" "$*" "${C_RESET}"; }
ok() { printf '%s✓%s %s\n' "${C_GREEN}" "${C_RESET}" "$*"; }
warn() { printf '%s⚠%s %s\n' "${C_YELLOW}" "${C_RESET}" "$*"; }
err() { printf '%s✗%s %s\n' "${C_RED}" "${C_RESET}" "$*" >&2; }
dim() { printf '%s%s%s\n' "${C_DIM}" "$*" "${C_RESET}"; }

# ---------- Parse pin from CLAUDE.md ----------

if [[ ! -f "${CLAUDE_MD}" ]]; then
  err "CLAUDE.md not found at ${CLAUDE_MD}"
  exit 2
fi

# Expected line: "- **Upstream pin**: `v1.83.10-stable`"
PIN_TAG="$(grep -m1 -oE '\*\*Upstream pin\*\*: `[^`]+`' "${CLAUDE_MD}" \
           | sed -E 's/.*`([^`]+)`.*/\1/' || true)"

if [[ -z "${PIN_TAG}" ]]; then
  err "Could not find the 'Upstream pin' line in CLAUDE.md."
  err "Expected a line like:  - **Upstream pin**: \`v1.83.10-stable\`"
  exit 2
fi

if ! [[ "${PIN_TAG}" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)(-stable)?$ ]]; then
  err "Pin tag does not match vMAJOR.MINOR.PATCH[-stable]: ${PIN_TAG}"
  exit 2
fi
PIN_MAJOR="${BASH_REMATCH[1]}"
PIN_MINOR="${BASH_REMATCH[2]}"
PIN_PATCH="${BASH_REMATCH[3]}"

# ---------- Verify upstream remote ----------

UPSTREAM_URL="$(git config --get "remote.${UPSTREAM_REMOTE}.url" 2>/dev/null || true)"
if [[ -z "${UPSTREAM_URL}" ]]; then
  err "Git remote '${UPSTREAM_REMOTE}' is not configured."
  err "Add it with: git remote add ${UPSTREAM_REMOTE} https://github.com/BerriAI/litellm.git"
  exit 2
fi

if [[ "${UPSTREAM_URL}" != *"${EXPECTED_UPSTREAM_FRAGMENT}"* ]]; then
  warn "Remote '${UPSTREAM_REMOTE}' URL does not contain '${EXPECTED_UPSTREAM_FRAGMENT}':"
  warn "  ${UPSTREAM_URL}"
fi

# ---------- Fetch ----------

if [[ "${NO_FETCH}" -eq 1 ]]; then
  dim "Skipping fetch (--no-fetch)."
else
  dim "Fetching ${UPSTREAM_REMOTE} (tags + heads) ..."
  git fetch "${UPSTREAM_REMOTE}" --tags --quiet
fi

# ---------- Verify pin reachable ----------

if ! git rev-parse --verify --quiet "${PIN_TAG}^{commit}" >/dev/null; then
  err "Pin tag ${PIN_TAG} is not reachable in this repo."
  err "Make sure 'git fetch ${UPSTREAM_REMOTE} --tags' has completed."
  exit 2
fi

# ---------- Header ----------

echo
printf '%sUpstream sync check%s — pinned to %s%s%s\n' \
  "${C_BOLD}" "${C_RESET}" "${C_BOLD}" "${PIN_TAG}" "${C_RESET}"
dim "Generated: $(date '+%Y-%m-%d %H:%M:%S')"

# ---------- 1. Missed backports on our minor line ----------

section "1. Missed backports on v${PIN_MAJOR}.${PIN_MINOR}.x"

# Collect candidates:
#   - .patch.N suffix on the exact pin tag (old convention)
#   - vX.Y.Z / vX.Y.Z-stable with same MAJOR.MINOR and higher PATCH
MISSED=()

while IFS= read -r tag; do
  [[ -n "${tag}" ]] && MISSED+=("${tag}")
done < <(git tag -l "${PIN_TAG}.patch.*" --sort=version:refname)

while IFS= read -r tag; do
  if [[ "${tag}" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)(-stable)?$ ]]; then
    M="${BASH_REMATCH[1]}"; N="${BASH_REMATCH[2]}"; P="${BASH_REMATCH[3]}"
    if (( M == PIN_MAJOR && N == PIN_MINOR && P > PIN_PATCH )); then
      MISSED+=("${tag}")
    fi
  fi
done < <(git tag -l "v${PIN_MAJOR}.${PIN_MINOR}.*" --sort=version:refname \
         | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+(-stable)?$' || true)

# Plus .patch.N on any newer same-minor stable tag
while IFS= read -r tag; do
  if [[ "${tag}" =~ ^v${PIN_MAJOR}\.${PIN_MINOR}\.([0-9]+)-stable\.patch\.[0-9]+$ ]]; then
    P="${BASH_REMATCH[1]}"
    if (( P > PIN_PATCH )); then
      MISSED+=("${tag}")
    fi
  fi
done < <(git tag -l "v${PIN_MAJOR}.${PIN_MINOR}.*-stable.patch.*" --sort=version:refname || true)

if [[ ${#MISSED[@]} -eq 0 ]]; then
  ok "Nothing new on v${PIN_MAJOR}.${PIN_MINOR}.x."
else
  warn "${#MISSED[@]} tag(s) on the pinned line we have not synced:"
  for tag in "${MISSED[@]}"; do
    COMMIT_DATE="$(git log -1 --format=%ai "${tag}" 2>/dev/null | head -c 10)"
    echo "    ${tag}  (${COMMIT_DATE})"
  done
  echo
  dim "  Inspect a tag:   git log ${PIN_TAG}..<tag> --oneline --no-merges"
fi

# ---------- 2. Newer minor lines stabilized ----------

section "2. Newer minor lines stabilized since pin"

declare -A SEEN_MINOR=()
NEWER_MINORS=()

while IFS= read -r tag; do
  if [[ "${tag}" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)(-stable)?$ ]]; then
    M="${BASH_REMATCH[1]}"; N="${BASH_REMATCH[2]}"
    if (( M == PIN_MAJOR && N > PIN_MINOR )); then
      KEY="${M}.${N}"
      if [[ -z "${SEEN_MINOR[$KEY]:-}" ]]; then
        SEEN_MINOR[$KEY]=1
        NEWER_MINORS+=("${KEY}")
      fi
    fi
  fi
done < <(git tag -l "v${PIN_MAJOR}.*" --sort=version:refname \
         | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+(-stable)?$' || true)

if [[ ${#NEWER_MINORS[@]} -eq 0 ]]; then
  ok "No newer minor lines published a stable tag."
else
  warn "${#NEWER_MINORS[@]} newer minor line(s) available:"
  for minor in "${NEWER_MINORS[@]}"; do
    LATEST="$(git tag -l "v${minor}.*" --sort=-version:refname \
              | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+(-stable)?$' \
              | head -1)"
    COMMIT_DATE="$(git log -1 --format=%ai "${LATEST}" 2>/dev/null | head -c 10)"
    echo "    v${minor}.x  →  latest stable: ${LATEST}  (${COMMIT_DATE})"
  done
  echo
  dim "  Consider quarterly bump per CLAUDE.md → Upstream sync cadence."
fi

# ---------- 3. upstream/main volume ----------

section "3. Volume on ${UPSTREAM_REMOTE}/main since ${PIN_TAG}"

if ! git rev-parse --verify --quiet "${UPSTREAM_REMOTE}/main" >/dev/null; then
  warn "${UPSTREAM_REMOTE}/main is not fetched. Re-run without --no-fetch."
else
  MAIN_COUNT="$(git rev-list --count "${PIN_TAG}..${UPSTREAM_REMOTE}/main" 2>/dev/null || echo "?")"
  printf '%sCommits on %s/main not in %s: %s%s%s\n' \
    "" "${UPSTREAM_REMOTE}" "${PIN_TAG}" "${C_BOLD}" "${MAIN_COUNT}" "${C_RESET}"
  if [[ "${MAIN_COUNT}" != "?" && "${MAIN_COUNT}" -gt 0 ]]; then
    echo
    dim "  Sample (5 most recent, no-merges):"
    git log --no-merges --format="    %h %s" -5 "${PIN_TAG}..${UPSTREAM_REMOTE}/main"
  fi
fi

# ---------- 4. Security-flagged commits ----------

section "4. Security-flagged commits on ${UPSTREAM_REMOTE}/main since ${PIN_TAG}"

if ! git rev-parse --verify --quiet "${UPSTREAM_REMOTE}/main" >/dev/null; then
  warn "${UPSTREAM_REMOTE}/main not fetched — skipping."
else
  SEC_TMP="$(mktemp)"
  trap 'rm -f "${SEC_TMP}"' EXIT
  git log --no-merges --format='%h %s' \
      "${PIN_TAG}..${UPSTREAM_REMOTE}/main" \
    | grep -iE "${SECURITY_RE}" > "${SEC_TMP}" || true

  COUNT="$(wc -l < "${SEC_TMP}" | tr -d ' ')"
  if [[ "${COUNT}" -eq 0 ]]; then
    ok "No security-flagged subjects matched."
  else
    warn "${COUNT} security-flagged commit(s) (regex: ${SECURITY_RE}):"
    sed 's/^/    /' "${SEC_TMP}"
    echo
    dim "  Triage per CLAUDE.md → Fork tier classification (Tier C → upstream PR or carry)."
  fi
fi

# ---------- Footer ----------

section "Next steps"
cat <<EOF
  • Open fix/* branches for must-backport security items.
  • Re-classify the carried ship-branch delta — drop fixes upstreamed by others.
  • Quarterly: evaluate whether to bump to a newer minor line.

EOF
dim "  Read-only check. To act, see CLAUDE.md → Upstream sync cadence."
echo
