from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
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
        self.assertIn("target_windows_version", contract["informational"])
        self.assertNotIn("product_label", contract["exact_identity"]["windows"])
        self.assertNotIn("windows_version", contract["exact_identity"]["windows"])
        self.assertEqual(contract["disposable_runner"]["labels"][-1], "symphony-disposable")

    def test_host_contract_is_ci_metadata_not_production_deployment_authority(self):
        import deployment_contract
        self.assertNotIn("host-contract/symphony-target-v1.json", deployment_contract.CONTRACT_FILES)
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory)
            for relative in deployment_contract.CONTRACT_FILES:
                target = source / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / relative).read_bytes())
            before = deployment_contract.contract_digest(source)
            host_metadata = source / "host-contract/symphony-target-v1.json"
            host_metadata.parent.mkdir(parents=True, exist_ok=True)
            host_metadata.write_text("{\"informational\":\"changed CI observation\"}\n", encoding="utf-8")
            self.assertEqual(before, deployment_contract.contract_digest(source))

    def test_exact_identity_verifier_requires_every_exact_field(self):
        contract = load_contract()
        expected = contract["exact_identity"]
        self.assertEqual(verify_exact_identity(contract, expected), [])
        altered = json.loads(json.dumps(expected))
        altered["windows"]["build"] = "10.0.22631.6200"
        self.assertIn("windows.build", verify_exact_identity(contract, altered))
        del altered["windows"]["build"]
        self.assertIn("windows.build", verify_exact_identity(contract, altered))

    def test_legacy_windows_labels_are_informational_only(self):
        contract = load_contract()
        observed = json.loads(json.dumps(contract["exact_identity"]))
        observed["windows"].update({
            "product_label": "Windows 11 Pro",
            "windows_version": "23H2",
        })
        self.assertEqual(verify_exact_identity(contract, observed), [])

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
        self.assertIn("run_step8_full_suite.py", workflow)
        self.assertIn("test_helper_compiles_with_strict_linux_warnings", workflow)
        self.assertIn("sudo -E python3 -m unittest", workflow)
        self.assertNotIn("continue-on-error: true", workflow)
        self.assertIn("contents: read", workflow)

    def test_tier_b_is_hosted_windows_and_evidence_bound(self):
        workflow = (ROOT / ".github/workflows/step8-windows-native.yml").read_text()
        self.assertIn("runs-on: windows-2022", workflow)
        self.assertIn("collect_step8_windows_observation.ps1", workflow)
        self.assertIn("--tier windows-native", workflow)
        self.assertIn("continue-on-error: true", workflow)
        self.assertIn("actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683", workflow)
        self.assertIn("contents: read", workflow)

    def test_tier_c_is_guarded_disposable_and_not_pull_request_triggered(self):
        workflow = (ROOT / ".github/workflows/step8-target-twin.yml").read_text()
        trigger = workflow[workflow.index("on:"):workflow.index("permissions:")]
        self.assertNotIn("pull_request", trigger)
        self.assertNotIn("push:", trigger)
        for label in ("self-hosted", "windows", "x64", "symphony-target", "symphony-disposable"):
            self.assertIn(label, workflow)
        self.assertIn("github.repository == 'duck-lint/symphony-pilot'", workflow)
        self.assertIn("disposable target runner marker is absent", workflow)
        self.assertIn("contents: read", workflow)

    def test_tier_c_contains_real_storage_acceptance_probes_and_bounded_cleanup(self):
        workflow = (ROOT / ".github/workflows/step8-target-twin.yml").read_text()
        probe = (ROOT / "scripts/step8_target_twin_probe.py").read_text()
        for token in (
            "step8_target_twin_probe.py", "quota-admit-task", "quota-release-task",
            "workspace_reclaimed", "reservation_after", "LinuxDevicesBefore",
            "LinuxDevicesAfter", "reconciled-detached", "Remove-Item -LiteralPath $env:SYMPHONY_TARGET_VHDX",
        ):
            self.assertIn(token, workflow + probe)
        for token in (
            "task_quota_binding_from_evidence", "create_empty_task_workspace",
            "reclaim_task_workspace", "storage_release_proof_from_evidence",
            "PROJINHERIT", "EDQUOT",
        ):
            self.assertIn(token, probe)

    def test_evidence_writer_binds_sha_and_contract_digest(self):
        writer = (ROOT / "scripts/write_step8_evidence.py").read_text()
        verifier = (ROOT / "scripts/verify_step8_host.py").read_text()
        self.assertIn('"git_sha": git_sha()', writer)
        self.assertIn('evidence.get("git_sha") != git_sha()', writer)
        self.assertIn("contract_digest(CONTRACT_PATH)", writer)
        self.assertIn('"target_contract_sha256"', verifier)

    def test_full_suite_runner_records_bounded_failure_identity(self):
        runner = (ROOT / "scripts/run_step8_full_suite.py").read_text()
        self.assertIn("RecordingResult", runner)
        self.assertIn("result.failure_records", runner)
        self.assertIn('"symphony-pilot-step8-test-result/v1"', runner)
        self.assertIn("::error file=tests::", runner)

    def test_production_paths_do_not_require_observed_platform_patch_versions(self):
        production_files = [
            *(ROOT / "runtime").rglob("*.py"),
            *(ROOT / "scripts").glob("*.py"),
            *(ROOT / "scripts").glob("*.ps1"),
            *(ROOT / "scripts").glob("*.sh"),
            *(ROOT / "provisioning").glob("*.c"),
        ]
        forbidden_observations = (
            "10.0.22631.6199", "2.7.12.0", "6.18.33.2-2",
            "24.04.4 LTS", "13.3.0-6ubuntu2~24.04.1",
            "2.39-0ubuntu8.8", "1.47.0", "2.39.3", "Windows 10 Home",
        )
        for path in production_files:
            text = path.read_text(encoding="utf-8")
            for observed in forbidden_observations:
                self.assertNotIn(observed, text, f"patch identity leaked into production: {path}")

    def test_recovery_operator_is_fixed_path_capability_driven_and_idempotent(self):
        script = (ROOT / "scripts/recover_storage_after_boot.ps1").read_text(encoding="utf-8")
        self.assertIn("param()", script)
        self.assertIn('C:\\ProgramData\\SymphonyPilot\\symphony-storage.vhdx', script)
        self.assertIn('$ExpectedOperatorRoot = Join-Path $ExpectedSourceRoot "scripts"', script)
        self.assertIn('function Invoke-IsolatedAttachOperator', script)
        self.assertIn('"-NoProfile", "-NonInteractive", "-File", $AttachScript', script)
        self.assertIn('"-Operation", "Attach"', script)
        self.assertIn('$start.ArgumentList.Add($argument)', script)
        self.assertIn('Stdout = $stdoutTask.GetAwaiter().GetResult().Trim()', script)
        self.assertIn('Stderr = $stderrTask.GetAwaiter().GetResult().Trim()', script)
        self.assertIn('Status = "reconciliation-required"', script)
        self.assertIn('Get-MountedPoolEvidence -RequireIdentity', script)
        self.assertIn('Invoke-DeployedProvisioner $device', script)
        self.assertIn('Invoke-QuotaPoolVerification', script)
        self.assertIn('storage-domain.identity.json', script)
        self.assertIn('filesystem_uuid -ne $fields["UUID"]', script)
        self.assertNotIn("mkfs.ext4", script)
        self.assertNotIn("New-VHD", script)
        self.assertNotIn("Mount-VHD", script)
        self.assertNotIn("Dismount-VHD", script)
        self.assertLess(script.index("Invoke-ReviewedAttach"), script.index("Invoke-DeployedProvisioner"))
        self.assertLess(script.index("Invoke-DeployedProvisioner"), script.index("Invoke-QuotaPoolVerification"))

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell unavailable")
    def test_isolated_attach_child_process_composition_is_bounded(self):
        script_path = (ROOT / "scripts/recover_storage_after_boot.ps1").as_posix()
        with tempfile.TemporaryDirectory() as directory:
            fixture_root = pathlib.Path(directory)
            success_fixture = fixture_root / "success.ps1"
            success_fixture.write_text(
                "$evidence = [ordered]@{ VhdPath = 'C:\\ProgramData\\SymphonyPilot\\symphony-storage.vhdx'; "
                "VhdType = 'Fixed'; VirtualSizeBytes = 68719476736; ProviderSubtype = 2; "
                "AttachmentAction = 'attached'; LinuxDevice = '/dev/sdf' }\n"
                "$evidence | ConvertTo-Json -Compress\nexit 0\n", encoding="utf-8",
            )
            reconciliation_fixture = fixture_root / "reconciliation.ps1"
            reconciliation_fixture.write_text(
                '$exception = New-Object System.InvalidOperationException -ArgumentList '
                '"attachment reconciliation required before Attach can proceed"\n'
                '$record = New-Object System.Management.Automation.ErrorRecord -ArgumentList @('
                '$exception, "VhdxReconciliationRequired", '
                '[System.Management.Automation.ErrorCategory]::ResourceBusy, "fixed-vhdx")\n'
                'throw $record\n', encoding="utf-8",
            )
            unrelated_fixture = fixture_root / "unrelated.ps1"
            unrelated_fixture.write_text('throw "unrelated terminating failure"\n', encoding="utf-8")
            command = f'''$script = '{script_path}'; . $script
$PowerShell = [IO.Path]::Combine($PSHOME, "pwsh.exe")
$AttachScript = '{success_fixture.as_posix()}'
$success = Invoke-ReviewedAttach
if ($success.Status -ne "attached" -or $success.Evidence.LinuxDevice -ne "/dev/sdf") {{ exit 1 }}
$AttachScript = '{reconciliation_fixture.as_posix()}'
$reconciled = Invoke-ReviewedAttach
if ($reconciled.Status -ne "reconciliation-required") {{ exit 2 }}
$AttachScript = '{unrelated_fixture.as_posix()}'
try {{ Invoke-ReviewedAttach; exit 3 }} catch {{
    if ($_.Exception.Message -notmatch "reviewed VHDX Attach failed") {{ throw }}
}}
Write-Output "Isolated Attach harness: PASS"'''
            result = subprocess.run(
                ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True, text=True, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Isolated Attach harness: PASS", result.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell unavailable")
    def test_recovery_operator_routes_attach_and_existing_mount_without_live_commands(self):
        script_path = (ROOT / "scripts/recover_storage_after_boot.ps1").as_posix()
        command = f'''$script = '{script_path}'; . $script
$global:events = @()
function Assert-Elevated {{}}
function Assert-FixedOperatorPaths {{}}
function Invoke-ReviewedAttach {{
    [pscustomobject]@{{ Status = "attached"; Evidence = [pscustomobject]@{{
        VhdPath = $ExpectedPath; VhdType = "Fixed"; VirtualSizeBytes = $ExpectedBytes;
        ProviderSubtype = 2; AttachmentAction = "attached"; LinuxDevice = "/dev/sdf"
    }} }}
}}
function Get-MountedPoolEvidence {{ param([switch]$RequireIdentity)
    [pscustomobject]@{{ LinuxDevice = "/dev/sdf"; FilesystemUuid = "3fe37adf-5873-4cbe-a656-0820f93def0" }}
}}
function Invoke-DeployedProvisioner {{ param([string]$LinuxDevice) $global:events += "provision:$LinuxDevice" }}
function Invoke-QuotaPoolVerification {{ $global:events += "verify-pool" }}
$first = Invoke-StorageRecovery | ConvertFrom-Json
if ($first.AttachmentRoute -ne "attached" -or ($global:events -join ",") -ne "provision:/dev/sdf,verify-pool") {{ exit 1 }}
function Invoke-ReviewedAttach {{ [pscustomobject]@{{ Status = "reconciliation-required"; Evidence = $null }} }}
$global:events = @()
$second = Invoke-StorageRecovery | ConvertFrom-Json
if ($second.AttachmentRoute -ne "reconciliation-required" -or ($global:events -join ",") -ne "provision:/dev/sdf,verify-pool") {{ exit 2 }}
Write-Output "Recovery route harness: PASS"'''
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Recovery route harness: PASS", result.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell unavailable")
    def test_recovery_operator_rejects_changed_existing_uuid_before_provision(self):
        script_path = (ROOT / "scripts/recover_storage_after_boot.ps1").as_posix()
        command = f'''$script = '{script_path}'; . $script
function Invoke-FixedLinuxCommand {{ param([string[]]$Arguments)
    $output = switch ($Arguments[0]) {{
        "/usr/bin/findmnt" {{ if ($Arguments[3] -eq "TARGET") {{ "/home/duck-lint/symphony-workspaces" }} elseif ($Arguments[3] -eq "SOURCE") {{ "/dev/sdf" }} elseif ($Arguments[3] -eq "FSTYPE") {{ "ext4" }} else {{ "rw,prjquota" }} }}
        "/usr/sbin/blkid" {{ "TYPE=ext4`nLABEL=SYMPHONY-POOL`nUUID=3fe37adf-5873-4cbe-a656-0820f93def0f" }}
        "/usr/sbin/blockdev" {{ "68719476736" }}
        "/usr/sbin/tune2fs" {{ "Filesystem features:    has_journal project quota`nProject quota inode: 12`nReserved block count: 0" }}
        "/usr/bin/cat" {{ '{{"schema":"symphony-pilot-storage-domain/v1","pool_label":"SYMPHONY-POOL","filesystem":"ext4","mount_target":"/home/duck-lint/symphony-workspaces","filesystem_uuid":"00000000-0000-0000-0000-000000000000"}}' }}
    }}
    [pscustomobject]@{{ ExitCode = 0; Output = [string]$output }}
}}
try {{ Get-MountedPoolEvidence -RequireIdentity; exit 1 }} catch {{
    if ($_.Exception.Message -notmatch "UUID differs from the accepted storage identity") {{ throw }}
}}
Write-Output "Changed UUID harness: PASS"'''
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Changed UUID harness: PASS", result.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell unavailable")
    def test_recovery_blockdev_uses_supported_argument_grammar(self):
        script_path = (ROOT / "scripts/recover_storage_after_boot.ps1").as_posix()
        command = f'''$script = '{script_path}'; . $script
$global:invocations = @()
function Invoke-FixedLinuxCommand {{ param([string[]]$Arguments)
    $global:invocations += ,$Arguments
    $output = switch ($Arguments[0]) {{
        "/usr/bin/findmnt" {{
            if ($Arguments[3] -eq "TARGET") {{ "/home/duck-lint/symphony-workspaces" }}
            elseif ($Arguments[3] -eq "SOURCE") {{ "/dev/sdf" }}
            elseif ($Arguments[3] -eq "FSTYPE") {{ "ext4" }}
            else {{ "rw,prjquota" }}
        }}
        "/usr/sbin/blkid" {{ "TYPE=ext4`nLABEL=SYMPHONY-POOL`nUUID=3fe37adf-5873-4cbe-a656-0820f93def0f" }}
        "/usr/sbin/blockdev" {{ "68719476736" }}
        "/usr/sbin/tune2fs" {{ "Filesystem features:    has_journal project quota`nProject quota inode: 12`nReserved block count: 0" }}
        "/usr/bin/cat" {{ $null }}
    }}
    [pscustomobject]@{{ ExitCode = if ($Arguments[0] -eq "/usr/bin/cat") {{ 1 }} else {{ 0 }}; Output = [string]$output }}
}}
Get-MountedPoolEvidence
$blockdev = @($global:invocations | Where-Object {{ $_[0] -eq "/usr/sbin/blockdev" }})
if ($blockdev.Count -ne 1) {{ exit 1 }}
if (($blockdev[0] -join "|") -ne "/usr/sbin/blockdev|--getsize64|/dev/sdf") {{ exit 2 }}
if ($blockdev[0] -contains "--") {{ exit 3 }}
Write-Output "blockdev grammar harness: PASS"'''
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("blockdev grammar harness: PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
