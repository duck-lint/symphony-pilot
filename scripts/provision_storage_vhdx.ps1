#requires -Version 7.2
<##
.SYNOPSIS
  Create/verify and explicitly attach or detach the fixed Symphony VHDX.

This is an operator-only attachment contract.  The path and VHD properties
are fixed in source; Linux provisioning is a separate phase that must run
while the reported WSL attachment remains live.  No Hyper-V disk mount is
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
$Distribution = "Ubuntu-24.04"
$Wsl = [IO.Path]::Combine($env:SystemRoot, "System32", "wsl.exe")
$StateSchema = "symphony-pilot-vhdx-attachment/v1"

if (-not (Test-Path -LiteralPath $Wsl -PathType Leaf)) {
    throw "the fixed Windows WSL executable is unavailable"
}
if ($Operation -eq "Attach" -and -not (Test-Path -LiteralPath $ExpectedParent -PathType Container)) {
    New-Item -ItemType Directory -Path $ExpectedParent -Force | Out-Null
}

function Invoke-WslText {
    param([string[]]$Arguments)
    $output = & $Wsl @Arguments 2>&1 | Out-String
    [pscustomobject]@{
        ExitCode = [int]$LASTEXITCODE
        Output = $output.Trim()
    }
}

function Get-LinuxWholeDiskEvidence {
    $result = Invoke-WslText @(
        "--distribution", $Distribution, "--exec", "/bin/lsblk",
        "--json", "--bytes", "--output", "NAME,PATH,TYPE,SIZE,PKNAME"
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
        if ($device.type -eq "disk" -and [int64]$device.size -eq $ExpectedBytes -and
            $null -eq $device.pkname -and $device.path -match '^/dev/[A-Za-z0-9._-]+$') {
            [pscustomobject]@{
                LinuxDevice = [string]$device.path
                Type = [string]$device.type
                SizeBytes = [int64]$device.size
                Parent = $null
            }
        }
    }
    @($evidence)
}

function Get-VhdEvidence {
    param([bool]$Create)
    if (-not (Test-Path -LiteralPath $ExpectedPath -PathType Leaf)) {
        if (-not $Create) {
            throw "the fixed Symphony VHDX does not exist"
        }
        New-VHD -Path $ExpectedPath -SizeBytes $ExpectedBytes -Fixed | Out-Null
    }
    $vhd = Get-VHD -Path $ExpectedPath
    if ([string]$vhd.VhdType -ne "Fixed") {
        throw "dynamic or differencing VHDs are rejected"
    }
    if ([int64]$vhd.Size -ne $ExpectedBytes) {
        throw "VHD virtual capacity must be exactly 64 GiB"
    }
    [pscustomobject]@{
        VhdPath = $ExpectedPath
        VhdType = [string]$vhd.VhdType
        VirtualSizeBytes = [int64]$vhd.Size
        FileSizeBytes = [int64]$vhd.FileSize
    }
}

function Assert-WslVhdCapability {
    $result = Invoke-WslText @("--help")
    if ($result.ExitCode -ne 0 -or $result.Output -notmatch "--vhd" -or
        $result.Output -notmatch "--unmount") {
        throw "installed WSL does not prove direct VHD attach/detach support"
    }
}

function Read-AttachmentState {
    if (-not (Test-Path -LiteralPath $AttachmentStatePath -PathType Leaf)) {
        return $null
    }
    $item = Get-Item -LiteralPath $AttachmentStatePath -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "VHDX attachment state is a reparse point"
    }
    try {
        $state = Get-Content -LiteralPath $AttachmentStatePath -Raw | ConvertFrom-Json
    }
    catch {
        throw "VHDX attachment state is malformed"
    }
    if ($null -eq $state -or $state.Schema -ne $StateSchema -or
        $state.VhdPath -ne $ExpectedPath -or $state.VhdType -ne "Fixed" -or
        [int64]$state.VirtualSizeBytes -ne $ExpectedBytes -or
        $state.Attached -ne $true -or
        $state.LinuxDevice -notmatch '^/dev/[A-Za-z0-9._-]+$') {
        throw "VHDX attachment state conflicts with the fixed contract"
    }
    $state
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
        Move-Item -LiteralPath $temporary -Destination $AttachmentStatePath -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

function New-Evidence {
    param(
        [object]$Vhd,
        [AllowNull()][string]$LinuxDevice,
        [string]$Action,
        [string]$State
    )
    [ordered]@{
        VhdPath = $Vhd.VhdPath
        VhdType = $Vhd.VhdType
        VirtualSizeBytes = $Vhd.VirtualSizeBytes
        FileSizeBytes = $Vhd.FileSizeBytes
        LinuxDevice = $LinuxDevice
        WslAttachmentState = $State
        AttachmentAction = $Action
        Distribution = $Distribution
    } | ConvertTo-Json -Compress
}

Assert-WslVhdCapability

if ($Operation -eq "Attach") {
    $vhd = Get-VhdEvidence $true
    $before = @(Get-LinuxWholeDiskEvidence)
    $state = Read-AttachmentState
    $exactBefore = @($before | Where-Object { $_.SizeBytes -eq $ExpectedBytes })

    if ($null -ne $state) {
        if (@($exactBefore | Where-Object { $_.LinuxDevice -eq $state.LinuxDevice }).Count -ne 1 -or
            $exactBefore.Count -ne 1) {
            throw "tracked VHDX attachment is missing or conflicts with another 64-GiB Linux disk"
        }
        New-Evidence $vhd $state.LinuxDevice "already-attached" "attached"
        exit 0
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
        $candidate = $_.LinuxDevice
        -not (@($before | Where-Object { $_.LinuxDevice -eq $candidate }).Count)
    })
    if ($new.Count -ne 1 -or $new[0].SizeBytes -ne $ExpectedBytes) {
        throw "direct VHD attachment did not produce exactly one new 64-GiB Linux disk"
    }
    Write-AttachmentState $new[0].LinuxDevice
    New-Evidence $vhd $new[0].LinuxDevice "attached" "attached"
    exit 0
}

$vhd = Get-VhdEvidence $false
$before = @(Get-LinuxWholeDiskEvidence)
$state = Read-AttachmentState
$exactBefore = @($before | Where-Object { $_.SizeBytes -eq $ExpectedBytes })
if ($null -eq $state) {
    if ($exactBefore.Count -ne 0) {
        throw "an untracked exact-size Linux disk is a conflicting attachment"
    }
    New-Evidence $vhd $null "already-detached" "detached"
    exit 0
}
if ($exactBefore.Count -ne 1 -or $exactBefore[0].LinuxDevice -ne $state.LinuxDevice) {
    throw "tracked VHDX attachment is missing or conflicts with another 64-GiB Linux disk"
}
$detached = Invoke-WslText @("--unmount", $ExpectedPath)
if ($detached.ExitCode -ne 0) {
    throw "direct WSL VHD detachment failed: $($detached.Output)"
}
$after = @(Get-LinuxWholeDiskEvidence)
if (@($after | Where-Object { $_.LinuxDevice -eq $state.LinuxDevice }).Count -ne 0 -or
    @($after | Where-Object { $_.SizeBytes -eq $ExpectedBytes }).Count -ne 0) {
    throw "WSL VHD detachment did not remove the tracked Linux device"
}
Remove-Item -LiteralPath $AttachmentStatePath -Force
New-Evidence $vhd $state.LinuxDevice "detached" "detached"
