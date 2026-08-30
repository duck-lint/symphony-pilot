#!/usr/bin/env bash
set -euo pipefail

ORIGINAL_CODEX_HOME_RAW="${CODEX_HOME:-$HOME/.codex}"
if command -v python >/dev/null 2>&1; then
  PYTHON_BIN=python
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN=python3
else
  echo "symphony-pilot: Python is required for Codex policy validation" >&2
  exit 78
fi

# The host may need the tracker credential; the Codex process must not receive
# it, including through a workspace-controlled environment fragment.
unset SYMPHONY_PILOT_GITHUB_TOKEN SYMPHONY_GITHUB_TOKEN GITHUB_TOKEN GH_TOKEN
if [[ -f .git/symphony-toolchain.env ]]; then
  source .git/symphony-toolchain.env
fi
unset SYMPHONY_PILOT_GITHUB_TOKEN SYMPHONY_GITHUB_TOKEN GITHUB_TOKEN GH_TOKEN

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROLE_SOURCE="${SYMPHONY_PILOT_ROLE_POLICY_DIR:-$SCRIPT_DIR/../workflow/agents}"
ROLE_FILES=("$ROLE_SOURCE"/*.toml)
EXPECTED_ROLE_FILES=(adversary archivist implementer planner project-manager reviewer)
ROLE_HOME_MARKER="$PWD/.git/symphony-role-home.json"

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
if [[ -e "$ROLE_HOME_MARKER" ]]; then
  echo "symphony-pilot: stale role-home lease must be reconciled by host preparation" >&2
  exit 78
fi

# Codex agent identity is the TOML name, not the filename. Parse every active
# personal/project agent layer before staging anything. A malformed file,
# duplicate logical identity, or reserved pilot name fails closed.
ORIGINAL_CODEX_HOME="$("$PYTHON_BIN" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$ORIGINAL_CODEX_HOME_RAW")"
if ! "$PYTHON_BIN" - "$ORIGINAL_CODEX_HOME/agents" "$PWD/.codex/agents" "$ROLE_SOURCE" <<'PY'
import pathlib
import sys
import tomllib

reserved = {"project-manager", "planner", "implementer", "reviewer", "adversary", "archivist"}
seen = {}
user_and_project = sys.argv[1:3]
role_source = pathlib.Path(sys.argv[3])

def load(path, layer):
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SystemExit(f"symphony-pilot: invalid Codex agent policy {path}: {exc}")
    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise SystemExit(f"symphony-pilot: Codex agent policy has no logical name: {path}")
    if layer == "pilot" and (name != path.stem or name not in reserved):
        raise SystemExit(f"symphony-pilot: pilot policy logical name does not match its reserved role: {path}")
    if name in seen:
        raise SystemExit(f"symphony-pilot: ambiguous Codex agent name {name!r}: {seen[name]} and {path}")
    if layer != "pilot" and name in reserved:
        raise SystemExit(f"symphony-pilot: reserved Codex agent name collision {name!r}: {path}")
    seen[name] = str(path)

for root_text in user_and_project:
    root = pathlib.Path(root_text)
    if not root.is_dir():
        continue
    for path in sorted(root.glob("*.toml")):
        load(path, "active")

for path in sorted(role_source.glob("*.toml")):
    load(path, "pilot")
PY
then
  exit 78
fi

ROLE_CODEX_HOME="$(mktemp -d "/tmp/symphony-pilot-codex-home.XXXXXX")"
ROLE_HOME_MARKER_CREATED=0
cleanup_setup() {
  if [[ -n "${ROLE_CODEX_HOME:-}" && -d "$ROLE_CODEX_HOME" ]]; then
    rm -rf -- "$ROLE_CODEX_HOME"
  fi
  if [[ "${ROLE_HOME_MARKER_CREATED:-0}" == 1 && -e "$ROLE_HOME_MARKER" ]]; then
    rm -f -- "$ROLE_HOME_MARKER"
  fi
}
trap cleanup_setup EXIT INT TERM

# Present Codex with a process-local home without whitelisting user state.
# Every original top-level surface (hooks, config layers, profiles, auth, and
# future user policy files) remains discoverable through a symlink. The
# personal agents directory is overlaid with preserved user agents plus the
# six control-plane policies. No credential contents are copied or inspected.
mkdir "$ROLE_CODEX_HOME/agents"
shopt -s dotglob nullglob
for entry in "$ORIGINAL_CODEX_HOME"/*; do
  base="$(basename "$entry")"
  [[ "$base" == "agents" ]] && continue
  ln -s -- "$entry" "$ROLE_CODEX_HOME/$base"
done
if [[ -e "$ORIGINAL_CODEX_HOME/agents" && ! -d "$ORIGINAL_CODEX_HOME/agents" ]]; then
  echo "symphony-pilot: operator Codex agents path is not a directory" >&2
  exit 78
fi
if [[ -d "$ORIGINAL_CODEX_HOME/agents" ]]; then
  for entry in "$ORIGINAL_CODEX_HOME/agents"/*.toml; do
    ln -s -- "$entry" "$ROLE_CODEX_HOME/agents/$(basename "$entry")"
  done
fi
for source in "${ROLE_FILES[@]}"; do
  cp -- "$source" "$ROLE_CODEX_HOME/agents/$(basename "$source")"
done
shopt -u dotglob nullglob
export CODEX_HOME="$ROLE_CODEX_HOME"

# This host-owned lease is intentionally inside .git, so it is invisible to
# target Git status. after_run removes it on normal completion; preparation
# and before_remove reconcile the exact /tmp path after a crash or kill.
temporary_marker="$ROLE_HOME_MARKER.tmp"
printf '{"path":"%s","pid":%s,"schema":"symphony-pilot-role-home/v1"}\n' \
  "$ROLE_CODEX_HOME" "$$" > "$temporary_marker"
mv -- "$temporary_marker" "$ROLE_HOME_MARKER"
ROLE_HOME_MARKER_CREATED=1

# The launcher must remain the directly managed app-server process. Cleanup is
# host-owned after this point; keeping a shell parent would falsify Symphony's
# PID, signal, timeout, and port-close contract.
trap - EXIT INT TERM
exec "${CODEX_BIN:-codex}" \
  --config shell_environment_policy.inherit=all \
  --config "model=\"${SYMPHONY_PILOT_CODEX_MODEL:-gpt-5.6-luna}\"" \
  --config "model_reasoning_effort=${SYMPHONY_PILOT_CODEX_REASONING:-high}" \
  app-server
