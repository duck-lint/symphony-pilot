from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import socket
import tomllib
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
sys.path.insert(0, str(ROOT / "scripts"))

import containment
import host_integration
import outbox
import prepare_workspace as pw
import rulesets
import publication
import project_registry
import runtime_lock
import task_admission


class InfrastructureTests(unittest.TestCase):
    def profile(self, root: pathlib.Path, slug: str = "demo", repository: str = "example/demo", port: int = 4040):
        return pw.Profile(
            slug=slug, repository=repository, git_remote="git@example:" + repository + ".git",
            workspace_root=root / "work", state_root=root / "state", log_root=root / "logs",
            secret_reference="github.token", trusted_dispatchers=("duck-lint",), dispatch_labels=("symphony:auto",),
            blocked_label="symphony:human", service_identity="symphony-pilot-" + slug,
            dashboard_port=port, max_concurrent_agents=1, max_turns=8,
            poll_interval_ms=1000, max_retry_backoff_ms=1000,
            codex_model="gpt-5.6-luna", codex_reasoning_effort="high", toolchain=None,
        )

    def runtime_identity(self):
        item = {"executable": "/reviewed/tool", "version": "tool 1", "sha256": "a" * 64}
        return {"symphony": item, "codex": item, "containment": item}

    def task(self):
        return task_admission.create_task(task_admission.ServerAdmission(
            repository="example/project", project_slug="demo", issue_number=10,
            dispatch_provenance=[{"label": "symphony:auto", "actor": "duck-lint", "event_id": 1, "created_at": "2026-01-01T00:00:00Z"}],
            default_ref="master", base_sha="b" * 40,
            workpad_comment_id=42, runtime_identity=self.runtime_identity(),
        ), task_id="c" * 32)

    def test_canonical_registry_is_arbitrary_and_collision_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            paths = []
            for index in range(5):
                path = root / ("p" + str(index))
                path.mkdir()
                (path / "profile.toml").write_text(
                    f'slug = "p{index}"\nrepository = "owner/r{index}"\n'
                    'git_remote = "git@example:repo.git"\nsecret_reference = "github.token"\ntrusted_dispatchers = ["duck-lint"]\n'
                    'dispatch_labels = ["auto"]\nblocked_label = "human"\n'
                    f'dashboard_port = {4100 + index}\nmax_concurrent_agents = 1\n'
                    'max_turns = 1\npoll_interval_ms = 1000\nmax_retry_backoff_ms = 0\n'
                    'codex_model = "model"\ncodex_reasoning_effort = "high"\n'
                    'storage_pool_bytes = 68719476736\ntask_storage_bytes = 8589934592\n'
                    'task_storage_inodes = 250000\nstorage_emergency_reserve_bytes = 8589934592\n'
                    'storage_emergency_reserve_inodes = 250000\n', encoding="utf-8")
                paths.append(path)
            self.assertEqual(len(project_registry.validate_registry(root)), 5)
            (root / "p5").mkdir()
            (root / "p5" / "profile.toml").write_text((paths[0] / "profile.toml").read_text().replace("p0", "p5"), encoding="utf-8")
            with self.assertRaises(pw.PreparationError):
                project_registry.validate_registry(root)

    def test_registry_supports_empty_and_arbitrary_cardinality(self):
        self.assertEqual(project_registry.validate_profiles([], []), ())
        profiles = tuple(self.profile(pathlib.Path("/tmp"), slug=slug,
                              repository="owner/" + slug, port=4200 + index)
                         for index, slug in enumerate(("alpha", "beta", "gamma", "delta")))
        self.assertEqual([item.slug for item in project_registry.validate_profiles(profiles)],
                         ["alpha", "beta", "gamma", "delta"])

    def test_registry_rejects_repository_service_port_and_namespace_collisions(self):
        alpha = self.profile(pathlib.Path("/tmp"), "alpha", "owner/shared", 4300)
        duplicate_repo = self.profile(pathlib.Path("/tmp"), "beta", "owner/shared", 4301)
        with self.assertRaises(pw.PreparationError):
            project_registry.validate_profiles((alpha, duplicate_repo))
        duplicate_service = self.profile(pathlib.Path("/tmp"), "beta", "owner/beta", 4301)
        duplicate_service = pw.Profile(**{**duplicate_service.__dict__, "service_identity": alpha.service_identity})
        with self.assertRaises(pw.PreparationError):
            project_registry.validate_profiles((alpha, duplicate_service))
        duplicate_port = self.profile(pathlib.Path("/tmp"), "beta", "owner/beta", 4300)
        with self.assertRaises(pw.PreparationError):
            project_registry.validate_profiles((alpha, duplicate_port))
        with mock.patch.object(project_registry, "project_namespaces", side_effect=lambda profile: {
            "root": pathlib.Path("/tmp/shared") if profile.slug == "alpha" else pathlib.Path("/tmp/shared/child")
        }):
            with self.assertRaises(pw.PreparationError):
                project_registry.validate_profiles((alpha, duplicate_port))

    def test_port_domain_and_occupancy_are_fail_closed_without_renumbering(self):
        for port in (0, 1023, 65536):
            with self.assertRaises(pw.PreparationError):
                project_registry.validate_profiles((self.profile(pathlib.Path("/tmp"), port=port),))
        import project
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            port = occupied.getsockname()[1]
            with self.assertRaises(pw.PreparationError):
                project.ensure_dashboard_port_available(port)

    def test_start_preflights_runtime_and_containment_before_secret(self):
        import project
        with tempfile.TemporaryDirectory() as directory:
            profile = self.profile(pathlib.Path(directory), port=4400)
            lock = {"schema": runtime_lock.LOCK_SCHEMA, **self.runtime_identity()}
            (pathlib.Path(directory) / "state").mkdir()
            (pathlib.Path(directory) / "state" / "runtime-lock.json").write_text(json.dumps(lock), encoding="utf-8")
            order = []
            identity = runtime_lock.ExecutableIdentity("/reviewed/tool", "tool 1", "a" * 64)
            containment_identity = mock.Mock(executable="/reviewed/unshare", version="unshare 1", sha256="b" * 64)
            with mock.patch.object(project, "verify_deployment", return_value={
                       "root": pathlib.Path(directory), "deployment_identity": "deployment",
                       "profile_sha256": "profile", "dashboard_url": "http://127.0.0.1:4400"}), \
                 mock.patch.object(project, "ensure_dashboard_port_available"), \
                 mock.patch.object(project, "resolve_symphony_binary", side_effect=lambda: order.append("symphony") or "/reviewed/tool"), \
                 mock.patch.object(project.shutil, "which", side_effect=lambda name: order.append(name) or "/reviewed/codex"), \
                 mock.patch.object(project, "identify", return_value=identity), \
                 mock.patch.object(project, "verify_entry"), \
                 mock.patch.object(project, "backend_identity", side_effect=lambda: order.append("containment") or containment_identity), \
                 mock.patch.object(project, "require_execution_capability", side_effect=lambda: order.append("capability") or (_ for _ in ()).throw(containment.ContainmentError("blocked", "fixture"))), \
                 mock.patch.object(project, "read_secret", side_effect=AssertionError("secret read before containment")):
                self.assertEqual(project.start(profile), 78)
            self.assertLess(order.index("containment"), order.index("capability"))
            self.assertNotIn("github", order)

    @unittest.skipIf(os.name == "nt", "PID identity is a native Linux/WSL contract")
    def test_pid_identity_and_recovery_records_fail_closed(self):
        import process_identity
        import project
        identity = process_identity.capture(os.getpid())
        self.assertIsNotNone(identity)
        self.assertTrue(process_identity.matches(identity))
        self.assertFalse(process_identity.matches(dict(identity, start_time="reused")))
        with tempfile.TemporaryDirectory() as directory:
            state = pathlib.Path(directory) / "symphony.pid"
            state.write_text("{}", encoding="utf-8")
            with mock.patch.object(project, "recovery_state_path", return_value=state):
                with self.assertRaises(pw.PreparationError):
                    project.read_recovery_state("removed-project")
            awake = pathlib.Path(directory) / "awake.json"
            awake.write_text("{}", encoding="utf-8")
            with self.assertRaises(pw.PreparationError):
                host_integration._read_awake_state(awake)

    def test_manifest_inventory_rejects_tampering_and_unexpected_files(self):
        import project
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "runtime").mkdir()
            (root / "runtime" / "contract.py").write_text("ok", encoding="utf-8")
            manifest_path = root / "DEPLOYMENT.json"
            manifest = {"files": {"runtime/contract.py": project.file_digest(root / "runtime/contract.py")}}
            project.verify_manifest(root, manifest_path, manifest)
            (root / "runtime" / "contract.py").write_text("tampered", encoding="utf-8")
            with self.assertRaises(ValueError):
                project.verify_manifest(root, manifest_path, manifest)

    def test_role_policy_pack_and_lifecycle_contract_remain_intact(self):
        policies = sorted((ROOT / "workflow" / "agents").glob("*.toml"))
        self.assertEqual({path.stem for path in policies},
                         {"project-manager", "planner", "implementer", "reviewer", "adversary", "archivist"})
        parsed = {path.stem: tomllib.loads(path.read_text(encoding="utf-8")) for path in policies}
        self.assertIn("mutating worker", parsed["implementer"]["description"].lower())
        self.assertEqual(parsed["reviewer"]["sandbox_mode"], "read-only")
        self.assertEqual(parsed["adversary"]["sandbox_mode"], "read-only")
        policy = (ROOT / "workflow" / "architect_policy.md").read_text(encoding="utf-8")
        self.assertIn("fresh implementer", policy.lower())
        self.assertIn("adversary", policy.lower())
        self.assertIn("older head", policy.lower())
        self.assertIn("do not auto-merge", policy.lower())

    def test_protection_preflight_consumes_real_ruleset_endpoint_shape(self):
        import project
        profile = self.profile(pathlib.Path("/tmp"))
        ruleset = {"id": 7, "target": "branch", "enforcement": "active",
                   "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
                   "bypass_actors": [], "rules": [{"type": "pull_request"}]}
        with mock.patch.object(project, "github", side_effect=[{"default_branch": "master"}, [{"id": 7}], ruleset]) as api:
            self.assertEqual(project.branch_protection_preflight(profile, "token"), ruleset)
        self.assertEqual(api.call_args_list[1].args[3], "/rulesets?includes_parents=true&per_page=100&page=1")
        self.assertEqual(api.call_args_list[2].args[3], "/rulesets/7?includes_parents=true")

    @unittest.skipUnless(os.name == "nt", "native Windows namespace guard only")
    def test_windows_never_fabricates_physical_wsl_namespace(self):
        self.assertEqual(pw.host_namespace_root(), pathlib.PurePosixPath("<wsl-home>"))
        with self.assertRaises(pw.PreparationError):
            pw.require_physical_namespace(pathlib.PurePosixPath("/home/operator"))

    def test_profiles_have_no_credential_values(self):
        schema = json.loads((ROOT / "schemas/project-profile.schema.json").read_text())
        self.assertFalse(schema["additionalProperties"])
        self.assertNotIn("token", schema["properties"])

    def test_task_admission_ignores_prose_and_derives_branch(self):
        task = self.task()
        self.assertEqual(task["issue_branch"], "codex/gh-10-cccccccccccc")
        with self.assertRaises(task_admission.TaskAdmissionError):
            task_admission.validate_task_record(dict(task, issue_branch="master"))

    def test_task_record_is_strict_and_host_owned(self):
        task = self.task()
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "tasks" / "GH-10" / "task.json"
            task_admission.write_task(path, task)
            self.assertEqual(task_admission.read_task(path), task)
            with self.assertRaises(task_admission.TaskAdmissionError):
                task_admission.validate_task_record(dict(task, attacker_control="master"))

    def test_outbox_and_ruleset_fail_closed(self):
        task = self.task()
        request = {"schema": outbox.OUTBOX_SCHEMA, "task_id": task["task_id"],
                   "head": "d" * 40, "workpad_body": "ok", "disposition": "ready_for_human_merge", "summary": "ok"}
        self.assertEqual(outbox.validate_request(request, task), request)
        with self.assertRaises(outbox.OutboxError):
            outbox.validate_request(dict(request, branch="master"), task)
        good = {"id": 7, "target": "branch", "enforcement": "active",
                "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
                "bypass_actors": [], "rules": [{"type": "pull_request"}]}
        self.assertEqual(rulesets.require_default_branch_ruleset([good], "master"), good)
        with self.assertRaises(rulesets.RulesetError):
            rulesets.require_default_branch_ruleset([dict(good, bypass_actors=[{"actor_type": "Integration"}])], "master")

    def test_workflow_removes_parent_write_root_and_open_network(self):
        from render_workflow import render
        profile = self.profile(pathlib.Path("/tmp"))
        with tempfile.TemporaryDirectory() as directory:
            policy = pathlib.Path(directory) / "policy.md"
            policy.write_text("policy\n", encoding="utf-8")
            rendered = render(profile, pathlib.Path(directory), policy)
        self.assertIn("type: externalSandbox", rendered)
        self.assertIn("networkAccess: restricted", rendered)
        self.assertNotIn("networkAccess: true", rendered)
        self.assertNotIn(str(profile.workspace_root), rendered.split("turn_sandbox_policy:", 1)[1])

    def test_launcher_is_minimal_and_fail_closed(self):
        text = (ROOT / "runtime/launch_codex.sh").read_text(encoding="utf-8")
        self.assertNotIn("ORIGINAL_CODEX_HOME", text)
        self.assertNotIn("ln -s", text)
        self.assertIn("CODEX_API_KEY", text)
        self.assertIn("exit 78", text)

    def test_containment_backend_has_explicit_auth_blocker(self):
        with mock.patch.object(containment, "require_backend", return_value=mock.Mock()):
            with self.assertRaises(containment.ContainmentError) as raised:
                containment.require_execution_capability()
        self.assertEqual(raised.exception.kind, "codex_auth_boundary")

    def test_publication_drops_task_credential_channels(self):
        with mock.patch.dict("os.environ", {"SSH_AUTH_SOCK": "/tmp/socket", "GIT_CONFIG_GLOBAL": "bad"}, clear=True):
            env = publication.safe_git_environment()
        self.assertNotIn("SSH_AUTH_SOCK", env)
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")

    def test_runtime_lock_is_strict(self):
        lock = {"schema": runtime_lock.LOCK_SCHEMA, **self.runtime_identity()}
        self.assertEqual(runtime_lock.validate_lock(lock), lock)
        with self.assertRaises(runtime_lock.RuntimeLockError):
            runtime_lock.validate_lock(dict(lock, extra=True))

    def test_deployment_dry_run_is_available_without_secret(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/deploy.py"),
                                 "--project", "cleanroom", "--dry-run"],
                                text=True, capture_output=True, check=True)
        self.assertIn('"profile": "cleanroom"', result.stdout)
        import deploy
        summary = json.loads(result.stdout)
        self.assertEqual(
            summary["files"],
            len(deploy.DEPLOYED_RUNTIME_FILES) + len(deploy.ROLE_POLICY_FILES) + 3,
        )

    def test_deployment_stages_manifest_covered_wsl_supervisor(self):
        import deploy

        def clean_git_result(args, **kwargs):
            stdout = "a" * 40 + "\n" if args[1:3] == ["rev-parse", "HEAD"] else ""
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(deploy.subprocess, "run", side_effect=clean_git_result):
            target = deploy.deploy(ROOT / "projects/symphony-canary/profile.toml",
                                   pathlib.Path(directory) / "deployment", False)
            supervisor = target / "runtime/wsl_contained_exec.py"
            manifest = json.loads((target / "DEPLOYMENT.json").read_text(encoding="utf-8"))
            self.assertTrue(supervisor.is_file())
            self.assertIn("runtime/wsl_contained_exec.py", manifest["files"])
            self.assertIn("runtime/containment.py", manifest["files"])

    def test_python_and_shell_contracts_compile(self):
        result = subprocess.run([sys.executable, "-m", "compileall", "-q", "runtime", "scripts", "tests"],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
