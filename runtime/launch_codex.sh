#!/usr/bin/env bash
set -euo pipefail

if [[ -f .git/symphony-toolchain.env ]]; then
  source .git/symphony-toolchain.env
fi

# The host may need the tracker credential; the Codex child must not receive it.
unset SYMPHONY_PILOT_GITHUB_TOKEN SYMPHONY_GITHUB_TOKEN GITHUB_TOKEN GH_TOKEN

exec "${CODEX_BIN:-codex}" \
  --config shell_environment_policy.inherit=all \
  --config "model=\"${SYMPHONY_PILOT_CODEX_MODEL:-gpt-5.6-luna}\"" \
  --config "model_reasoning_effort=${SYMPHONY_PILOT_CODEX_REASONING:-high}" \
  app-server
