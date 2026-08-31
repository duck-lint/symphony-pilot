#!/bin/sh
set -eu

# This launcher creates only the pilot-owned policy surface. It deliberately
# has no path to the operator CODEX_HOME, auth.json, hooks, profiles, agents,
# history, caches, or ambient task credentials.
if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN=python
else
  echo "symphony-pilot: Python is required for containment validation" >&2
  exit 78
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROLE_SOURCE=${SYMPHONY_PILOT_ROLE_POLICY_DIR:-$SCRIPT_DIR/../workflow/agents}
EXPECTED_ROLES="adversary archivist implementer planner project-manager reviewer"

if [ ! -d "$ROLE_SOURCE" ]; then
  echo "symphony-pilot: deployed role policy pack is incomplete" >&2
  exit 78
fi
for name in $EXPECTED_ROLES; do
  if [ ! -f "$ROLE_SOURCE/$name.toml" ]; then
    echo "symphony-pilot: deployed role policy pack is missing $name.toml" >&2
    exit 78
  fi
done

# Validate logical names without reading any operator-owned Codex path.
if ! "$PYTHON_BIN" - "$ROLE_SOURCE" <<'PY'
import pathlib
import sys
import tomllib

reserved = {"project-manager", "planner", "implementer", "reviewer", "adversary", "archivist"}
root = pathlib.Path(sys.argv[1])
paths = sorted(root.glob("*.toml"))
if len(paths) != 6:
    raise SystemExit("symphony-pilot: role policy pack must contain exactly six files")
seen = set()
for path in paths:
    try:
        name = tomllib.loads(path.read_text(encoding="utf-8")).get("name")
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SystemExit(f"symphony-pilot: invalid role policy {path}: {exc}")
    if name not in reserved or name != path.stem or name in seen:
        raise SystemExit(f"symphony-pilot: invalid or duplicate pilot role: {path}")
    seen.add(name)
PY
then
  exit 78
fi

# The home is fresh and task-local. The runtime must not be started with an
# authentication file or secret environment variable in this domain.
TASK_CODEX_HOME=$(mktemp -d /tmp/symphony-pilot-task-codex-home.XXXXXX)
cleanup() { rm -rf -- "$TASK_CODEX_HOME"; }
trap cleanup 0 1 2 3 15
mkdir "$TASK_CODEX_HOME/agents"
for name in $EXPECTED_ROLES; do
  cp -- "$ROLE_SOURCE/$name.toml" "$TASK_CODEX_HOME/agents/$name.toml"
done
printf '%s\n' '# Generated allowlist-only task configuration.' 'approval_policy = "never"' > "$TASK_CODEX_HOME/config.toml"
chmod 700 "$TASK_CODEX_HOME"
chmod 600 "$TASK_CODEX_HOME/config.toml"

unset SYMPHONY_PILOT_GITHUB_TOKEN SYMPHONY_GITHUB_TOKEN GITHUB_TOKEN GH_TOKEN
unset OPENAI_API_KEY CODEX_API_KEY CODEX_ACCESS_TOKEN
export CODEX_HOME="$TASK_CODEX_HOME"

# This is intentionally a hard gate. Current Codex authentication and
# external-sandbox routing do not provide the proven credential boundary the
# project requires. Do not replace this with direct codex app-server or a
# same-user fallback.
"$PYTHON_BIN" "$SCRIPT_DIR/containment.py" || {
  echo "symphony-pilot: Codex execution blocked; authentication/containment capability is unproven" >&2
  exit 78
}
echo "symphony-pilot: containment capability unexpectedly passed without an auth broker" >&2
exit 78
