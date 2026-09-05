from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

import wsl_contained_exec


class QuotaHelperContractTests(unittest.TestCase):
    def test_source_identity_is_pinned_by_supervisor(self):
        source = ROOT / "provisioning" / "quota-admit-task.c"
        self.assertEqual(
            hashlib.sha256(source.read_bytes()).hexdigest(),
            wsl_contained_exec.QUOTA_HELPER_SOURCE_SHA256,
        )

    def test_provisioning_recipe_is_fixed_and_not_a_command_broker(self):
        recipe = (ROOT / "scripts" / "provision_storage_domain.sh").read_text()
        self.assertIn("mkfs.ext4 -L \"$POOL_LABEL\" -m 0 -i 65536 -I 256 -J size=64", recipe)
        self.assertIn("-O project,quota -E quotatype=prjquota", recipe)
        self.assertIn("mount -t ext4 -o prjquota", recipe)
        self.assertIn('chown duck-lint:duck-lint "$POOL_ROOT"', recipe)
        self.assertIn('chmod 0750 "$POOL_ROOT"', recipe)
        self.assertIn("/etc/fstab", recipe)
        self.assertNotIn("/etc/fstab.d", recipe)
        self.assertIn("f_bavail", recipe)
        self.assertIn("f_favail", recipe)
        self.assertIn("quota-admit-task.c", recipe)
        self.assertIn("ACTUAL_SOURCE_SHA256", recipe)
        self.assertIn("EXPECTED_SOURCE_SHA256", recipe)
        self.assertIn("64 * 1024 * 1024 * 1024", recipe)
        self.assertIn("/home/duck-lint/symphony-workspaces", recipe)
        self.assertIn("/dev/sdd", recipe)
        self.assertIn("POOL_LABEL=SYMPHONY-POOL", recipe)
        self.assertIn('DEVICE_LABEL=$(blkid -o value -s LABEL "$POOL_DEVICE")', recipe)
        self.assertIn('if [ -z "$DEVICE_TYPE" ]; then', recipe)
        self.assertIn('if [ -e "$STORAGE_IDENTITY" ]; then', recipe)
        self.assertIn('if ! cmp -s "$FSTAB_TMP" "$FSTAB"', recipe)
        self.assertIn("--verify-deployment", recipe)
        self.assertIn("/usr/bin/python3 -B", recipe)
        self.assertIn("/var/lib/symphony-pilot/storage-domain.identity.json", recipe)
        helper = (ROOT / "provisioning" / "quota-admit-task.c").read_text()
        self.assertNotIn("system(", helper)
        self.assertNotIn("popen(", helper)
        self.assertNotIn("execvp(", helper)
        self.assertIn("FS_IOC_FSSETXATTR", helper)
        self.assertIn("FS_XFLAG_PROJINHERIT", helper)
        self.assertIn("Q_GETQUOTA", helper)
        self.assertIn("Q_SETQUOTA", helper)
        self.assertIn("struct dqblk", helper)
        self.assertIn("SYS_quotactl_fd", helper)
        self.assertNotIn("Q_XGETQUOTA", helper)
        self.assertNotIn("Q_XSETQLIM", helper)

    def test_task_helper_proves_project_inheritance(self):
        helper = (ROOT / "provisioning" / "quota-admit-task.c").read_text()
        self.assertIn("attrs->fsx_xflags |= FS_XFLAG_PROJINHERIT", helper)
        self.assertIn("attrs.fsx_projid != project_id", helper)
        self.assertIn("!(attrs.fsx_xflags & FS_XFLAG_PROJINHERIT)", helper)
        self.assertIn("inheritance_probe", helper)
        self.assertNotIn("project_fd < 0 && create", helper)
        self.assertNotIn("task_fd < 0 && create", helper)

    def test_fixed_vhdx_operator_contract_has_no_growth_or_generic_broker(self):
        recipe = (ROOT / "scripts" / "provision_storage_vhdx.ps1").read_text()
        self.assertIn("New-VHD", recipe)
        self.assertIn("-Fixed", recipe)
        self.assertIn("64GB", recipe)
        self.assertIn('VhdType -ne "Fixed"', recipe)
        self.assertIn('ValidateSet("Attach", "Detach")', recipe)
        self.assertIn('"--mount", $ExpectedPath, "--vhd", "--bare"', recipe)
        self.assertIn('"--unmount", $ExpectedPath', recipe)
        self.assertIn("SERIAL,WWN,MODEL", recipe)
        self.assertIn("Get-LinuxWholeDiskEvidence", recipe)
        self.assertIn("exactly one new 64-GiB Linux disk", recipe)
        self.assertIn("malformed JSON", recipe)
        self.assertIn("conflicting attachment", recipe)
        self.assertNotIn("Mount-VHD", recipe)
        self.assertNotIn("Dismount-VHD", recipe)
        self.assertNotIn("PHYSICALDRIVE", recipe)
        self.assertNotIn("FileSize -ne", recipe)
        self.assertNotIn("Resize-VHD", recipe)
        self.assertNotIn("Invoke-Expression", recipe)

    def test_wsl_vhd_capability_uses_help_grammar_not_exit_status(self):
        recipe = (ROOT / "scripts" / "provision_storage_vhdx.ps1").read_text()
        capability_start = recipe.index("function Assert-WslVhdCapability")
        parser_start = recipe.index("function Test-WslOptionToken")
        capability = recipe[capability_start:parser_start]
        self.assertIn('Invoke-WslText @("--help")', capability)
        self.assertIn("Test-WslVhdHelpGrammar", capability)
        self.assertNotIn("ExitCode -ne 0", capability)
        attach_main = recipe[recipe.index('Ensure-OperatorStateNamespace ($Operation -eq "Attach")'):]
        self.assertLess(
            attach_main.index("Assert-WslVhdCapability"),
            attach_main.index("Get-VhdEvidence $true"),
        )

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell unavailable")
    def test_wsl_vhd_help_parser_accepts_nonzero_help_and_rejects_ambiguous_grammar(self):
        recipe = (ROOT / "scripts" / "provision_storage_vhdx.ps1").read_text()
        parser_start = recipe.index("function Test-WslOptionToken")
        parser_end = recipe.index("function Throw-ReconciliationRequired")
        parser = recipe[parser_start:parser_end]
        capability_start = recipe.index("function Assert-WslVhdCapability")
        capability = recipe[capability_start:parser_start]
        command = f'''$ErrorActionPreference = "Stop"
function Invoke-WslText {{
    [pscustomobject]@{{ ExitCode = -1; Output = $script:helpText }}
}}
{parser}
{capability}
$complete = @'
Usage: wsl [options]
  --mount <Disk>
      --vhd
      --bare
  --unmount <Disk>
  --import <Distribution> <InstallLocation> [options]
      --vhd
'@
$script:helpText = -join ($complete.ToCharArray() | ForEach-Object {{ [string]$_ + [char]0 }})
Assert-WslVhdCapability
$script:helpText = $complete
if (-not (Test-WslVhdHelpGrammar $complete)) {{ exit 1 }}
if (Test-WslVhdHelpGrammar ($complete -replace '  --mount <Disk>', '  --mountx <Disk>')) {{ exit 1 }}
if (Test-WslVhdHelpGrammar ($complete -replace '      --vhd', '      --vhx')) {{ exit 1 }}
if (Test-WslVhdHelpGrammar ($complete -replace '      --bare', '      --barx')) {{ exit 1 }}
if (Test-WslVhdHelpGrammar ($complete -replace '  --unmount <Disk>', '  --unmountx <Disk>')) {{ exit 1 }}
$unrelated = @'
Usage: wsl [options]
  --mount <Disk>
      attaches a disk
  --unmount <Disk>
  --import <Distribution> <InstallLocation> [options]
      --vhd
      --bare
'@
if (Test-WslVhdHelpGrammar $unrelated) {{ exit 1 }}
if (Test-WslVhdHelpGrammar "") {{ exit 1 }}
"WSL help grammar parser: PASS"
'''
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WSL help grammar parser: PASS", result.stdout)

    def test_vhdx_operator_state_acl_contract_is_explicit(self):
        recipe = (ROOT / "scripts" / "provision_storage_vhdx.ps1").read_text()
        self.assertIn("Ensure-OperatorStateNamespace", recipe)
        self.assertIn("Assert-OperatorStateAcl", recipe)
        self.assertIn("Set-OperatorStateAcl", recipe)
        self.assertIn("Get-Acl", recipe)
        self.assertIn("Set-Acl", recipe)
        self.assertIn("S-1-5-32-544", recipe)
        self.assertIn("S-1-5-18", recipe)
        self.assertIn("AreAccessRulesProtected", recipe)
        self.assertIn("SetAccessRuleProtection($true, $false)", recipe)
        self.assertIn("FileSystemRights]::FullControl", recipe)
        self.assertIn("NTAccount", recipe)
        self.assertIn("IdentityReference", recipe)
        self.assertIn("IsNullOrWhiteSpace", recipe)
        self.assertIn("Throw-ReconciliationRequired", recipe)
        self.assertIn("VhdxReconciliationRequired", recipe)
        self.assertIn("attachment reconciliation required before Attach", recipe)
        self.assertIn("Read-AttachmentState -AllowInvalid", recipe)
        self.assertIn("Remove-AttachmentState", recipe)
        self.assertIn("AttachmentCacheState", recipe)
        self.assertIn("invalid-cache", recipe)
        self.assertIn("[IO.File]::Delete($AttachmentStatePath)", recipe)
        self.assertIn("$isReparse", recipe)
        self.assertIn("VHDX attachment state is not a normal non-reparse file", recipe)
        attach_body = recipe[recipe.index('if ($Operation -eq "Attach")'):]
        self.assertNotIn('"already-attached"', attach_body)
        self.assertIn("Move-Item -LiteralPath $temporary -Destination $AttachmentStatePath -Force", recipe)

    @unittest.skipUnless(sys.platform.startswith("win") and shutil.which("pwsh"),
                         "Windows PowerShell unavailable")
    def test_vhdx_operator_sid_normalization_uses_real_acl_representations(self):
        recipe = (ROOT / "scripts" / "provision_storage_vhdx.ps1").read_text()
        convert_start = recipe.index("function ConvertTo-SidValue")
        assert_start = recipe.index("function Assert-OperatorStateAcl")
        set_start = recipe.index("function Set-OperatorStateAcl")
        ensure_start = recipe.index("function Ensure-OperatorStateNamespace")
        functions = (
            recipe[convert_start:assert_start]
            + recipe[assert_start:set_start]
            + recipe[set_start:ensure_start]
        )
        command = f'''$ErrorActionPreference = "Stop"
$OperatorAdminSid = New-Object System.Security.Principal.SecurityIdentifier("S-1-5-32-544")
$OperatorSystemSid = New-Object System.Security.Principal.SecurityIdentifier("S-1-5-18")
{functions}
$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ("symphony-vhdx-acl-test-" + [guid]::NewGuid().ToString("N"))
try {{
    New-Item -ItemType Directory -Path $temporaryRoot | Out-Null
    $acl = Get-Acl -LiteralPath $temporaryRoot
    $ownerSid = ConvertTo-SidValue $acl.Owner
    $ownerAccount = New-Object -TypeName System.Security.Principal.NTAccount -ArgumentList ([string]$acl.Owner)
    $accountSid = ConvertTo-SidValue $ownerAccount
    $sidObject = New-Object -TypeName System.Security.Principal.SecurityIdentifier -ArgumentList $ownerSid
    $identityReferenceSid = ConvertTo-SidValue $acl.Access[0].IdentityReference
    $builtinAdminSid = ConvertTo-SidValue "BUILTIN\\Administrators"
    if ($accountSid -ne $ownerSid -or
        (ConvertTo-SidValue $sidObject) -ne $ownerSid -or
        [string]::IsNullOrWhiteSpace($identityReferenceSid) -or
        $builtinAdminSid -ne "S-1-5-32-544") {{ exit 1 }}
    try {{
        Set-OperatorStateAcl $temporaryRoot $true
        $reviewedAcl = Get-Acl -LiteralPath $temporaryRoot
        if ((ConvertTo-SidValue $reviewedAcl.Owner) -ne "S-1-5-32-544") {{ exit 1 }}
        Assert-OperatorStateAcl $temporaryRoot $true
    }} catch {{
        if ($_.Exception.Message -notmatch "Access is denied|not allowed to be the owner|UnauthorizedAccessException") {{ throw }}
        "ACL shape smoke skipped: elevation unavailable"
    }}
    try {{ ConvertTo-SidValue ""; exit 1 }} catch {{
        if ($_.Exception.Message -notmatch "operator-state ACL contains an unresolvable identity") {{ exit 1 }}
    }}
    try {{ ConvertTo-SidValue "NoSuchDomain\\NoSuchAccount_987654"; exit 1 }} catch {{
        if ($_.Exception.Message -notmatch "operator-state ACL contains an unresolvable identity") {{ exit 1 }}
    }}
    "ACL normalization smoke: PASS"
}}
finally {{
    if (Test-Path -LiteralPath $temporaryRoot) {{ Remove-Item -LiteralPath $temporaryRoot -Recurse -Force }}
}}'''
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0 and (
            "Access is denied" in result.stderr or
            "UnauthorizedAccessException" in result.stderr
        ):
            self.skipTest("temporary ACL smoke test requires elevation")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ACL normalization smoke: PASS", result.stdout)

    def test_fixed_vhdx_recovery_contract_is_bounded_and_evidence_driven(self):
        recipe = (ROOT / "scripts" / "provision_storage_vhdx.ps1").read_text()
        self.assertIn("Resolve-DetachReconciliation", recipe)
        self.assertIn("reconciled-detached", recipe)
        self.assertIn("UnmountExitCode", recipe)
        self.assertIn("LinuxDevicesBefore", recipe)
        self.assertIn("LinuxDevicesAfter", recipe)
        self.assertIn("Get-ChildItem -LiteralPath $ExpectedParent -Force", recipe)
        self.assertIn("PSIsContainer", recipe)
        self.assertIn("ReparsePoint", recipe)
        self.assertIn("PSObject.Properties.Name", recipe)
        self.assertIn("GetFileName($AttachmentStatePath)", recipe)
        self.assertIn("an untracked exact-size Linux disk is a conflicting attachment", recipe)
        self.assertIn('Invoke-WslText @("--unmount", $ExpectedPath)', recipe)
        self.assertLess(
            recipe.rfind("Resolve-DetachReconciliation"),
            recipe.rfind("Remove-AttachmentState"),
        )

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell unavailable")
    def test_vhdx_detach_reconciler_covers_recovery_and_conflict_evidence(self):
        recipe = (ROOT / "scripts" / "provision_storage_vhdx.ps1").read_text()
        helper_start = recipe.index("function Get-LinuxDeviceIdentityKey")
        helper_end = recipe.index("function Get-LinuxWholeDiskEvidence")
        start = recipe.index("function Resolve-DetachReconciliation")
        end = recipe.index("function Write-AttachmentState")
        reconciler = recipe[start:end]
        identity_helper = recipe[helper_start:helper_end]
        prefix = "$ExpectedBytes = 64GB\n" + identity_helper + reconciler

        def run(before, after, exit_code, expect_success):
            def evidence(devices):
                return "@(" + ",".join(
                    "[pscustomobject]@{SizeBytes=64GB; LinuxDevice='%s'; Type='disk'; Serial=''; Wwn=''; Model=''}" % device
                    for device in devices
                ) + ")"

            command = prefix + "\ntry {\n" + (
                "$result = Resolve-DetachReconciliation %s %s %d\n"
                "$result | ConvertTo-Json -Compress\n"
            ) % (evidence(before), evidence(after), exit_code) + (
                "exit 0\n} catch { exit 1 }"
                if expect_success else
                "exit 1\n} catch { exit 0 }"
            )
            result = subprocess.run(
                ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

        self.assertIn('"Action":"reconciled-detached"', run(["/dev/sdb", "/dev/sdc"], ["/dev/sdc"], 0, True))
        self.assertIn('"Action":"reconciled-detached"', run(["/dev/sdb", "/dev/sdc", "/dev/sde"], ["/dev/sdc", "/dev/sde"], 0, True))
        self.assertIn('"Action":"already-detached"', run([], [], 1, True))
        run(["/dev/sdc"], [], 1, False)
        run(["/dev/sdb", "/dev/sdc"], [], 0, False)
        run(["/dev/sdb"], ["/dev/sdc"], 0, False)
        run(["/dev/sdc"], ["/dev/sdc"], 1, False)
        run(["/dev/sdc"], ["/dev/sdc"], 0, False)

    def test_vhdx_operator_contract_is_in_source_digest(self):
        import deployment_contract
        self.assertIn("scripts/provision_storage_vhdx.ps1", deployment_contract.CONTRACT_FILES)
        before = deployment_contract.contract_digest(ROOT)
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory)
            for relative in deployment_contract.CONTRACT_FILES:
                target = source / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / relative).read_bytes())
            changed = source / "scripts/provision_storage_vhdx.ps1"
            changed.write_bytes(changed.read_bytes() + b"\n# contract mutation\n")
            self.assertNotEqual(before, deployment_contract.contract_digest(source))

    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("cc"),
                         "native Linux compiler unavailable")
    def test_fsxattr_transition_uses_original_kernel_state(self):
        source = (ROOT / "provisioning" / "quota-admit-task.c").as_posix()
        harness = f'''#define main quota_helper_original_main
#include "{source}"
#undef main
int main(void) {{
    struct fsxattr fresh = {{ .fsx_projid = 0, .fsx_xflags = 0x100 }};
    if (!prepare_project_attributes(&fresh, 1000001) ||
        fresh.fsx_projid != 1000001 ||
        !(fresh.fsx_xflags & FS_XFLAG_PROJINHERIT) ||
        !(fresh.fsx_xflags & 0x100)) return 1;
    if (prepare_project_attributes(&fresh, 1000001)) return 2;
    struct fsxattr wrong = {{ .fsx_projid = 1000002, .fsx_xflags = 0 }};
    if (prepare_project_attributes(&wrong, 1000001) || wrong.fsx_projid != 1000002) return 3;
    return 0;
}}
'''
        with tempfile.TemporaryDirectory() as directory:
            harness_path = pathlib.Path(directory) / "fsxattr-transition.c"
            output = pathlib.Path(directory) / "fsxattr-transition"
            harness_path.write_text(harness, encoding="utf-8")
            compile_result = subprocess.run(
                ["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                 str(harness_path), "-o", str(output)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
            result = subprocess.run([str(output)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_provisioning_source_digest_is_verified_before_compile(self):
        recipe = (ROOT / "scripts" / "provision_storage_domain.sh").read_text()
        self.assertLess(recipe.index("ACTUAL_SOURCE_SHA256=$(sha256sum"), recipe.index('"$CC" -std=c11'))
        self.assertIn('[ "$ACTUAL_SOURCE_SHA256" = "$EXPECTED_SOURCE_SHA256" ]', recipe)
        self.assertIn('stat -c \'%a\' "$HELPER"', recipe)
        self.assertIn('HELPER_UID=$(stat -c \'%u\' "$HELPER")', recipe)
        self.assertIn('HELPER_GID=$(stat -c \'%g\' "$HELPER")', recipe)

    def test_existing_mount_identity_is_verified_before_mutation(self):
        recipe = (ROOT / "scripts" / "provision_storage_domain.sh").read_text()
        self.assertLess(recipe.index("POOL_UUID=$(blkid -s UUID -o value"), recipe.index("    verify_mount\n"))
        self.assertLess(recipe.index("if mountpoint -q \"$POOL_ROOT\"; then"), recipe.index('chown duck-lint:duck-lint'))
        self.assertLess(recipe.index("verify_device_filesystem\n    verify_mount"), recipe.index('chown duck-lint:duck-lint'))
        self.assertIn('MOUNT_SOURCE_REAL=$(readlink -f -- "$MOUNT_SOURCE")', recipe)
        self.assertIn('[ "$MOUNT_SOURCE_REAL" = "$POOL_DEVICE_REAL" ]', recipe)
        self.assertIn('findmnt -no UUID --target "$POOL_ROOT"', recipe)
        self.assertIn('findmnt -no FSTYPE --target "$POOL_ROOT"', recipe)
        self.assertIn('findmnt -no OPTIONS --target "$POOL_ROOT"', recipe)

    def test_fstab_update_is_exact_and_preserves_unrelated_entries(self):
        recipe = (ROOT / "scripts" / "provision_storage_domain.sh").read_text()
        self.assertIn('NF >= 2 && $2 == root', recipe)
        self.assertIn('if ($0 != desired) conflict = 1', recipe)
        self.assertIn('if (found == 0) print desired', recipe)
        self.assertIn('if (conflict || found > 1) exit 42', recipe)

    def test_runtime_requires_actual_setuid_owner_and_group_state(self):
        binary = b"reviewed-helper"
        identity = {
            "schema": "symphony-pilot-quota-helper/v1",
            "source_sha256": wsl_contained_exec.QUOTA_HELPER_SOURCE_SHA256,
            "helper_sha256": hashlib.sha256(binary).hexdigest(),
            "group": wsl_contained_exec.QUOTA_HELPER_GROUP,
            "privilege": "setuid-root",
        }
        good_parent = types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
        good_helper = types.SimpleNamespace(
            st_mode=stat.S_IFREG | stat.S_ISUID | 0o750, st_uid=0, st_gid=4242,
        )
        good_identity = types.SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=0)
        fake_grp = types.SimpleNamespace(
            getgrnam=lambda name: types.SimpleNamespace(gr_gid=4242),
        )

        def run_case(helper=good_helper, parent=good_parent, document=identity):
            seen = [False]

            def helper_read(fd, size):
                if fd == 8:
                    return json.dumps(document).encode("ascii")
                if not seen[0]:
                    seen[0] = True
                    return binary
                return b""

            with mock.patch.dict(sys.modules, {"grp": fake_grp}), \
                 mock.patch.object(wsl_contained_exec.os, "O_CLOEXEC", 0, create=True), \
                 mock.patch.object(wsl_contained_exec.os, "stat", return_value=parent), \
                 mock.patch.object(wsl_contained_exec.os, "open", side_effect=[7, 8]), \
                 mock.patch.object(wsl_contained_exec.os, "fstat", side_effect=[helper, good_identity]), \
                 mock.patch.object(wsl_contained_exec.os, "read", side_effect=helper_read), \
                 mock.patch.object(wsl_contained_exec.os, "lseek"), \
                 mock.patch.object(wsl_contained_exec.os, "close"):
                return wsl_contained_exec._quota_helper_fd()

        self.assertEqual(run_case(), 7)
        for bad_helper, bad_parent, bad_document in (
            (types.SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=0, st_gid=4242), good_parent, identity),
            (types.SimpleNamespace(st_mode=stat.S_IFREG | stat.S_ISUID | 0o750, st_uid=1, st_gid=4242), good_parent, identity),
            (types.SimpleNamespace(st_mode=stat.S_IFREG | stat.S_ISUID | 0o750, st_uid=0, st_gid=99), good_parent, identity),
            (types.SimpleNamespace(st_mode=stat.S_IFREG | stat.S_ISUID | 0o755, st_uid=0, st_gid=4242), good_parent, identity),
            (good_helper, good_parent, {**identity, "group": "wrong"}),
            (good_helper, types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o775, st_uid=0), identity),
        ):
            with self.subTest(helper=bad_helper, parent=bad_parent, document=bad_document):
                with self.assertRaises(wsl_contained_exec.ContainmentError):
                    run_case(bad_helper, bad_parent, bad_document)

    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("cc"),
                         "native Linux compiler unavailable")
    def test_helper_compiles_with_strict_linux_warnings(self):
        source = ROOT / "provisioning" / "quota-admit-task.c"
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "quota-admit-task"
            result = subprocess.run(
                ["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", str(source), "-o", str(output)],
                capture_output=True, text=True, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cleanup_capability_accepts_only_exact_zero_growth_proof(self):
        evidence = {
            "schema": "symphony-pilot-task-quota-release/v1",
            "project": "symphony-pilot", "identifier": "T-000001",
            "workspace_path": "/home/duck-lint/symphony-workspaces/symphony-pilot/T-000001",
            "project_id": 1_000_001, "workspace_state": "destroyed",
            "quota_state": "removed", "growth_possible": False,
            "remaining_bytes": 0, "remaining_inodes": 0,
        }
        result = mock.Mock(returncode=0, stdout=json.dumps(evidence))
        with mock.patch.object(wsl_contained_exec, "_quota_helper_fd", return_value=7), \
             mock.patch.object(wsl_contained_exec.os, "close"), \
             mock.patch.object(wsl_contained_exec.subprocess, "run", return_value=result):
            self.assertEqual(
                wsl_contained_exec._quota_task_release("symphony-pilot", "T-000001"),
                evidence,
            )

    def test_cleanup_helper_proves_removed_limits_after_zero_usage(self):
        helper = (ROOT / "provisioning" / "quota-admit-task.c").read_text()
        release = helper[helper.index("static int release_task"):helper.index("int main")]
        self.assertIn("quota_set(root, id, 0, 0)", release)
        self.assertIn("quota.dqb_bhardlimit || quota.dqb_ihardlimit", release)


if __name__ == "__main__":
    unittest.main()
