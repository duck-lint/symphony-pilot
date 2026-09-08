"""Topology-independent workspace boundary contracts."""
from __future__ import annotations

import pathlib
import tempfile
import unittest
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "runtime"))
import workspace_boundary as boundary


class WorkspaceBoundaryTests(unittest.TestCase):
    def test_task_workspace_is_exact_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            project = pathlib.Path(directory) / "workspaces" / "demo"
            project.parent.mkdir()
            workspace = boundary.create_empty_task_workspace(project, "T-000001")
            self.assertEqual(workspace, project / "T-000001")
            self.assertEqual(boundary.create_empty_task_workspace(project, "T-000001"), workspace)
            (workspace / "unexpected").write_text("evidence", encoding="utf-8")
            with self.assertRaises(boundary.WorkspaceBoundaryError):
                boundary.create_empty_task_workspace(project, "T-000001")

    def test_reclaim_rejects_symlink_without_mutating_outside(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "workspaces"
            task = root / "demo" / "T-000001"
            task.mkdir(parents=True)
            outside = pathlib.Path(directory) / "outside"
            outside.mkdir()
            try:
                (task / "escape").symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symbolic links are unavailable")
            with self.assertRaises(boundary.WorkspaceBoundaryError):
                boundary.reclaim_task_workspace(root, "demo", "T-000001")
            self.assertTrue(outside.exists())


if __name__ == "__main__":
    unittest.main()
