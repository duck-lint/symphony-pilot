from __future__ import annotations

import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class WebUiObservabilityTests(unittest.TestCase):
    source = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    def test_architect_is_presented_as_orchestrator_activity(self):
        self.assertIn('const roleRuns=runs.filter(run=>run.role!=="ARCHITECT")', self.source)
        self.assertIn('const architectRuns=runs.filter(run=>run.role==="ARCHITECT")', self.source)
        self.assertIn("Role execution", self.source)
        self.assertIn("Orchestrator activity", self.source)
        self.assertIn("Architect turns are lifecycle and orchestration passes", self.source)

    def test_retry_and_failure_history_remain_visible(self):
        self.assertIn("run.result_summary||\"—\"", self.source)
        self.assertIn("statusClass(run.status)", self.source)
        self.assertIn("Attempt", self.source)
        self.assertIn("Turn", self.source)

    def test_role_history_is_deterministically_chronological(self):
        self.assertIn("started_at||\"\"", self.source)
        self.assertIn("String(left.id||\"\").localeCompare", self.source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
