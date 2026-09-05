#!/usr/bin/env python3
"""Verify Pilot-produced rows through the pinned Runtime adapter and scheduler.

Run on Linux with Runtime's mise tools installed. No orchestrator process or
agent starts; the real scheduling predicate is invoked against empty state.
Example: python3 scripts/verify_step6_pair.py ../symphony-runtime RUNTIME_SHA
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import tempfile

from generate_step6_fixture import generate

ROOT = pathlib.Path(__file__).resolve().parents[1]
ACTIVE = ["QUEUED", "PLANNED", "IMPLEMENTED", "REVIEW", "ADVERSARIAL_REVIEW",
          "FINAL_MECHANICAL_ACCEPTANCE"]
PROBE = '''
alias SymphonyElixir.{Workflow, Config, Orchestrator}
alias SymphonyElixir.Tracker.SQLite.Adapter
[database, workflow] = System.argv()
{:ok, _} = Application.ensure_all_started(:exqlite)
:ok = Workflow.set_workflow_file_path(workflow)
settings = Config.settings!().tracker
{:ok, rows} = Adapter.fetch_issues_by_states_for_test(
  ["PREPARED", "ARCHIVIST" | settings.active_states], settings)
state = %Orchestrator.State{max_concurrent_agents: 1}
expected = Map.new(1..9, fn n -> {"T-" <> String.pad_leading(to_string(n), 6, "0"), n in 2..7} end)
true = length(rows) == 9
Enum.each(rows, fn row ->
  dispatch = Orchestrator.should_dispatch_issue_for_test(row, state)
  true = dispatch == Map.fetch!(expected, row.identifier)
  true = row.dispatchable == (row.identifier != "T-000009")
  IO.puts("#{row.identifier} #{row.state} #{if dispatch, do: "yes", else: "no"}")
end)
{:ok, []} = Adapter.fetch_issues_by_ids_for_test(
  ["a0000000-0000-0000-0000-000000000000"], settings)
IO.puts("other-project no")
IO.puts("EXACT_PAIR_COMPATIBILITY_PASSED " <> database)
'''


def head(root: pathlib.Path) -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", type=pathlib.Path)
    parser.add_argument("runtime_sha")
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    if head(runtime) != args.runtime_sha:
        raise SystemExit("Runtime candidate differs from required SHA")
    for root in (runtime, ROOT):
        if subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip():
            raise SystemExit(f"source must be clean: {root}")
    with tempfile.TemporaryDirectory(prefix="symphony-step6-pair-") as directory:
        temporary = pathlib.Path(directory)
        database = temporary / "control.sqlite3"
        generate(database)
        workflow = temporary / "WORKFLOW.md"
        workflow.write_text("---\ntracker:\n  kind: sqlite\n  project_slug: alpha\n"
                            f"  database_path: {json.dumps(str(database))}\n"
                            f"  active_states: {json.dumps(ACTIVE)}\n"
                            "  terminal_states: [READY_FOR_HUMAN_MERGE]\n"
                            "agent:\n  max_concurrent_agents: 1\n---\nPair verification.\n")
        probe = temporary / "verify.exs"
        probe.write_text(PROBE)
        subprocess.run(["mise", "exec", "--", "mix", "run", "--no-start", str(probe),
                        str(database), str(workflow)], cwd=runtime / "elixir",
                       env={**os.environ, "MIX_ENV": "test"}, check=True)
    print(json.dumps({"runtime": head(runtime), "pilot": head(ROOT)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
