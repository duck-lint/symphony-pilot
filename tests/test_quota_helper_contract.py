from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
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
        self.assertIn("(uint32_t)id", helper)
        self.assertNotIn("(qid_t)id", helper)
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
        self.assertIn('"System32", "diskpart.exe"', recipe)
        self.assertIn("maximum=65536", recipe)
        self.assertIn("type=fixed", recipe)
        self.assertIn("ArgumentList", recipe)
        self.assertIn("VirtDisk.dll", recipe)
        self.assertIn("OpenVirtualDisk", recipe)
        self.assertIn("GetVirtualDiskInformation", recipe)
        self.assertIn("CloseHandle", recipe)
        self.assertIn("GetCompressedFileSizeW", recipe)
        self.assertIn("AllocatedBytes", recipe)
        self.assertIn("Assert-NativeVhdxPostconditions", recipe)
        self.assertIn("64GB", recipe)
        self.assertIn('ValidateSet("Attach", "Detach")', recipe)
        self.assertIn('"--mount", $ExpectedPath, "--vhd", "--bare"', recipe)
        self.assertIn('"--unmount", $ExpectedPath', recipe)
        self.assertIn("SERIAL,WWN,MODEL", recipe)
        self.assertIn("Get-LinuxWholeDiskEvidence", recipe)
        self.assertIn("exactly one new 64-GiB Linux disk", recipe)
        self.assertIn("malformed JSON", recipe)
        self.assertIn("conflicting attachment", recipe)
        self.assertIn("SparseFile", recipe)
        self.assertIn("Compressed", recipe)
        self.assertIn("Remove-NewlyCreatedVhdx", recipe)
        self.assertIn("maximum=65536 type=fixed", recipe)
        self.assertIn("VirtualDiskAccessGetInfo = 0x00080000", recipe)
        self.assertIn("GetVirtualDiskInfoSize = 1", recipe)
        self.assertIn("GetVirtualDiskInfoVirtualStorageType = 6", recipe)
        self.assertIn("GetVirtualDiskInfoProviderSubtype = 7", recipe)
        self.assertIn("VirtualStorageTypeDeviceUnknown = 0", recipe)
        self.assertIn("VendorId = Guid.Empty", recipe)
        self.assertIn("OpenVirtualDiskFlagNone,\n                    IntPtr.Zero", recipe)
        self.assertIn("IntPtr sizeUsed", recipe)
        self.assertIn("IntPtr.Zero", recipe)
        self.assertIn("SafeVirtualDiskHandle", recipe)
        self.assertIn("public SafeVirtualDiskHandle()", recipe)
        self.assertIn('"OpenVirtualDisk"', recipe)
        self.assertIn('"VIRTUAL_STORAGE_TYPE"', recipe)
        self.assertIn('"PROVIDER_SUBTYPE"', recipe)
        self.assertIn('"SIZE"', recipe)
        self.assertIn("VirtDiskStageException", recipe)
        self.assertIn("status=$([uint32]$failure.Status)", recipe)
        self.assertIn("Size = 32", recipe)
        self.assertIn("FieldOffset(16)] public ulong PhysicalSize", recipe)
        self.assertIn("FieldOffset(24)] public uint BlockSize", recipe)
        self.assertIn("FieldOffset(28)] public uint SectorSize", recipe)
        self.assertNotIn("GetVirtualDiskInfoVirtualStorageType = 8", recipe)
        self.assertNotIn("GetVirtualDiskInfoProviderSubtype = 9", recipe)
        self.assertNotIn("AttachVirtualDisk", recipe)
        self.assertNotIn("DetachVirtualDisk", recipe)
        self.assertNotIn("ResizeVirtualDisk", recipe)
        self.assertNotIn("CompactVirtualDisk", recipe)
        self.assertNotIn("MergeVirtualDisk", recipe)
        self.assertNotIn("CreateVirtualDisk", recipe)
        self.assertNotIn("SetVirtualDiskInformation", recipe)
        self.assertNotIn("ExpandVirtualDisk", recipe)
        self.assertNotIn("VirtualDiskAccessMetaOps", recipe)
        self.assertNotIn("VirtualDiskAccessAttach", recipe)
        self.assertNotIn("OpenVirtualDiskParameters", recipe)
        self.assertNotIn("GetInfoOnly", recipe)
        self.assertNotIn("ReadOnly", recipe)
        self.assertNotIn("New-VHD", recipe)
        self.assertNotIn("Get-VHD", recipe)
        self.assertNotIn("Mount-VHD", recipe)
        self.assertNotIn("Dismount-VHD", recipe)
        self.assertNotIn("Import-Module Hyper-V", recipe)
        self.assertNotIn("PHYSICALDRIVE", recipe)
        self.assertNotIn("FileSize -ne", recipe)
        self.assertNotIn("Resize-VHD", recipe)
        self.assertNotIn("attach vdisk", recipe.lower())
        self.assertNotIn("detach vdisk", recipe.lower())
        self.assertNotIn("expand vdisk", recipe.lower())
        self.assertNotIn("compact vdisk", recipe.lower())
        self.assertNotIn("create partition", recipe.lower())
        self.assertNotIn("assign letter", recipe.lower())
        self.assertNotIn("cmd.exe", recipe.lower())
        self.assertNotIn("powershell -command", recipe.lower())
        self.assertNotIn("Invoke-Expression", recipe)
        attach_main = recipe[recipe.index('if ($Operation -eq "Attach") {'):]
        self.assertLess(
            attach_main.index('$vhd = Get-VhdEvidence $true'),
            attach_main.index('$attached = Invoke-WslText @("--mount"'),
        )

    def test_native_vhdx_postcondition_contract_is_explicit(self):
        recipe = (ROOT / "scripts" / "provision_storage_vhdx.ps1").read_text()
        self.assertIn("DeviceId -ne 3", recipe)
        self.assertIn("ProviderSubtype -ne 2", recipe)
        self.assertIn("VirtualStorageType = \"VHDX\"", recipe)
        self.assertIn("ProviderSubtype = [uint32]$NativeInfo.ProviderSubtype", recipe)
        self.assertIn("VirtualSizeBytes", recipe)
        self.assertIn("physical allocation is below 64 GiB", recipe)
        self.assertIn("fixed VHDX must not be a reparse point", recipe)
        self.assertIn("fixed VHDX must not be sparse or compressed", recipe)
        self.assertIn("VhdType = \"Fixed\"", recipe)
        self.assertIn("$existing = Get-Item", recipe)
        self.assertIn("if (-not $Create)", recipe)

    @unittest.skipUnless(sys.platform.startswith("win") and shutil.which("pwsh"),
                         "Windows PowerShell unavailable")
    def test_native_vhdx_postconditions_reject_conflicting_evidence(self):
        recipe = (ROOT / "scripts" / "provision_storage_vhdx.ps1").read_text()
        start = recipe.index("function Assert-NativeVhdxPostconditions")
        end = recipe.index("function Get-VhdEvidence")
        validator = recipe[start:end]
        command = f'''$ErrorActionPreference = "Stop"
$ExpectedBytes = 64GB
$ExpectedPath = Join-Path ([IO.Path]::GetTempPath()) "symphony-native-vhdx-test.vhdx"
{validator}
$normal = [pscustomobject]@{{
    PSIsContainer = $false
    FullName = $ExpectedPath
    Attributes = [IO.FileAttributes]::Archive
    Length = 4096
}}
$valid = [pscustomobject]@{{ DeviceId = 3; VendorId = "ec984aec-a0f9-47e9-901f-71415a66345b"; ProviderSubtype = 2; VirtualSize = 64GB; PhysicalSize = 64GB }}
$evidence = Assert-NativeVhdxPostconditions @($valid) $normal 64GB
if ($evidence.VhdType -ne "Fixed" -or $evidence.VirtualStorageType -ne "VHDX" -or $evidence.ProviderSubtype -ne 2 -or $evidence.AllocatedBytes -ne 64GB) {{ exit 1 }}
function Must-Fail([object]$NativeInfo, [object]$FileItem, [uint64]$Allocated) {{
    try {{ Assert-NativeVhdxPostconditions $NativeInfo $FileItem $Allocated | Out-Null; return $false }}
    catch {{ return $true }}
}}
if (-not (Must-Fail ([pscustomobject]@{{ DeviceId=2; VendorId="ec984aec-a0f9-47e9-901f-71415a66345b"; ProviderSubtype=2; VirtualSize=64GB; PhysicalSize=64GB }}) $normal 64GB)) {{ exit 1 }}
if (-not (Must-Fail ([pscustomobject]@{{ DeviceId=3; VendorId="00000000-0000-0000-0000-000000000000"; ProviderSubtype=2; VirtualSize=64GB; PhysicalSize=64GB }}) $normal 64GB)) {{ exit 1 }}
if (-not (Must-Fail ([pscustomobject]@{{ DeviceId=3; VendorId="ec984aec-a0f9-47e9-901f-71415a66345b"; ProviderSubtype=3; VirtualSize=64GB; PhysicalSize=64GB }}) $normal 64GB)) {{ exit 1 }}
if (-not (Must-Fail ([pscustomobject]@{{ DeviceId=3; VendorId="ec984aec-a0f9-47e9-901f-71415a66345b"; ProviderSubtype=4; VirtualSize=64GB; PhysicalSize=64GB }}) $normal 64GB)) {{ exit 1 }}
if (-not (Must-Fail ([pscustomobject]@{{ DeviceId=3; VendorId="ec984aec-a0f9-47e9-901f-71415a66345b"; ProviderSubtype=99; VirtualSize=64GB; PhysicalSize=64GB }}) $normal 64GB)) {{ exit 1 }}
if (-not (Must-Fail ([pscustomobject]@{{ DeviceId=3; VendorId="ec984aec-a0f9-47e9-901f-71415a66345b"; ProviderSubtype=2; VirtualSize=1GB; PhysicalSize=1GB }}) $normal 64GB)) {{ exit 1 }}
if (-not (Must-Fail $valid $normal (64GB - 1))) {{ exit 1 }}
if (-not (Must-Fail $valid ([pscustomobject]@{{ PSIsContainer=$false; FullName=$ExpectedPath; Attributes=[IO.FileAttributes]::SparseFile; Length=4096 }}) 64GB)) {{ exit 1 }}
if (-not (Must-Fail $valid ([pscustomobject]@{{ PSIsContainer=$false; FullName=$ExpectedPath; Attributes=[IO.FileAttributes]::Compressed; Length=4096 }}) 64GB)) {{ exit 1 }}
if (-not (Must-Fail $valid ([pscustomobject]@{{ PSIsContainer=$false; FullName=$ExpectedPath; Attributes=[IO.FileAttributes]::ReparsePoint; Length=4096 }}) 64GB)) {{ exit 1 }}
"Native VHDX postconditions: PASS"
'''
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Native VHDX postconditions: PASS", result.stdout)

    @unittest.skipUnless(sys.platform.startswith("win") and shutil.which("pwsh"),
                         "Windows PowerShell unavailable")
    def test_native_allocated_size_smoke_uses_fixed_windows_api(self):
        recipe = (ROOT / "scripts" / "provision_storage_vhdx.ps1").read_text()
        helper_start = recipe.index("function Get-InteropContractIdentity")
        start = recipe.index("function Get-NativeAllocatedFileBytes")
        end = recipe.index("function Remove-NewlyCreatedVhdx")
        helper = recipe[helper_start:end]
        command = f'''$ErrorActionPreference = "Stop"
{helper}
$temporary = [IO.Path]::GetTempFileName()
try {{
    [IO.File]::WriteAllBytes($temporary, (New-Object byte[] 4096))
    $allocated = Get-NativeAllocatedFileBytes $temporary
    if ($allocated -lt 4096) {{ exit 1 }}
    "Native allocated-size smoke: PASS"
}}
finally {{
    if (Test-Path -LiteralPath $temporary) {{ [IO.File]::Delete($temporary) }}
}}
'''
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Native allocated-size smoke: PASS", result.stdout)

    @unittest.skipUnless(sys.platform.startswith("win") and shutil.which("pwsh"),
                         "Windows PowerShell unavailable")
    def test_native_windows_storage_capabilities_are_available(self):
        command = '''$diskpart = Join-Path $env:SystemRoot "System32\\diskpart.exe"
if (-not (Test-Path -LiteralPath $diskpart -PathType Leaf)) { exit 1 }
if (-not (Test-Path -LiteralPath (Join-Path $env:SystemRoot "System32\\VirtDisk.dll") -PathType Leaf)) { exit 1 }
"Native Windows storage capabilities: PASS"
'''
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Native Windows storage capabilities: PASS", result.stdout)

    @unittest.skipUnless(sys.platform.startswith("win") and shutil.which("pwsh"),
                         "Windows PowerShell unavailable")
    def test_virtdisk_readonly_api_smoke_resolves_and_rejects_ordinary_file(self):
        recipe = (ROOT / "scripts" / "provision_storage_vhdx.ps1").read_text()
        helper_start = recipe.index("function Get-InteropContractIdentity")
        start = recipe.index("function Get-NativeVhdxInformation")
        end = recipe.index("function Get-NativeAllocatedFileBytes")
        verifier = recipe[helper_start:end]
        command = f'''$ErrorActionPreference = "Stop"
{verifier}
$temporary = [IO.Path]::GetTempFileName()
try {{
    try {{ Get-NativeVhdxInformation $temporary | Out-Null }} catch {{}}
    if ($null -eq ("SymphonyVirtDiskEvidence" -as [type])) {{ exit 1 }}
    $layout = [SymphonyVirtDiskEvidence]::GetAbiLayout()
    if ($layout.SizeInfoVersion -ne 1 -or
        $layout.VirtualStorageTypeInfoVersion -ne 6 -or
        $layout.ProviderSubtypeInfoVersion -ne 7 -or
        $layout.RequestedDeviceId -ne 0 -or
        $layout.RequestedVendorId -ne [guid]::Empty -or
        $layout.InformationAccessMask -ne 0x00080000) {{ exit 1 }}
    if ($layout.GetInfoBufferSize -ne 32 -or
        $layout.GetInfoVersionOffset -ne 0 -or
        $layout.GetInfoVirtualSizeOffset -ne 8 -or
        $layout.GetInfoPhysicalSizeOffset -ne 16 -or
        $layout.GetInfoBlockSizeOffset -ne 24 -or
        $layout.GetInfoSectorSizeOffset -ne 28 -or
        $layout.GetInfoVirtualStorageTypeOffset -ne 8 -or
        $layout.GetInfoProviderSubtypeOffset -ne 8) {{ exit 1 }}
    try {{ [SymphonyVirtDiskEvidence]::Read($temporary) | Out-Null; exit 1 }}
    catch {{
        if ($_.Exception.Message -notmatch "VirtDisk native operation failed") {{ exit 1 }}
    }}
    try {{ Get-NativeVhdxInformation $temporary | Out-Null; exit 1 }}
    catch {{
        if ($_.Exception.Message -notmatch "stage 'OpenVirtualDisk' \\(status=\\d+\\)") {{ exit 1 }}
    }}
    "VirtDisk read-only API smoke: PASS"
}}
finally {{
    if (Test-Path -LiteralPath $temporary) {{ [IO.File]::Delete($temporary) }}
}}
'''
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("VirtDisk read-only API smoke: PASS", result.stdout)

    @unittest.skipUnless(sys.platform.startswith("win") and shutil.which("pwsh"),
                         "Windows PowerShell unavailable")
    def test_positive_temporary_fixed_vhdx_virtdisk_smoke(self):
        recipe = (ROOT / "scripts" / "provision_storage_vhdx.ps1").read_text()
        helper_start = recipe.index("function Get-InteropContractIdentity")
        verifier_start = recipe.index("function Get-NativeVhdxInformation")
        verifier_end = recipe.index("function Get-NativeAllocatedFileBytes")
        verifier = recipe[helper_start:verifier_end]
        diskpart = pathlib.Path(
            shutil.which("diskpart.exe") or
            pathlib.Path(os.environ["SystemRoot"]) / "System32" / "diskpart.exe"
        )
        with tempfile.TemporaryDirectory(prefix="symphony-virtdisk-smoke-") as directory:
            root = pathlib.Path(directory)
            fixture = root / "fixture.vhdx"
            script = root / "create.txt"
            script.write_text(
                f'create vdisk file="{fixture}" maximum=16 type=fixed\r\n'
                "exit\r\n",
                encoding="ascii",
            )
            created = subprocess.run(
                [str(diskpart), "/s", str(script)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertTrue(fixture.is_file())
            command = f'''$ErrorActionPreference = "Stop"
{verifier}
$info = Get-NativeVhdxInformation "{fixture}"
if ($info.DeviceId -ne 3 -or
    $info.VendorId.ToString() -ne
        "ec984aec-a0f9-47e9-901f-71415a66345b" -or
    $info.ProviderSubtype -ne 2 -or
    $info.VirtualSize -ne 16MB) {{ exit 1 }}
"Temporary fixed VHDX VirtDisk smoke: PASS size=16MiB device=$($info.DeviceId) vendor=$($info.VendorId) subtype=$($info.ProviderSubtype) virtualSize=$($info.VirtualSize)"
'''
            inspected = subprocess.run(
                ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(inspected.returncode, 0, inspected.stderr)
            self.assertIn("Temporary fixed VHDX VirtDisk smoke: PASS", inspected.stdout)

    def test_interop_contract_identity_is_source_derived_and_guarded(self):
        recipe = (ROOT / "scripts" / "provision_storage_vhdx.ps1").read_text()
        self.assertIn("function Get-InteropContractIdentity", recipe)
        self.assertIn("function Read-InteropTypeIdentity", recipe)
        self.assertIn("function Ensure-InteropType", recipe)
        self.assertIn('"SymphonyVirtDiskEvidence"', recipe)
        self.assertIn('"SymphonyNativeFileEvidence"', recipe)
        self.assertIn('InteropContractIdentity', recipe)
        self.assertIn('sha256:{INTEROP_CONTRACT_IDENTITY}', recipe)
        self.assertIn(
            "stale PowerShell interop type conflicts with deployed storage contract; "
            "start a fresh elevated PowerShell session",
            recipe,
        )

    @unittest.skipUnless(sys.platform.startswith("win") and shutil.which("pwsh"),
                         "Windows PowerShell unavailable")
    def test_interop_contract_digest_changes_when_embedded_source_changes(self):
        recipe = (ROOT / "scripts" / "provision_storage_vhdx.ps1").read_text()
        helper_start = recipe.index("function Get-InteropContractIdentity")
        helper_end = recipe.index("function Get-NativeVhdxInformation")
        helpers = recipe[helper_start:helper_end]
        command = f'''$ErrorActionPreference = "Stop"
{helpers}
$sourceA = "embedded implementation marker"
$sourceB = $sourceA.Replace("marker", "changed")
if ((Get-InteropContractIdentity $sourceA) -eq (Get-InteropContractIdentity $sourceB)) {{ exit 1 }}
"Interop contract digest mutation: PASS"
'''
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Interop contract digest mutation: PASS", result.stdout)

    @unittest.skipUnless(sys.platform.startswith("win") and shutil.which("pwsh"),
                         "Windows PowerShell unavailable")
    def test_stale_virtdisk_type_fails_before_native_method_invocation(self):
        recipe = (ROOT / "scripts" / "provision_storage_vhdx.ps1").read_text()
        helper_start = recipe.index("function Get-InteropContractIdentity")
        verifier_start = recipe.index("function Get-NativeVhdxInformation")
        verifier_end = recipe.index("function Get-NativeAllocatedFileBytes")
        verifier = recipe[helper_start:verifier_end]
        command = f'''$ErrorActionPreference = "Stop"
Add-Type -TypeDefinition @'
using System;
public static class SymphonyVirtDiskEvidence
{{
    public const string InteropContractIdentity = "sha256:stale";
    public static object Read(string path) {{ throw new Exception("SENTINEL_INVOKED"); }}
}}
'@
{verifier}
try {{ Get-NativeVhdxInformation "unused" | Out-Null; exit 1 }}
catch {{
    if ($_.Exception.Message -notmatch "stale PowerShell interop type conflicts") {{ exit 1 }}
    if ($_.Exception.Message -match "SENTINEL_INVOKED") {{ exit 1 }}
}}
"Stale VirtDisk interop guard: PASS"
'''
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Stale VirtDisk interop guard: PASS", result.stdout)

    @unittest.skipUnless(sys.platform.startswith("win") and shutil.which("pwsh"),
                         "Windows PowerShell unavailable")
    def test_identity_absent_virtdisk_type_fails_closed_before_invocation(self):
        recipe = (ROOT / "scripts" / "provision_storage_vhdx.ps1").read_text()
        helper_start = recipe.index("function Get-InteropContractIdentity")
        verifier_start = recipe.index("function Get-NativeVhdxInformation")
        verifier_end = recipe.index("function Get-NativeAllocatedFileBytes")
        verifier = recipe[helper_start:verifier_end]
        command = f'''$ErrorActionPreference = "Stop"
Add-Type -TypeDefinition @'
using System;
public static class SymphonyVirtDiskEvidence
{{
    public static object Read(string path) {{ throw new Exception("SENTINEL_INVOKED"); }}
}}
'@
{verifier}
try {{ Get-NativeVhdxInformation "unused" | Out-Null; exit 1 }}
catch {{
    if ($_.Exception.Message -notmatch "stale PowerShell interop type conflicts") {{ exit 1 }}
    if ($_.Exception.Message -match "SENTINEL_INVOKED") {{ exit 1 }}
}}
"Identity-absent VirtDisk interop guard: PASS"
'''
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Identity-absent VirtDisk interop guard: PASS", result.stdout)

    @unittest.skipUnless(sys.platform.startswith("win") and shutil.which("pwsh"),
                         "Windows PowerShell unavailable")
    def test_matching_virtdisk_type_is_reused_within_one_process(self):
        recipe = (ROOT / "scripts" / "provision_storage_vhdx.ps1").read_text()
        helper_start = recipe.index("function Get-InteropContractIdentity")
        verifier_start = recipe.index("function Get-NativeVhdxInformation")
        verifier_end = recipe.index("function Get-NativeAllocatedFileBytes")
        verifier = recipe[helper_start:verifier_end]
        command = f'''$ErrorActionPreference = "Stop"
{verifier}
$temporary = [IO.Path]::GetTempFileName()
try {{
    $messages = @()
    foreach ($unused in 1..2) {{
        try {{ Get-NativeVhdxInformation $temporary | Out-Null; exit 1 }}
        catch {{ $messages += $_.Exception.Message }}
    }}
    if ($messages.Count -ne 2) {{ exit 1 }}
    foreach ($message in $messages) {{
        if ($message -notmatch "stage 'OpenVirtualDisk' \\(status=\\d+\\)") {{ exit 1 }}
        if ($message -match "stale PowerShell interop type conflicts") {{ exit 1 }}
    }}
    "Matching VirtDisk interop reuse: PASS"
}}
finally {{
    if (Test-Path -LiteralPath $temporary) {{ [IO.File]::Delete($temporary) }}
}}
'''
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Matching VirtDisk interop reuse: PASS", result.stdout)

    @unittest.skipUnless(sys.platform.startswith("win") and shutil.which("pwsh"),
                         "Windows PowerShell unavailable")
    def test_stale_native_file_type_fails_before_native_method_invocation(self):
        recipe = (ROOT / "scripts" / "provision_storage_vhdx.ps1").read_text()
        helper_start = recipe.index("function Get-InteropContractIdentity")
        loader_start = recipe.index("function Get-NativeAllocatedFileBytes")
        loader_end = recipe.index("function Remove-NewlyCreatedVhdx")
        loader = recipe[helper_start:loader_end]
        command = f'''$ErrorActionPreference = "Stop"
Add-Type -TypeDefinition @'
using System;
public static class SymphonyNativeFileEvidence
{{
    public const string InteropContractIdentity = "sha256:stale";
    public static uint GetCompressedFileSizeW(string path, out uint high) {{ throw new Exception("SENTINEL_INVOKED"); }}
}}
'@
{loader}
try {{ Get-NativeAllocatedFileBytes "unused" | Out-Null; exit 1 }}
catch {{
    if ($_.Exception.Message -notmatch "stale PowerShell interop type conflicts") {{ exit 1 }}
    if ($_.Exception.Message -match "SENTINEL_INVOKED") {{ exit 1 }}
}}
"Stale native-file interop guard: PASS"
'''
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Stale native-file interop guard: PASS", result.stdout)

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

    @unittest.skipUnless(sys.platform.startswith("win") and shutil.which("pwsh"),
                         "Windows PowerShell unavailable")
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

    @unittest.skipUnless(sys.platform.startswith("win") and shutil.which("pwsh"),
                         "Windows PowerShell unavailable")
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
        source_digest = recipe.index("ACTUAL_SOURCE_SHA256=$(sha256sum")
        preflight = recipe.index("compile_helper_preflight\n")
        first_mutations = [
            recipe.index('mkdir -p "$POOL_ROOT"'),
            recipe.index("mkfs.ext4 -L"),
            recipe.index('mount -t ext4 -o prjquota'),
            recipe.index('chown duck-lint:duck-lint "$POOL_ROOT"'),
            recipe.index('chmod 0750 "$POOL_ROOT"'),
            recipe.index('FSTAB_TMP=$(mktemp /etc/'),
            recipe.index('groupadd --system "$HELPER_GROUP"'),
            recipe.index('install -d -o root -g "$HELPER_GROUP"'),
            recipe.index('usermod --append --groups "$HELPER_GROUP" duck-lint'),
            recipe.index('install -o root -g "$HELPER_GROUP" -m 4750'),
        ]
        self.assertLess(source_digest, preflight)
        self.assertTrue(all(preflight < mutation for mutation in first_mutations))
        self.assertIn('CC=/usr/bin/cc', recipe)
        self.assertIn('[ -f "$CC" ] && [ -x "$CC" ]', recipe)
        self.assertIn('/usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin', recipe)
        self.assertIn('-std=c11 -O2 -Wall -Wextra -Werror', recipe)
        self.assertEqual(recipe.count('-std=c11 -O2 -Wall -Wextra -Werror'), 1)
        self.assertNotIn('-o "$HELPER_TMP" >/dev/null 2>&1', recipe)
        self.assertIn('trusted provisioning prerequisite helper compilation failed', recipe)
        self.assertIn('install -o root -g "$HELPER_GROUP" -m 4750 "$HELPER_TMP" "$HELPER"', recipe)
        self.assertIn('rm -f -- "$HELPER_TMP"', recipe)
        self.assertIn('[ "$ACTUAL_SOURCE_SHA256" = "$EXPECTED_SOURCE_SHA256" ]', recipe)
        self.assertIn('stat -c \'%a\' "$HELPER"', recipe)
        self.assertIn('HELPER_UID=$(stat -c \'%u\' "$HELPER")', recipe)
        self.assertIn('HELPER_GID=$(stat -c \'%g\' "$HELPER")', recipe)
        install_helper = recipe.index("install_verified_helper()")
        identity_record = recipe.rindex('if [ -e "$IDENTITY" ]; then')
        verify_pool = recipe.index('"$HELPER" --operation verify-pool')
        self.assertLess(install_helper, identity_record)
        self.assertLess(identity_record, verify_pool)
        install_body = recipe[install_helper:identity_record]
        self.assertIn('HELPER_SHA256=$(sha256sum "$HELPER"', install_body)
        self.assertIn('[ "$HELPER_SHA256" = "$COMPILED_HELPER_SHA256" ]', install_body)
        self.assertIn('cmp -s "$HELPER_TMP" "$HELPER"', install_body)
        self.assertLess(install_body.index('cmp -s'), install_body.index('rm -f -- "$HELPER_TMP"'))
        self.assertLess(install_body.index('rm -f -- "$HELPER_TMP"'), install_body.index('HELPER_TMP='))
        self.assertIn('trap cleanup EXIT HUP INT TERM', recipe)
        self.assertIn('[ -z "$HELPER_TMP" ] || rm -f -- "$HELPER_TMP"', recipe)

    def test_partial_state_recovery_reuses_identity_and_reaches_helper_install(self):
        recipe = (ROOT / "scripts" / "provision_storage_domain.sh").read_text()
        existing_device = recipe.index('if [ -n "$DEVICE_TYPE" ]; then')
        format_block = recipe.index('if [ -z "$DEVICE_TYPE" ]; then')
        format_command = recipe.index('mkfs.ext4 -L')
        existing_group = recipe.index('getent group "$HELPER_GROUP" >/dev/null 2>&1 || groupadd')
        helper_install = recipe.index('install -o root -g "$HELPER_GROUP" -m 4750 "$HELPER_TMP" "$HELPER"')
        self.assertLess(existing_device, format_block)
        self.assertLess(format_block, format_command)
        self.assertIn('DEVICE_TYPE=$(blkid -o value -s TYPE "$POOL_DEVICE")', recipe)
        self.assertIn('[ "$DEVICE_LABEL" = "$POOL_LABEL" ]', recipe)
        self.assertIn('POOL_UUID=$(blkid -s UUID -o value "$POOL_DEVICE")', recipe)
        self.assertIn('if [ -e "$STORAGE_IDENTITY" ]; then', recipe)
        self.assertIn('if [ -e "$HELPER" ]; then', recipe)
        self.assertLess(existing_group, helper_install)
        self.assertIn('rm -f -- "$HELPER_TMP"', recipe)
        self.assertNotIn('groupadd --force', recipe)

    def _preflight_shell_function(self):
        recipe = (ROOT / "scripts" / "provision_storage_domain.sh").read_text()
        match = re.search(
            r"(?ms)^compile_helper_preflight\(\) \{\n.*?^\}\n",
            recipe,
        )
        self.assertIsNotNone(match)
        return match.group(0)

    def _verified_helper_install_shell_function(self):
        recipe = (ROOT / "scripts" / "provision_storage_domain.sh").read_text()
        match = re.search(
            r"(?ms)^install_verified_helper\(\) \{\n.*?^\}\n",
            recipe,
        )
        self.assertIsNotNone(match)
        return match.group(0)

    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("sh"),
                         "native POSIX shell unavailable")
    def test_exact_copy_is_proven_before_identity_and_execution(self):
        install_function = self._verified_helper_install_shell_function()
        with tempfile.TemporaryDirectory(prefix="symphony-helper-copy-") as directory:
            root = pathlib.Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_install = fake_bin / "install"
            fake_install.write_text(
                """#!/bin/sh
last=
previous=
for argument in "$@"; do
    previous=$last
    last=$argument
done
cp "$previous" "$last"
""",
                encoding="ascii",
            )
            fake_install.chmod(0o700)
            preflight = root / "preflight-helper"
            preflight.write_bytes(b"exact preflight helper bytes\n")
            helper = root / "quota-admit-task"
            identity = root / "identity"
            executed = root / "executed"
            command = f'''#!/bin/sh
set -eu
fail() {{ echo "symphony storage provisioning stopped: $*" >&2; exit 78; }}
PATH="{fake_bin}:/usr/bin:/bin"
HELPER_GROUP=symphony-pilot
EXPECTED_GID=0
HELPER_TMP="{preflight}"
HELPER="{helper}"
COMPILED_HELPER_SHA256=$(sha256sum "$HELPER_TMP" | awk '{{print $1}}')
cleanup() {{ [ -z "$HELPER_TMP" ] || rm -f -- "$HELPER_TMP"; }}
trap cleanup EXIT
{install_function}
install_verified_helper
if [ -e "$HELPER_TMP" ]; then exit 1; fi
printf 'reviewed helper identity\\n' > "{identity}"
touch "{executed}"
echo "exact-copy install: PASS"
'''
            harness = root / "run.sh"
            harness.write_text(command, encoding="ascii")
            harness.chmod(0o700)
            result = subprocess.run(
                [shutil.which("sh"), str(harness)],
                capture_output=True, text=True, check=False,
            )
            installed_bytes = helper.read_bytes()
            identity_exists = identity.exists()
            executed_exists = executed.exists()
            temp_exists = preflight.exists()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("exact-copy install: PASS", result.stdout)
        self.assertEqual(installed_bytes, b"exact preflight helper bytes\n")
        self.assertTrue(identity_exists)
        self.assertTrue(executed_exists)
        self.assertFalse(temp_exists)

    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("sh"),
                         "native POSIX shell unavailable")
    def test_corrupt_install_fails_before_identity_or_helper_execution(self):
        install_function = self._verified_helper_install_shell_function()
        with tempfile.TemporaryDirectory(prefix="symphony-helper-corrupt-") as directory:
            root = pathlib.Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_install = fake_bin / "install"
            fake_install.write_text(
                """#!/bin/sh
last=
for argument in "$@"; do
    last=$argument
done
printf 'corrupt installed bytes\\n' > "$last"
""",
                encoding="ascii",
            )
            fake_install.chmod(0o700)
            preflight = root / "preflight-helper"
            preflight.write_bytes(b"exact preflight helper bytes\n")
            helper = root / "quota-admit-task"
            identity = root / "identity"
            executed = root / "executed"
            command = f'''#!/bin/sh
set -eu
fail() {{ echo "symphony storage provisioning stopped: $*" >&2; exit 78; }}
PATH="{fake_bin}:/usr/bin:/bin"
HELPER_GROUP=symphony-pilot
EXPECTED_GID=0
HELPER_TMP="{preflight}"
HELPER="{helper}"
COMPILED_HELPER_SHA256=$(sha256sum "$HELPER_TMP" | awk '{{print $1}}')
cleanup() {{ [ -z "$HELPER_TMP" ] || rm -f -- "$HELPER_TMP"; }}
trap cleanup EXIT
{install_function}
install_verified_helper
printf 'reviewed helper identity\\n' > "{identity}"
touch "{executed}"
'''
            harness = root / "run.sh"
            harness.write_text(command, encoding="ascii")
            harness.chmod(0o700)
            result = subprocess.run(
                [shutil.which("sh"), str(harness)],
                capture_output=True, text=True, check=False,
            )
            installed_bytes = helper.read_bytes()
            identity_exists = identity.exists()
            executed_exists = executed.exists()
            temp_exists = preflight.exists()
        self.assertEqual(result.returncode, 78)
        self.assertIn("installed quota helper bytes differ from the preflight build", result.stderr)
        self.assertNotEqual(installed_bytes, b"exact preflight helper bytes\n")
        self.assertFalse(identity_exists)
        self.assertFalse(executed_exists)
        self.assertFalse(temp_exists)

    @unittest.skipUnless(
        sys.platform.startswith("linux") and shutil.which("sh") and
        hasattr(os, "geteuid") and os.geteuid() == 0,
        "root native POSIX shell unavailable",
    )
    def test_missing_compiler_preflight_stops_before_mutation(self):
        preflight = self._preflight_shell_function()
        with tempfile.TemporaryDirectory(prefix="symphony-preflight-missing-") as directory:
            root = pathlib.Path(directory)
            source = root / "quota-admit-task.c"
            source.write_text("int main(void) { return 0; }\n", encoding="ascii")
            marker = root / "mutation-marker"
            command = f'''#!/bin/sh
set -eu
fail() {{ echo "symphony storage provisioning stopped: $*" >&2; exit 78; }}
CC="{root / "missing-cc"}"
HELPER_SOURCE="{source}"
HELPER_TMP=
COMPILED_HELPER_SHA256=
cleanup() {{ [ -z "$HELPER_TMP" ] || rm -f -- "$HELPER_TMP"; }}
trap cleanup EXIT
{preflight}
touch "{marker}"
'''
            harness = root / "run.sh"
            harness.write_text(command, encoding="ascii")
            harness.chmod(0o700)
            result = subprocess.run(
                [shutil.which("sh"), str(harness)],
                capture_output=True, text=True, check=False,
            )
        self.assertEqual(result.returncode, 78)
        self.assertIn("trusted provisioning prerequisite /usr/bin/cc is unavailable", result.stderr)
        self.assertFalse(marker.exists())

    @unittest.skipUnless(
        sys.platform.startswith("linux") and shutil.which("sh") and
        hasattr(os, "geteuid") and os.geteuid() == 0,
        "root native POSIX shell unavailable",
    )
    def test_failing_compiler_preflight_stops_before_mutation(self):
        preflight = self._preflight_shell_function()
        with tempfile.TemporaryDirectory(prefix="symphony-preflight-failing-") as directory:
            root = pathlib.Path(directory)
            source = root / "quota-admit-task.c"
            source.write_text("int main(void) { return 0; }\n", encoding="ascii")
            compiler = root / "cc"
            compiler.write_text("#!/bin/sh\nexit 17\n", encoding="ascii")
            compiler.chmod(0o700)
            marker = root / "mutation-marker"
            command = f'''#!/bin/sh
set -eu
fail() {{ echo "symphony storage provisioning stopped: $*" >&2; exit 78; }}
CC="{compiler}"
HELPER_SOURCE="{source}"
HELPER_TMP=
COMPILED_HELPER_SHA256=
cleanup() {{ [ -z "$HELPER_TMP" ] || rm -f -- "$HELPER_TMP"; }}
trap cleanup EXIT
{preflight}
touch "{marker}"
'''
            harness = root / "run.sh"
            harness.write_text(command, encoding="ascii")
            harness.chmod(0o700)
            result = subprocess.run(
                [shutil.which("sh"), str(harness)],
                capture_output=True, text=True, check=False,
            )
        self.assertEqual(result.returncode, 78)
        self.assertIn("trusted provisioning prerequisite helper compilation failed", result.stderr)
        self.assertFalse(marker.exists())

    @unittest.skipUnless(
        sys.platform.startswith("linux") and shutil.which("sh") and
        hasattr(os, "geteuid") and os.geteuid() == 0,
        "root native POSIX shell unavailable",
    )
    def test_successful_preflight_installs_same_binary_without_recompile(self):
        preflight = self._preflight_shell_function()
        with tempfile.TemporaryDirectory(prefix="symphony-preflight-success-") as directory:
            root = pathlib.Path(directory)
            source = root / "quota-admit-task.c"
            source.write_text("int main(void) { return 0; }\n", encoding="ascii")
            compiler = root / "cc"
            count = root / "compile-count"
            count.write_text("0\n", encoding="ascii")
            compiler.write_text(
                f'''#!/bin/sh
count=$(cat "{count}")
count=$((count + 1))
printf '%s\\n' "$count" > "{count}"
output=
while [ "$#" -gt 0 ]; do
    if [ "$1" = "-o" ]; then
        shift
        output=$1
    fi
    shift
done
printf 'preflight-compiled-helper\\n' > "$output"
''',
                encoding="ascii",
            )
            compiler.chmod(0o700)
            installed = root / "installed-helper"
            command = f'''#!/bin/sh
set -eu
fail() {{ echo "symphony storage provisioning stopped: $*" >&2; exit 78; }}
CC="{compiler}"
HELPER_SOURCE="{source}"
HELPER_TMP=
COMPILED_HELPER_SHA256=
cleanup() {{ [ -z "$HELPER_TMP" ] || rm -f -- "$HELPER_TMP"; }}
trap cleanup EXIT
{preflight}
install -m 4750 "$HELPER_TMP" "{installed}"
if ! cmp -s "$HELPER_TMP" "{installed}"; then exit 1; fi
if [ "$(cat "{count}")" != "1" ]; then exit 1; fi
echo "preflight same-binary install: PASS"
'''
            harness = root / "run.sh"
            harness.write_text(command, encoding="ascii")
            harness.chmod(0o700)
            result = subprocess.run(
                [shutil.which("sh"), str(harness)],
                capture_output=True, text=True, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("preflight same-binary install: PASS", result.stdout)

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
