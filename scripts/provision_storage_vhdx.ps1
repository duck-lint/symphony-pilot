#requires -Version 7.2
<##
.SYNOPSIS
  Create/verify and explicitly attach or detach the fixed Symphony VHDX.

This is an operator-only attachment contract.  The path and VHD properties
are fixed in source; Linux provisioning is a separate phase that must run
while the reported WSL attachment remains live.  No Windows disk mount is
performed because a transient Windows disk identity is not the Linux
provisioning identity.
#>
[CmdletBinding()]
param(
    [ValidateSet("Attach", "Detach")]
    [string]$Operation = "Attach"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ExpectedBytes = 64GB
$ExpectedParent = "C:\ProgramData\SymphonyPilot"
$ExpectedPath = [IO.Path]::Combine($ExpectedParent, "symphony-storage.vhdx")
$AttachmentStatePath = [IO.Path]::Combine($ExpectedParent, "symphony-storage.vhdx.attachment.json")
$DiskPart = [IO.Path]::Combine($env:SystemRoot, "System32", "diskpart.exe")
$Distribution = "Ubuntu-24.04"
$Wsl = [IO.Path]::Combine($env:SystemRoot, "System32", "wsl.exe")
$StateSchema = "symphony-pilot-vhdx-attachment/v1"
$OperatorAdminSid = New-Object System.Security.Principal.SecurityIdentifier("S-1-5-32-544")
$OperatorSystemSid = New-Object System.Security.Principal.SecurityIdentifier("S-1-5-18")

if (-not (Test-Path -LiteralPath $Wsl -PathType Leaf)) {
    throw "the fixed Windows WSL executable is unavailable"
}

function Invoke-WslText {
    param([string[]]$Arguments)
    $output = & $Wsl @Arguments 2>&1 | Out-String
    [pscustomobject]@{
        ExitCode = [int]$LASTEXITCODE
        Output = $output.Trim()
    }
}

function Get-LinuxDeviceIdentityKey {
    param([object]$Device)
    $path = [string]$Device.LinuxDevice
    $type = [string]$Device.Type
    $size = [int64]$Device.SizeBytes
    $serial = [string]$Device.Serial
    $wwn = [string]$Device.Wwn
    $model = [string]$Device.Model
    # The path is retained in the fingerprint.  Stable serial/WWN/model
    # fields strengthen the comparison, but a path substitution is still a
    # device-set change unless a reviewed direct attachment query proves it.
    "path=$path|type=$type|size=$size|serial=$serial|wwn=$wwn|model=$model"
}

function Get-LinuxWholeDiskEvidence {
    $result = Invoke-WslText @(
        "--distribution", $Distribution, "--exec", "/bin/lsblk",
        "--json", "--bytes", "--output", "NAME,PATH,TYPE,SIZE,PKNAME,SERIAL,WWN,MODEL"
    )
    if ($result.ExitCode -ne 0) {
        throw "Ubuntu-24.04 lsblk discovery failed: $($result.Output)"
    }
    try {
        $payload = $result.Output | ConvertFrom-Json
        $devices = @($payload.blockdevices)
    }
    catch {
        throw "Ubuntu-24.04 lsblk discovery returned malformed JSON"
    }
    $evidence = foreach ($device in $devices) {
        if ($device.type -ne "disk") {
            continue
        }
        try {
            $sizeBytes = [int64]$device.size
        }
        catch {
            throw "Ubuntu-24.04 lsblk returned a disk with invalid size evidence"
        }
        if ($null -ne $device.pkname -or $device.path -notmatch '^/dev/[A-Za-z0-9._-]+$') {
            throw "Ubuntu-24.04 lsblk returned an unsafe whole-disk identity"
        }
        $item = [pscustomobject]@{
            LinuxDevice = [string]$device.path
            Type = [string]$device.type
            SizeBytes = $sizeBytes
            Serial = [string]$device.serial
            Wwn = [string]$device.wwn
            Model = [string]$device.model
            Parent = $null
        }
        $item | Add-Member -NotePropertyName IdentityKey -NotePropertyValue (Get-LinuxDeviceIdentityKey $item)
        $item
    }
    @($evidence)
}

function ConvertTo-SidValue {
    param([object]$Identity)
    try {
        if ($null -eq $Identity) {
            throw "identity is empty"
        }
        if ($Identity -is [System.Security.Principal.SecurityIdentifier]) {
            return $Identity.Value
        }
        if ($Identity -is [string]) {
            if ([string]::IsNullOrWhiteSpace($Identity)) {
                throw "identity string is empty"
            }
            $Identity = New-Object -TypeName System.Security.Principal.NTAccount -ArgumentList $Identity
        }
        if ($Identity -is [System.Security.Principal.IdentityReference]) {
            return $Identity.Translate([System.Security.Principal.SecurityIdentifier]).Value
        }
        if ($Identity.PSObject.Methods.Name -contains "Translate") {
            return $Identity.Translate([System.Security.Principal.SecurityIdentifier]).Value
        }
        throw "identity cannot be translated"
    }
    catch {
        throw "operator-state ACL contains an unresolvable identity"
    }
}

function Assert-OperatorStateAcl {
    param(
        [string]$Path,
        [bool]$Directory
    )
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        ($Directory -and -not $item.PSIsContainer) -or
        (-not $Directory -and $item.PSIsContainer)) {
        throw "operator-state path is not the expected normal fixed object"
    }
    $acl = Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected) {
        throw "operator-state ACL must disable inheritance"
    }
    if ((ConvertTo-SidValue $acl.Owner) -ne $OperatorAdminSid.Value) {
        throw "operator-state owner must be local Administrators"
    }
    $rules = @($acl.Access)
    if ($rules.Count -ne 2) {
        throw "operator-state ACL must contain only SYSTEM and Administrators"
    }
    $expectedInheritance = if ($Directory) {
        [int]([System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [System.Security.AccessControl.InheritanceFlags]::ObjectInherit)
    } else {
        [int][System.Security.AccessControl.InheritanceFlags]::None
    }
    $seen = @{}
    foreach ($rule in $rules) {
        $sid = ConvertTo-SidValue $rule.IdentityReference
        if ($sid -ne $OperatorAdminSid.Value -and $sid -ne $OperatorSystemSid.Value) {
            throw "operator-state ACL grants an unexpected identity"
        }
        if ($rule.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow -or
            [int64]$rule.FileSystemRights -ne [int64][System.Security.AccessControl.FileSystemRights]::FullControl -or
            [int]$rule.InheritanceFlags -ne $expectedInheritance -or
            [int]$rule.PropagationFlags -ne [int][System.Security.AccessControl.PropagationFlags]::None) {
            throw "operator-state ACL grants unexpected rights or inheritance"
        }
        $seen[$sid] = $true
    }
    if (-not $seen.ContainsKey($OperatorAdminSid.Value) -or
        -not $seen.ContainsKey($OperatorSystemSid.Value)) {
        throw "operator-state ACL is missing SYSTEM or Administrators"
    }
}

