from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "runtime"))

from step8_host_contract import (  # noqa: E402
    CONTRACT_PATH,
    CONTRACT_SCHEMA,
    load_contract,
    verify_exact_identity,
    verify_ubuntu_buildability,
)


class Step8CiContractTests(unittest.TestCase):
    def test_host_contract_is_versioned_and_classifies_properties(self):
        contract = load_contract()
        self.assertEqual(contract["schema"], CONTRACT_SCHEMA)
        self.assertEqual(contract["version"], 1)
        self.assertIn("windows", contract["exact_identity"])
        self.assertIn("ubuntu_linux", contract["compatible_buildability"])
        self.assertIn("target_windows_product_label", contract["informational"])
        self.assertEqual(contract["disposable_runner"]["labels"][-1], "symphony-disposable")

    def test_host_contract_is_in_source_deployment_digest(self):
        import deployment_contract
        self.assertIn("host-contract/symphony-target-v1.json", deployment_contract.CONTRACT_FILES)

    def test_exact_identity_verifier_requires_every_exact_field(self):
        contract = load_contract()
        expected = contract["exact_identity"]
        self.assertEqual(verify_exact_identity(contract, expected), [])
        altered = json.loads(json.dumps(expected))
        altered["windows"]["build"] = "10.0.22631.6200"
        self.assertIn("windows.build", verify_exact_identity(contract, altered))
        del altered["windows"]["build"]
        self.assertIn("windows.build", verify_exact_identity(contract, altered))

    def test_ubuntu_buildability_verifier_accepts_compatible_userspace(self):
        contract = load_contract()
        observed = {
            "architecture": "x86_64",
            "os_release": {"ID": "ubuntu", "VERSION_ID": "24.04"},
            "compiler": {"path": "/usr/bin/cc", "executable": True,
                          "version_output": "gcc (Ubuntu 13.3.0) 13.3.0"},
            "glibc": "ldd (Ubuntu GLIBC 2.39-0ubuntu8.8)",
            "e2fsprogs": "mke2fs 1.47.0",
            "util_linux": {"mount": "util-linux 2.40.2", "findmnt": "util-linux 2.40.2"},
            "quotactl_fd": 443,
        }
        self.assertEqual(verify_ubuntu_buildability(contract, observed), [])
        observed["quotactl_fd"] = 444
        self.assertIn("quotactl_fd", verify_ubuntu_buildability(contract, observed))

    def test_contract_digest_is_stable_and_machine_readable(self):
        self.assertEqual(
            hashlib.sha256(CONTRACT_PATH.read_bytes()).hexdigest(),
            hashlib.sha256(CONTRACT_PATH.read_bytes()).hexdigest(),
        )

    def test_tier_a_workflow_requires_native_build_and_no_relevant_skip(self):
        workflow = (ROOT / ".github/workflows/step8-buildability.yml").read_text()
        self.assertIn("runs-on: ubuntu-24.04", workflow)
        self.assertIn("/usr/bin/cc -std=c11 -O2 -Wall -Wextra -Werror", workflow)
        self.assertIn("python3 -m unittest discover -s tests -v", workflow)
        self.assertIn("test_helper_compiles_with_strict_linux_warnings", workflow)
        self.assertIn("sudo -E python3 -m unittest", workflow)
        self.assertNotIn("continue-on-error: true", workflow)
        self.assertIn("contents: read", workflow)

    def test_tier_b_is_hosted_windows_and_evidence_bound(self):
        workflow = (ROOT / ".github/workflows/step8-windows-native.yml").read_text()
        self.assertIn("runs-on: windows-2022", workflow)
        self.assertIn("collect_step8_windows_observation.ps1", workflow)
        self.assertIn("--tier windows-native", workflow)
        self.assertIn("actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683", workflow)
        self.assertIn("contents: read", workflow)

    def test_tier_c_is_guarded_disposable_and_not_pull_request_triggered(self):
        workflow = (ROOT / ".github/workflows/step8-target-twin.yml").read_text()
        trigger = workflow[workflow.index("on:"):workflow.index("permissions:")]
        self.assertNotIn("pull_request", trigger)
        for label in ("self-hosted", "windows", "x64", "symphony-target", "symphony-disposable"):
            self.assertIn(label, workflow)
        self.assertIn("github.repository == 'duck-lint/symphony-pilot'", workflow)
        self.assertIn("disposable target runner marker is absent", workflow)
        self.assertIn("contents: read", workflow)

    def test_evidence_writer_binds_sha_and_contract_digest(self):
        writer = (ROOT / "scripts/write_step8_evidence.py").read_text()
        verifier = (ROOT / "scripts/verify_step8_host.py").read_text()
        self.assertIn('"git_sha": git_sha()', writer)
        self.assertIn('evidence.get("git_sha") != git_sha()', writer)
        self.assertIn("contract_digest(CONTRACT_PATH)", writer)
        self.assertIn('"target_contract_sha256"', verifier)


if __name__ == "__main__":
    unittest.main()
