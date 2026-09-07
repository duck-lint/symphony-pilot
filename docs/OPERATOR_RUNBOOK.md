# Supervised-local operator runbook

This is the ordinary local development path proven by the 2026-09-07 canary.
It is operator-supervised. It does not claim unattended credential isolation,
Step-8 quota admission, publication, PR creation, or merge.

## 1. Verify and deploy

From the Pilot checkout under WSL/Linux:

```sh
cd /mnt/f/PROJECT-REPOS/symphony/symphony-pilot
python3 scripts/validate_profile.py
python3 scripts/deploy.py --project symphony-canary
```

Build or select the reviewed Runtime artifact as appropriate. The supervised
launch below uses the explicit `SYMPHONY_BIN` path.

## 2. Start the Pilot control UI

In one terminal:

```sh
cd /mnt/f/PROJECT-REPOS/symphony/symphony-pilot
python3 scripts/serve_control_ui.py --host 127.0.0.1 --port 8765
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). The browser surface is
read-only; task mutation stays on the trusted CLI.

## 3. Create and queue a task

Use a bounded objective and the registered project:

```sh
python3 scripts/task.py create \
  --project symphony-canary \
  --title "Supervised local canary" \
  --objective "Add a file named symphony-canary.txt containing exactly: SYMPHONY CANARY OK\\n\\nValidate the repository remains otherwise unchanged. Do not publish, merge, or create a PR."

python3 scripts/task.py queue \
  --project symphony-canary \
  --task T-000001
```

Creation must produce `PREPARED`; queueing must perform the ordinary local
`PREPARED -> QUEUED` transition and create the initial workpad. This path does
not call WSL storage, quota admission, storage reservation, or verification.

## 4. Start supervised Runtime

In a second terminal:

```sh
cd /mnt/f/PROJECT-REPOS/symphony/symphony-pilot
SYMPHONY_SUPERVISED_LOCAL=1 \
SYMPHONY_BIN=/mnt/f/PROJECT-REPOS/symphony/symphony-runtime/elixir/bin/symphony \
python3 scripts/project.py --project symphony-canary start
```

The command starts the normal installed `codex app-server` using the
operator's existing authenticated Codex environment and enables the six
project-independent role policies. Runtime dashboard observability is at
[http://127.0.0.1:4041](http://127.0.0.1:4041) while Runtime is running.

## 5. Inspect status and evidence

Open [http://127.0.0.1:8765](http://127.0.0.1:8765), select the project, and
click the task row first. The task detail view's **Execution receipts** surface
shows the resolved workspace, local branch, SQLite-authorized result range,
commits, changed files, unified diff, role history, and publication status.
Use the WSL/CLI commands below as lower-level troubleshooting or independent
verification tools when needed.

```sh
python3 scripts/task.py list --project symphony-canary
python3 scripts/task.py show --project symphony-canary --task T-000001
python3 scripts/task.py blockers --project symphony-canary --task T-000001
```

Inspect the task state, current HEAD, workpad, role runs, findings, blockers,
and timeline in the Pilot UI or `task show`. SQLite is the lifecycle authority;
Runtime is read-only.

## 6. Graceful finish and normal stop

Allow the task to reach `FINAL_MECHANICAL_ACCEPTANCE`. No publication, PR, or
merge is part of this run. Then stop the project normally:

```sh
python3 scripts/project.py --project symphony-canary finish
python3 scripts/project.py --project symphony-canary stop
```

Use `finish` only when the project command reports that the active execution
has completed. A terminal task may be inspected after Runtime stops.

## 7. Emergency stop

`stop-now` is different from normal stop:

```sh
python3 scripts/project.py --project symphony-canary stop-now
```

It is bounded emergency process control for a wedged or unsafe active Runtime.
It is not lifecycle reconciliation, publication, merge, or a reason to delete
SQLite evidence. Inspect the task and any started role run after using it.
