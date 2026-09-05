#requires -Version 7.2
<#
.SYNOPSIS
  Verify or create the fixed Windows backing object for the Symphony pool.

This is an operator-only precondition. It is deliberately not a general
Windows command broker and never resizes an existing virtual disk.
#>
[CmdletBinding()]
param(
    [string]$Path = "C:\ProgramData\SymphonyPilot\symphony-storage.vhdx"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ExpectedBytes = 64GB
$ExpectedPath = [IO.Path]::GetFullPath($Path)
$ExpectedParent = "C:\ProgramData\SymphonyPilot"

if ($ExpectedPath -ne [IO.Path]::Combine($ExpectedParent, "symphony-storage.vhdx")) {
    throw "the Symphony backing path is fixed and cannot be operator-selected"
}
if (-not (Test-Path -LiteralPath $ExpectedParent -PathType Container)) {
    New-Item -ItemType Directory -Path $ExpectedParent -Force | Out-Null
}

if (-not (Test-Path -LiteralPath $ExpectedPath -PathType Leaf)) {
    New-VHD -Path $ExpectedPath -SizeBytes $ExpectedBytes -Fixed | Out-Null
}

$vhd = Get-VHD -Path $ExpectedPath
if ($vhd.VhdType -ne "Fixed") {
    throw "dynamic or differencing VHDs are rejected"
}
if ([int64]$vhd.Size -ne $ExpectedBytes -or [int64]$vhd.FileSize -ne $ExpectedBytes) {
    throw "VHD must be exactly 64 GiB and fully allocated"
}

Mount-VHD -Path $ExpectedPath -NoDriveLetter
try {
    $disk = Get-Disk | Where-Object { $_.Location -like "*${ExpectedPath}*" }
    if ($null -eq $disk -or @($disk).Count -ne 1) {
        throw "the fixed VHD did not resolve to one dedicated disk"
    }
    if ($disk.IsReadOnly -or $disk.IsOffline) {
        throw "the dedicated disk is not usable"
    }
    $device = "\\.\PHYSICALDRIVE$($disk.Number)"
    & "C:\Windows\System32\wsl.exe" --distribution "Ubuntu-24.04" --mount $device --bare
    if ($LASTEXITCODE -ne 0) {
        throw "WSL bare attachment failed"
    }
    [pscustomobject]@{
        VhdPath = $ExpectedPath
        VhdType = $vhd.VhdType
        VirtualSizeBytes = [int64]$vhd.Size
        FileSizeBytes = [int64]$vhd.FileSize
        LinuxDevice = $device
        Distribution = "Ubuntu-24.04"
    } | ConvertTo-Json -Compress
}
finally {
    # The Linux provisioning phase owns filesystem creation. This script only
    # attaches the verified fixed object and never formats or grows it.
    Dismount-VHD -Path $ExpectedPath
}
