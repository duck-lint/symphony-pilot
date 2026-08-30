#!/usr/bin/env bash
set -euo pipefail

ORIGINAL_CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"

# The host may need the tracker credential; the Codex child must not receive it.
# Clear it before and after sourcing any workspace-controlled shell fragment.
unset SYMPHONY_PILOT_GITHUB_TOKEN SYMPHONY_GITHUB_TOKEN GITHUB_TOKEN GH_TOKEN

if [[ -f .git/symphony-toolchain.env ]]; then
  source .git/symphony-toolchain.env
fi
unset SYMPHONY_PILOT_GITHUB_TOKEN SYMPHONY_GITHUB_TOKEN GITHUB_TOKEN GH_TOKEN

# Codex supports personal custom agents below $CODEX_HOME/agents as well as
# project-scoped agents below .codex/agents. Use a temporary per-process home so
# role setup is control-plane state outside the target checkout. Preserve the
# operator's authentication/configuration through symlinks; do not copy or
# inspect credential contents. Never shadow a target-owned same-name role.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROLE_SOURCE="${SYMPHONY_PILOT_ROLE_POLICY_DIR:-$SCRIPT_DIR/../workflow/agents}"
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

# A target-owned project role with the same name would make the selected
# policy ambiguous. Refuse it without creating or modifying .codex.
for name in "${EXPECTED_ROLE_FILES[@]}"; do
  target="$PWD/.codex/agents/$name.toml"
  if [[ -e "$target" ]]; then
    echo "symphony-pilot: target-owned role policy collision: $target" >&2
    exit 78
  fi
done

# Keep the staging location independent of workspace-controlled TMPDIR values.
ROLE_CODEX_HOME="$(mktemp -d "/tmp/symphony-pilot-codex-home.XXXXXX")"
mkdir "$ROLE_CODEX_HOME/agents"
for preserved in auth.json config.toml; do
  if [[ -f "$ORIGINAL_CODEX_HOME/$preserved" ]]; then
    ln -s "$ORIGINAL_CODEX_HOME/$preserved" "$ROLE_CODEX_HOME/$preserved"
  fi
done
export CODEX_HOME="$ROLE_CODEX_HOME"

INSTALLED_ROLE_FILES=()
for source in "${ROLE_FILES[@]}"; do
  target="$ROLE_CODEX_HOME/agents/$(basename "$source")"
  INSTALLED_ROLE_FILES+=("$target")
done

cleanup_role_home() {
  if [[ -n "$ROLE_CODEX_HOME" && -d "$ROLE_CODEX_HOME" ]]; then
    rm -rf -- "$ROLE_CODEX_HOME"
  fi
}
trap cleanup_role_home EXIT INT TERM

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
