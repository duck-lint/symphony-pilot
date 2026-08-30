#!/usr/bin/env bash
set -euo pipefail

# The host may need the tracker credential; the Codex child must not receive it.
# Clear it before sourcing any workspace-controlled shell fragment.
unset SYMPHONY_PILOT_GITHUB_TOKEN SYMPHONY_GITHUB_TOKEN GITHUB_TOKEN GH_TOKEN

if [[ -f .git/symphony-toolchain.env ]]; then
  source .git/symphony-toolchain.env
fi

# Codex discovers project-scoped custom agents from .codex/agents. Materialize
# the deployed, generic role pack only for this app-server lifetime so the
# target repository is not modified in Git and the operator's global CODEX_HOME
# (including authentication) is not replaced. Never overwrite target-owned
# role files; a collision is an infrastructure/capability blocker.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROLE_SOURCE="${SYMPHONY_PILOT_ROLE_POLICY_DIR:-$SCRIPT_DIR/../workflow/agents}"
ROLE_TARGET="$PWD/.codex/agents"
ROLE_FILES=("$ROLE_SOURCE"/*.toml)
EXPECTED_ROLE_FILES=(adversary archivist implementer planner project-manager reviewer)

if [[ ! -d "$ROLE_SOURCE" || ${#ROLE_FILES[@]} -ne 6 ]]; then
  echo "symphony-pilot: deployed role policy pack is incomplete" >&2
  exit 78
fi
for name in "${EXPECTED_ROLE_FILES[@]}"; do
  if [[ ! -f "$ROLE_SOURCE/$name.toml" ]]; then
    echo "symphony-pilot: deployed role policy pack is missing $name.toml" >&2
    exit 78
  fi
done

# Preflight all collisions before creating anything in the target checkout.
for source in "${ROLE_FILES[@]}"; do
  target="$ROLE_TARGET/$(basename "$source")"
  if [[ -e "$target" ]]; then
    echo "symphony-pilot: target-owned role policy collision: $target" >&2
    exit 78
  fi
done

CREATED_CODEX_DIR=0
CREATED_ROLE_DIR=0
if [[ ! -d "$PWD/.codex" ]]; then
  mkdir "$PWD/.codex"
  CREATED_CODEX_DIR=1
else
  CREATED_CODEX_DIR=0
fi
if [[ ! -d "$ROLE_TARGET" ]]; then
  mkdir "$ROLE_TARGET"
  CREATED_ROLE_DIR=1
else
  CREATED_ROLE_DIR=0
fi

INSTALLED_ROLE_FILES=()
for source in "${ROLE_FILES[@]}"; do
  target="$ROLE_TARGET/$(basename "$source")"
  INSTALLED_ROLE_FILES+=("$target")
done

cleanup_role_policies() {
  for target in "${INSTALLED_ROLE_FILES[@]}"; do
    rm -f -- "$target"
  done
  if [[ "$CREATED_ROLE_DIR" -eq 1 ]]; then
    rmdir -- "$ROLE_TARGET" 2>/dev/null || true
  fi
  if [[ "$CREATED_CODEX_DIR" -eq 1 ]]; then
    rmdir -- "$PWD/.codex" 2>/dev/null || true
  fi
}
trap cleanup_role_policies EXIT

for index in "${!ROLE_FILES[@]}"; do
  cp "${ROLE_FILES[$index]}" "${INSTALLED_ROLE_FILES[$index]}"
done

set +e
"${CODEX_BIN:-codex}" \
  --config shell_environment_policy.inherit=all \
  --config "model=\"${SYMPHONY_PILOT_CODEX_MODEL:-gpt-5.6-luna}\"" \
  --config "model_reasoning_effort=${SYMPHONY_PILOT_CODEX_REASONING:-high}" \
  app-server
status=$?
set -e
exit "$status"