function Set-OperatorStateAcl {
    param(
        [string]$Path,
        [bool]$Directory
    )
    $security = if ($Directory) {
        New-Object System.Security.AccessControl.DirectorySecurity
    } else {
        New-Object System.Security.AccessControl.FileSecurity
    }
    $security.SetOwner($OperatorAdminSid)
    $security.SetAccessRuleProtection($true, $false)
    $inheritance = if ($Directory) {
        [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
    } else {
        [System.Security.AccessControl.InheritanceFlags]::None
    }
    foreach ($sid in @($OperatorSystemSid, $OperatorAdminSid)) {
        $rule = New-Object -TypeName System.Security.AccessControl.FileSystemAccessRule -ArgumentList @(
            $sid,
            [System.Security.AccessControl.FileSystemRights]::FullControl,
            $inheritance,
            [System.Security.AccessControl.PropagationFlags]::None,
            [System.Security.AccessControl.AccessControlType]::Allow
        )
        $security.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $security
    Assert-OperatorStateAcl $Path $Directory
}

function Ensure-OperatorStateNamespace {
    param([bool]$Create)
    try {
        $parent = Get-Item -LiteralPath $ExpectedParent -Force -ErrorAction Stop
    }
    catch [System.Management.Automation.ItemNotFoundException] {
        if (-not $Create) {
            throw "fixed operator-state namespace is unavailable"
        }
        New-Item -ItemType Directory -Path $ExpectedParent -Force | Out-Null
        Set-OperatorStateAcl $ExpectedParent $true
        return
    }
    Assert-OperatorStateAcl $ExpectedParent $true
}

function Invoke-DiskPartFixedVhdxCreate {
    if (-not (Test-Path -LiteralPath $DiskPart -PathType Leaf)) {
        throw "the fixed Windows DiskPart executable is unavailable"
    }
    $scriptPath = [IO.Path]::Combine(
        $ExpectedParent,
        ".symphony-vhdx-create-" + [guid]::NewGuid().ToString("N") + ".txt"
    )
    # This is a fixed script assembled only from the fixed source path.  It
    # is not a caller-provided DiskPart interface and contains no attachment,
    # resize, partition, filesystem, or drive-letter operation.
    $scriptText = 'create vdisk file="' + $ExpectedPath +
        '" maximum=65536 type=fixed' + "`r`nexit`r`n"
    try {
        [IO.File]::WriteAllText(
            $scriptPath,
            $scriptText,
            [Text.UTF8Encoding]::new($false)
        )
        $startInfo = [Diagnostics.ProcessStartInfo]::new()
        $startInfo.FileName = $DiskPart
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        [void]$startInfo.ArgumentList.Add("/s")
        [void]$startInfo.ArgumentList.Add($scriptPath)
        $process = [Diagnostics.Process]::new()
        $process.StartInfo = $startInfo
        if (-not $process.Start()) {
            throw "fixed Windows DiskPart could not be started"
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        [pscustomobject]@{
            ExitCode = [int]$process.ExitCode
            Output = (($stdoutTask.Result + "`n" + $stderrTask.Result).Trim())
        }
    }
    finally {
        if (Test-Path -LiteralPath $scriptPath -PathType Leaf) {
            [IO.File]::Delete($scriptPath)
        }
    }
}

function Get-NativeAllocatedFileBytes {
    param([Parameter(Mandatory)][string]$Path)
    if ($null -eq ("SymphonyNativeFileEvidence" -as [type])) {
        Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public static class SymphonyNativeFileEvidence
{
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern uint GetCompressedFileSizeW(
        string lpFileName,
        out uint lpFileSizeHigh);
}
"@
    }
    [uint32]$high = 0
    [uint32]$low = [SymphonyNativeFileEvidence]::GetCompressedFileSizeW(
        $Path,
        [ref]$high
    )
    $errorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
    if ($low -eq [uint32]::MaxValue -and $errorCode -ne 0) {
        throw "native allocated-size inspection failed for the fixed VHDX"
    }
    ([uint64]$high * 4294967296) + [uint64]$low
}

function Remove-NewlyCreatedVhdx {
    # Cleanup is intentionally limited to the exact newly-created leaf.  A
    # conflicting pre-existing object is never removed or repaired.
    try {
        $item = Get-Item -LiteralPath $ExpectedPath -Force -ErrorAction Stop
        if ($item.PSIsContainer -or
            ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            return
        }
        [IO.File]::Delete($ExpectedPath)
    }
    catch {
        # Retain the exact failed object for bounded operator reconciliation;
        # cleanup failure must not widen into deletion authority.
    }
}

function Assert-NativeVhdxPostconditions {
    param(
        [object[]]$Images,
        [object]$FileItem,
        [uint64]$AllocatedBytes
    )
    if (@($Images).Count -ne 1) {
        throw "native Windows storage evidence did not identify exactly one VHDX"
    }
    $image = @($Images)[0]
    if ([IO.Path]::GetFullPath([string]$image.ImagePath) -ne
        [IO.Path]::GetFullPath($ExpectedPath)) {
        throw "native Windows storage evidence identified an unexpected image path"
    }
    if ([string]$image.StorageType -ne "VHDX") {
        throw "fixed Symphony image must have native VHDX storage type"
    }
    if ([int64]$image.Size -ne $ExpectedBytes) {
        throw "VHD virtual capacity must be exactly 64 GiB"
    }
    if ($null -eq $FileItem -or $FileItem.PSIsContainer -or
        [IO.Path]::GetFullPath([string]$FileItem.FullName) -ne
        [IO.Path]::GetFullPath($ExpectedPath)) {
        throw "fixed VHDX is not the exact ordinary file leaf"
    }
    $attributes = [IO.FileAttributes]$FileItem.Attributes
    if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "fixed VHDX must not be a reparse point"
    }
    if (($attributes -band [IO.FileAttributes]::SparseFile) -ne 0 -or
        ($attributes -band [IO.FileAttributes]::Compressed) -ne 0) {
        throw "fixed VHDX must not be sparse or compressed"
    }
    if ($AllocatedBytes -lt [uint64]$ExpectedBytes) {
        throw "fixed VHDX physical allocation is below 64 GiB"
    }
    [pscustomobject]@{
        VhdPath = $ExpectedPath
        VhdType = "Fixed"
        VirtualSizeBytes = [int64]$image.Size
        FileSizeBytes = [int64]$FileItem.Length
        AllocatedBytes = [uint64]$AllocatedBytes
    }
}

function Get-VhdEvidence {
    param([bool]$Create)
    $created = $false
    try {
        $existing = $null
        try {
            $existing = Get-Item -LiteralPath $ExpectedPath -Force -ErrorAction Stop
        }
        catch [System.Management.Automation.ItemNotFoundException] {
            if (-not $Create) {
                throw "the fixed Symphony VHDX does not exist"
            }
            Invoke-DiskPartFixedVhdxCreate | Out-Null
            $created = $true
        }
        if ($created) {
            Set-OperatorStateAcl $ExpectedPath $false
        } else {
            Assert-OperatorStateAcl $ExpectedPath $false
        }
        $fileItem = Get-Item -LiteralPath $ExpectedPath -Force -ErrorAction Stop
        $images = @(Get-DiskImage -ImagePath $ExpectedPath -ErrorAction Stop)
        $allocatedBytes = Get-NativeAllocatedFileBytes $ExpectedPath
        Assert-NativeVhdxPostconditions $images $fileItem $allocatedBytes
    }
    catch {
        if ($created) {
            Remove-NewlyCreatedVhdx
        }
        throw
    }
}

function Assert-WslVhdCapability {
    $result = Invoke-WslText @("--help")
    # The help process exit status is not capability evidence: installed WSL
    # versions can emit valid usage text while returning a nonzero status.
    if (-not (Test-WslVhdHelpGrammar $result.Output)) {
        throw "installed WSL does not prove direct VHD attach/detach support"
    }
}

function Test-WslOptionToken {
    param(
        [AllowNull()][string]$Text,
        [Parameter(Mandatory)][string]$Option
    )
    if ([string]::IsNullOrWhiteSpace($Text)) {
        return $false
    }
    $pattern = "(?m)(?<!\S)" + [regex]::Escape($Option) + "(?!\S)"
    return [regex]::IsMatch($Text, $pattern)
}

function Test-WslVhdHelpGrammar {
    param([AllowNull()][string]$HelpText)

    # PowerShell can expose native wsl.exe help as NUL-interleaved text when
    # the executable reports UTF-16 output.  Normalize that representation
    # before parsing option tokens; it does not add or infer any option.
    $normalizedHelp = if ($null -eq $HelpText) {
        $null
    } else {
        $HelpText.Replace([string][char]0, "")
    }
    if ([string]::IsNullOrWhiteSpace($normalizedHelp)) {
        return $false
    }
    foreach ($option in @("--mount", "--vhd", "--bare", "--unmount")) {
        if (-not (Test-WslOptionToken $normalizedHelp $option)) {
            return $false
        }
    }

    # Keep --vhd and --bare tied to the structured --mount option region when
    # the help surface provides one.  This prevents an unrelated import/export
    # option from being mistaken for direct VHD mount support.
    $lines = @($normalizedHelp -split "`r?`n")
    $mountIndex = -1
    for ($index = 0; $index -lt $lines.Count; $index++) {
        if (Test-WslOptionToken $lines[$index] "--mount") {
            $mountIndex = $index
            break
        }
    }
    if ($mountIndex -lt 0) {
        return $false
    }
    $mountIndent = ([regex]::Match($lines[$mountIndex], '^\s*')).Value.Length
    $mountRegion = @($lines[$mountIndex])
    for ($index = $mountIndex + 1; $index -lt $lines.Count; $index++) {
        $line = $lines[$index]
        $optionMatch = [regex]::Match($line, '^\s*--[A-Za-z0-9-]+(?:\s|$)')
        if ($optionMatch.Success -and
            ([regex]::Match($line, '^\s*')).Value.Length -le $mountIndent) {
            break
        }
        $mountRegion += $line
    }
    $mountText = $mountRegion -join "`n"
    return (Test-WslOptionToken $mountText "--vhd") -and
        (Test-WslOptionToken $mountText "--bare")
}

function Throw-ReconciliationRequired {
    param([string]$Reason)
    $exception = New-Object -TypeName System.InvalidOperationException -ArgumentList $Reason
    $record = New-Object -TypeName System.Management.Automation.ErrorRecord -ArgumentList @(
        $exception,
        "VhdxReconciliationRequired",
        [System.Management.Automation.ErrorCategory]::ResourceBusy,
        $ExpectedPath
    )
    throw $record
}

function New-AttachmentCacheResult {
    param(
        [bool]$Present,
        [bool]$Valid,
        [AllowNull()][object]$State,
        [AllowNull()][string]$Error
    )
    [pscustomobject]@{
        Present = $Present
        Valid = $Valid
        State = $State
        Error = $Error
    }
}

function Read-AttachmentState {
    param([switch]$AllowInvalid)
    try {
        $parent = Get-Item -LiteralPath $ExpectedParent -Force -ErrorAction Stop
    }
    catch [System.Management.Automation.ItemNotFoundException] {
        return New-AttachmentCacheResult $false $true $null $null
    }
    if (-not $parent.PSIsContainer -or
        ($parent.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "VHDX attachment state parent is not a normal fixed namespace"
    }
    $stateName = [IO.Path]::GetFileName($AttachmentStatePath)
    $items = @(
        Get-ChildItem -LiteralPath $ExpectedParent -Force |
            Where-Object { $_.Name -eq $stateName }
    )
    if ($items.Count -eq 0) {
        return New-AttachmentCacheResult $false $true $null $null
    }
    if ($items.Count -ne 1) {
        throw "VHDX attachment state namespace is ambiguous"
    }
    $item = $items[0]
    $invalidReason = $null
    if ($item.PSIsContainer -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        $invalidReason = "VHDX attachment state is not a normal non-reparse file"
    } else {
        try {
            $state = Get-Content -LiteralPath $AttachmentStatePath -Raw | ConvertFrom-Json
            $expectedFields = @(
                "Schema", "VhdPath", "VhdType", "VirtualSizeBytes",
                "LinuxDevice", "Attached"
            )
            if ($null -eq $state) {
                throw "VHDX attachment state is malformed"
            }
            # The sidecar is bounded cache/recovery evidence, not authority.
            # Its fixed namespace, exact field set, and immutable contract
            # values prevent it from widening the VHD object identity; an
            # untrusted edit can only cause fail-closed reconciliation.
            $actualFields = @($state.PSObject.Properties.Name)
            if ($actualFields.Count -ne $expectedFields.Count -or
                @($actualFields | Where-Object { $_ -notin $expectedFields }).Count -ne 0 -or
                $state.Schema -ne $StateSchema -or
                $state.VhdPath -ne $ExpectedPath -or $state.VhdType -ne "Fixed" -or
                [int64]$state.VirtualSizeBytes -ne $ExpectedBytes -or
                $state.Attached -ne $true -or
                $state.LinuxDevice -notmatch '^/dev/[A-Za-z0-9._-]+$') {
                throw "VHDX attachment state conflicts with the fixed contract"
            }
        }
        catch {
            $invalidReason = $_.Exception.Message
        }
    }
    if ($null -ne $invalidReason) {
        if ($AllowInvalid) {
            return New-AttachmentCacheResult $true $false $null $invalidReason
        }
        throw $invalidReason
    }
    New-AttachmentCacheResult $true $true $state $null
}

function Resolve-DetachReconciliation {
    param(
        [object[]]$Before,
        [object[]]$After,
        [int]$UnmountExitCode
    )
    $beforeMap = @{}
    foreach ($device in @($Before)) {
        $key = Get-LinuxDeviceIdentityKey $device
        if ($beforeMap.ContainsKey($key)) {
            throw "before-detach Linux device evidence contains duplicate identities"
        }
        $beforeMap[$key] = $device
    }
    $afterMap = @{}
    foreach ($device in @($After)) {
        $key = Get-LinuxDeviceIdentityKey $device
        if ($afterMap.ContainsKey($key)) {
            throw "after-detach Linux device evidence contains duplicate identities"
        }
        $afterMap[$key] = $device
    }
    $missing = @(
        foreach ($key in @($beforeMap.Keys)) {
            if (-not $afterMap.ContainsKey($key)) {
                $beforeMap[$key]
            }
        }
    )
    $unexpected = @(
        foreach ($key in @($afterMap.Keys)) {
            if (-not $beforeMap.ContainsKey($key)) {
                $afterMap[$key]
            }
        }
    )
    if ($unexpected.Count -ne 0) {
        throw "WSL VHD detachment produced unexpected new Linux device evidence"
    }
    if ($missing.Count -gt 1) {
        throw "WSL VHD detachment removed more than one Linux device"
    }
    if ($missing.Count -eq 1 -and $missing[0].SizeBytes -ne $ExpectedBytes) {
        throw "WSL VHD detachment removed an unrelated Linux device"
    }
    if ($missing.Count -eq 0) {
        if (@($Before | Where-Object { $_.SizeBytes -eq $ExpectedBytes }).Count -ne 0) {
            throw "WSL VHD detachment did not prove removal of an exact-size Linux device"
        }
        return [pscustomobject]@{
            Action = "already-detached"
            LinuxDevice = $null
            UnmountExitCode = $UnmountExitCode
        }
    }
    # The fixed VHD path is the detachment capability.  A nonzero result is
    # recoverable only when the structured delta proves exactly one exact-size
    # device disappeared and the exact-path mutation succeeded; the evidence,
    # not a stale sidecar or a guessed /dev name, closes the reconciliation.
    if ($UnmountExitCode -ne 0) {
        throw "WSL VHD detachment did not succeed before device attribution"
    }
    [pscustomobject]@{
        Action = "reconciled-detached"
        LinuxDevice = [string]$missing[0].LinuxDevice
        UnmountExitCode = $UnmountExitCode
    }
}

function Write-AttachmentState {
    param([string]$LinuxDevice)
    $temporary = Join-Path $ExpectedParent (".symphony-vhdx-" + [guid]::NewGuid().ToString("N") + ".tmp")
    try {
        [ordered]@{
            Schema = $StateSchema
            VhdPath = $ExpectedPath
            VhdType = "Fixed"
            VirtualSizeBytes = $ExpectedBytes
            LinuxDevice = $LinuxDevice
            Attached = $true
        } | ConvertTo-Json -Compress | Set-Content -LiteralPath $temporary -Encoding utf8
        Set-OperatorStateAcl $temporary $false
        Move-Item -LiteralPath $temporary -Destination $AttachmentStatePath -Force
        Assert-OperatorStateAcl $AttachmentStatePath $false
    }
    finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

function Remove-AttachmentState {
    try {
        $item = Get-Item -LiteralPath $AttachmentStatePath -Force -ErrorAction Stop
    }
    catch [System.Management.Automation.ItemNotFoundException] {
        return
    }
    if ($item.PSIsContainer) {
        throw "VHDX attachment state leaf is unexpectedly a directory"
    }
    $isReparse = ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
    # LiteralPath deletion targets only this fixed leaf.  Reparse state is
    # never opened or traversed; the postcondition proves the leaf vanished.
    # This explicit branch documents that reparse cleanup is a leaf operation,
    # not a followed path; if the platform cannot honor that contract,
    # Remove-Item fails and the recovery state remains for bounded retry.
    if ($isReparse) {
        Remove-Item -LiteralPath $AttachmentStatePath -Force -ErrorAction Stop
    } else {
        [IO.File]::Delete($AttachmentStatePath)
    }
    if (Test-Path -LiteralPath $AttachmentStatePath) {
        throw "VHDX attachment state leaf could not be removed"
    }
}

function New-Evidence {
    param(
        [object]$Vhd,
        [AllowNull()][string]$LinuxDevice,
        [string]$Action,
        [string]$State,
        [object[]]$LinuxDevicesBefore = @(),
        [object[]]$LinuxDevicesAfter = @(),
        [AllowNull()][object]$Cache = $null
    )
    [ordered]@{
        VhdPath = $Vhd.VhdPath
        VhdType = $Vhd.VhdType
        VirtualSizeBytes = $Vhd.VirtualSizeBytes
        FileSizeBytes = $Vhd.FileSizeBytes
        LinuxDevice = $LinuxDevice
        LinuxDevicesBefore = @($LinuxDevicesBefore | ForEach-Object { $_.LinuxDevice })
        LinuxDevicesAfter = @($LinuxDevicesAfter | ForEach-Object { $_.LinuxDevice })
        AttachmentCacheState = if ($null -eq $Cache -or -not $Cache.Present) {
            "absent"
        } elseif ($Cache.Valid) {
            "valid-cache"
        } else {
            "invalid-cache"
        }
        WslAttachmentState = $State
        AttachmentAction = $Action
        Distribution = $Distribution
    } | ConvertTo-Json -Compress
}

Ensure-OperatorStateNamespace ($Operation -eq "Attach")
Assert-WslVhdCapability

if ($Operation -eq "Attach") {
    $vhd = Get-VhdEvidence $true
    $before = @(Get-LinuxWholeDiskEvidence)
    $cache = Read-AttachmentState
    $exactBefore = @($before | Where-Object { $_.SizeBytes -eq $ExpectedBytes })

    if ($cache.Present) {
        # Reviewed WSL exposes no direct exact-VHD attachment-state query.
        # A cached /dev path plus size is therefore never sufficient to bless
        # already-attached; the bounded Detach path must reconcile it first.
        Throw-ReconciliationRequired "attachment reconciliation required before Attach can proceed"
    }
    if ($exactBefore.Count -ne 0) {
        throw "an untracked exact-size Linux disk is a conflicting attachment"
    }

    $attached = Invoke-WslText @("--mount", $ExpectedPath, "--vhd", "--bare")
    if ($attached.ExitCode -ne 0) {
        throw "direct WSL VHD attachment failed: $($attached.Output)"
    }
    $after = @(Get-LinuxWholeDiskEvidence)
    $new = @($after | Where-Object {
        $candidate = $_.IdentityKey
        -not (@($before | Where-Object { $_.IdentityKey -eq $candidate }).Count)
    })
    if ($new.Count -ne 1 -or $new[0].SizeBytes -ne $ExpectedBytes) {
        throw "direct VHD attachment did not produce exactly one new 64-GiB Linux disk"
    }
    Write-AttachmentState $new[0].LinuxDevice
    New-Evidence $vhd $new[0].LinuxDevice "attached" "attached" $before $after
    exit 0
}

$vhd = Get-VhdEvidence $false
$before = @(Get-LinuxWholeDiskEvidence)
$cache = Read-AttachmentState -AllowInvalid
$detached = Invoke-WslText @("--unmount", $ExpectedPath)
$after = @(Get-LinuxWholeDiskEvidence)
$reconciliation = Resolve-DetachReconciliation $before $after $detached.ExitCode
if ($cache.Present) {
    Remove-AttachmentState
}
$reconciliation.LinuxDevice = if ($null -ne $reconciliation.LinuxDevice) {
    [string]$reconciliation.LinuxDevice
} else {
    $null
}
New-Evidence $vhd $reconciliation.LinuxDevice $reconciliation.Action "detached" $before $after $cache
exit 0
