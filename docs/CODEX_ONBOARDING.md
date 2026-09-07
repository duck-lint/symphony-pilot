# Codex onboarding

## Current supervised-local path

The supported local-development launch is explicit:

```sh
SYMPHONY_SUPERVISED_LOCAL=1 \
SYMPHONY_BIN=/absolute/path/to/symphony-runtime/elixir/bin/symphony \
python3 scripts/project.py --project <slug> start
```

This launches the normal installed `codex app-server` using the operator's
existing authenticated Codex environment. The six role TOMLs remain
project-independent and are made available through the task-local policy
pack. Supervised local use is not claimed safe for unattended hostile
execution.

The launcher creates a fresh task-local `CODEX_HOME` containing only the six
pilot role policies and generated task configuration. It never copies or
symlinks the operator home and never passes tracker or publication credentials
to tools. Role TOML files are an allowlist; their presence does not prove
native role dispatch or read-only enforcement.

The launcher still retains the reviewed namespace and resource checks where
they apply, but supervised-local mode deliberately permits the operator's
authenticated App Server environment. This proves live App Server startup and
agent orchestration, not unattended credential isolation or aggregate quota
enforcement.

Do not describe supervised-local authentication as a production credential
boundary. Do not claim the operator's Codex credential is isolated from
hostile children. The fixed Git identity supplied to supervised agents is
`Symphony Agent <symphony@localhost>` and does not mutate global Git config.
