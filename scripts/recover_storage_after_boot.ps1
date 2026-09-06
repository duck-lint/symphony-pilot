#requires -Version 7.2
<#!
.SYNOPSIS
    Recover the fixed Symphony storage domain after a Windows/WSL restart.

This is one bounded elevated operator action.  It has no caller-selected
paths, devices, commands, or storage properties.  Attachment authority stays
in provision_storage_vhdx.ps1; this script only consumes its structured
Attach evidence, or verifies an already-mounted accepted pool before reusing
it.  The Linux provisioner is the immutable deployed copy at the fixed
canary path and remains responsible for its own deployment verification.
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ExpectedSourceRoot = "F:\PROJECT-REPOS\symphony-pilot"
$ExpectedOperatorRoot = Join-Path $ExpectedSourceRoot "scripts"
$ExpectedPath = "C:\ProgramData\SymphonyPilot\symphony-storage.vhdx"
$ExpectedBytes = [int64]64GB
$Distribution = "Ubuntu-24.04"
$Wsl = [IO.Path]::Combine($env:SystemRoot, "System32", "wsl.exe")
$PowerShell = [IO.Path]::Combine($PSHOME, "pwsh.exe")
$AttachScript = Join-Path $ExpectedOperatorRoot "provision_storage_vhdx.ps1"
$Provisioner = "/home/duck-lint/.local/share/symphony-pilot/deployments/symphony-canary/scripts/provision_storage_domain.sh"
$PoolRoot = "/home/duck-lint/symphony-workspaces"
$StorageIdentity = "/var/lib/symphony-pilot/storage-domain.identity.json"
$QuotaHelper = "/var/lib/symphony-pilot/quota-admit-task"

function Fail-Recovery {
    param([Parameter(Mandatory)][string]$Message)
    throw "Symphony storage recovery stopped: $Message"
}

function Assert-FixedOperatorPaths {
    if (-not [Environment]::Is64BitOperatingSystem) {
        Fail-Recovery "the reviewed storage operator requires a 64-bit Windows host"
    }
    if (-not (Test-Path -LiteralPath $Wsl -PathType Leaf)) {
        Fail-Recovery "the fixed Windows WSL executable is unavailable"
    }
    if (([IO.Path]::GetFullPath($PSScriptRoot)) -ne
        ([IO.Path]::GetFullPath($ExpectedOperatorRoot))) {
        Fail-Recovery "the recovery operator is not running from the fixed source path"
    }
    if (-not (Test-Path -LiteralPath $AttachScript -PathType Leaf)) {
        Fail-Recovery "the reviewed VHDX Attach operator is unavailable"
    }
    $attachItem = Get-Item -LiteralPath $AttachScript -Force
    if (($attachItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        Fail-Recovery "the reviewed VHDX Attach operator is a reparse point"
    }
}

function Assert-Elevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Fail-Recovery "the post-boot recovery action must run elevated"
    }
}

function Invoke-FixedLinuxCommand {
    param([Parameter(Mandatory)][string[]]$Arguments)
    # This is a private fixed-command allowlist, not a generic WSL broker.
    # Every executable below is literal; only a read-only device observed by
    # findmnt may be carried into the later fixed inspection commands.
    $allowed = @(
        "/usr/bin/findmnt", "/usr/sbin/blkid", "/usr/sbin/blockdev",
        "/usr/sbin/tune2fs", "/usr/bin/cat", "/var/lib/symphony-pilot/quota-admit-task"
    )
    if ($Arguments.Count -lt 1 -or $Arguments[0] -notin $allowed) {
        Fail-Recovery "internal Linux recovery command is outside the reviewed allowlist"
    }
    $wslArguments = @("--distribution", $Distribution, "--user", "root", "--") + $Arguments
    $output = (& $Wsl @wslArguments 2>&1 | Out-String).Trim()
    [pscustomobject]@{
        ExitCode = [int]$LASTEXITCODE
        Output = $output
    }
}

function Get-LinuxFieldMap {
    param([Parameter(Mandatory)][string]$Text)
    $fields = @{}
    foreach ($line in ($Text -split "`r?`n")) {
        if ($line -match '^([A-Z0-9_]+)=(.*)$') {
            $fields[$Matches[1]] = $Matches[2].Trim('"')
        }
    }
    $fields
}

function Get-MountedPoolEvidence {
    param([switch]$RequireIdentity)
    $target = Invoke-FixedLinuxCommand @("/usr/bin/findmnt", "--noheadings", "--output", "TARGET", "--target", $PoolRoot)
    if ($target.ExitCode -ne 0) {
        if ([string]::IsNullOrWhiteSpace($target.Output)) { return $null }
        Fail-Recovery "the workspace mount could not be inspected"
    }
    if ($target.Output.Trim() -ne $PoolRoot) {
        Fail-Recovery "the workspace mount target is unexpected"
    }
    $sourceResult = Invoke-FixedLinuxCommand @("/usr/bin/findmnt", "--noheadings", "--output", "SOURCE", "--target", $PoolRoot)
    $typeResult = Invoke-FixedLinuxCommand @("/usr/bin/findmnt", "--noheadings", "--output", "FSTYPE", "--target", $PoolRoot)
    $optionsResult = Invoke-FixedLinuxCommand @("/usr/bin/findmnt", "--noheadings", "--output", "OPTIONS", "--target", $PoolRoot)
    if ($sourceResult.ExitCode -ne 0 -or $typeResult.ExitCode -ne 0 -or $optionsResult.ExitCode -ne 0) {
        Fail-Recovery "the workspace mount evidence is incomplete"
    }
    $device = $sourceResult.Output.Trim()
    if ($device -notmatch '^/dev/[A-Za-z0-9._-]+$' -or $device -match '^/dev/sdd(?:/|$)') {
        Fail-Recovery "the workspace mount source is not an approved dedicated device"
    }
    if ($typeResult.Output.Trim() -ne "ext4") {
        Fail-Recovery "the workspace filesystem is not ext4"
    }
    $options = "," + $optionsResult.Output.Trim() + ","
    if ($options -notmatch ',(?:prjquota|pquota),') {
        Fail-Recovery "the workspace mount does not enforce project quotas"
    }

    $blkid = Invoke-FixedLinuxCommand @("/usr/sbin/blkid", "-o", "export", "--", $device)
    if ($blkid.ExitCode -ne 0) { Fail-Recovery "the dedicated filesystem identity could not be read" }
    $fields = Get-LinuxFieldMap $blkid.Output
    if ($fields["TYPE"] -ne "ext4" -or $fields["LABEL"] -ne "SYMPHONY-POOL" -or
        $fields["UUID"] -notmatch '^[0-9a-fA-F-]{36}$') {
        Fail-Recovery "the existing filesystem identity is not the accepted Symphony pool"
    }
    $size = Invoke-FixedLinuxCommand @("/usr/sbin/blockdev", "--getsize64", $device)
    if ($size.ExitCode -ne 0 -or $size.Output.Trim() -ne [string]$ExpectedBytes) {
        Fail-Recovery "the mounted dedicated device is not exactly 64 GiB"
    }
    $features = Invoke-FixedLinuxCommand @("/usr/sbin/tune2fs", "-l", "--", $device)
    if ($features.ExitCode -ne 0 -or
        $features.Output -notmatch '(?m)^Filesystem features:.*\bproject\b' -or
        $features.Output -notmatch '(?m)^Filesystem features:.*\bquota\b' -or
        $features.Output -notmatch '(?m)^Project quota inode:\s*[1-9][0-9]*\s*$' -or
        $features.Output -notmatch '(?m)^Reserved block count:\s*0\s*$') {
        Fail-Recovery "the existing filesystem lacks the reviewed quota features"
    }

    $identityResult = Invoke-FixedLinuxCommand @("/usr/bin/cat", $StorageIdentity)
    $identity = $null
    if ($identityResult.ExitCode -eq 0) {
        try { $identity = $identityResult.Output | ConvertFrom-Json }
        catch { Fail-Recovery "the storage-domain identity record is malformed" }
        if ($identity.schema -ne "symphony-pilot-storage-domain/v1" -or
            $identity.pool_label -ne "SYMPHONY-POOL" -or $identity.filesystem -ne "ext4" -or
            $identity.mount_target -ne $PoolRoot -or $identity.filesystem_uuid -notmatch '^[0-9a-fA-F-]{36}$') {
            Fail-Recovery "the storage-domain identity record conflicts with the fixed pool contract"
        }
        if ($identity.filesystem_uuid -ne $fields["UUID"]) {
            Fail-Recovery "the live filesystem UUID differs from the accepted storage identity"
        }
    } elseif (-not [string]::IsNullOrWhiteSpace($identityResult.Output) -or $RequireIdentity) {
        Fail-Recovery "the accepted storage-domain identity record is unavailable"
    }
    [pscustomobject]@{
        LinuxDevice = $device
        FilesystemUuid = $fields["UUID"].ToLowerInvariant()
        IdentityEstablished = ($null -ne $identity)
    }
}

function Invoke-IsolatedAttachOperator {
    if (-not (Test-Path -LiteralPath $PowerShell -PathType Leaf)) {
        Fail-Recovery "the current trusted PowerShell 7 executable is unavailable"
    }
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $PowerShell
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    # ArgumentList performs native argv construction.  There is no shell,
    # caller-provided text, or selectable operation/path in this entry point.
    foreach ($argument in @(
        "-NoProfile", "-NonInteractive", "-File", $AttachScript,
        "-Operation", "Attach"
    )) {
        [void]$start.ArgumentList.Add($argument)
    }
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $start
    try {
        if (-not $process.Start()) {
            Fail-Recovery "the isolated VHDX Attach operator could not be started"
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        [pscustomobject]@{
            ExitCode = [int]$process.ExitCode
            Stdout = $stdoutTask.GetAwaiter().GetResult().Trim()
            Stderr = $stderrTask.GetAwaiter().GetResult().Trim()
        }
    }
    catch [System.Management.Automation.RuntimeException] {
        throw
    }
    catch {
        Fail-Recovery "the isolated VHDX Attach operator failed to execute"
    }
    finally {
        $process.Dispose()
    }
}

function Invoke-ReviewedAttach {
    $child = Invoke-IsolatedAttachOperator
    $output = $child.Stdout
    $diagnostic = ($child.Stdout + "`n" + $child.Stderr).Trim()
    $exitCode = $child.ExitCode
    if ($exitCode -eq 0) {
        $jsonLine = ($output -split "`r?`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Last 1)
        try { $evidence = $jsonLine | ConvertFrom-Json }
        catch { Fail-Recovery "the reviewed Attach returned malformed evidence" }
        if ($evidence.VhdPath -ne $ExpectedPath -or $evidence.VhdType -ne "Fixed" -or
            [int64]$evidence.VirtualSizeBytes -ne $ExpectedBytes -or
            [int]$evidence.ProviderSubtype -ne 2 -or
            $evidence.AttachmentAction -ne "attached" -or
            $evidence.LinuxDevice -notmatch '^/dev/[A-Za-z0-9._-]+$' -or
            $evidence.LinuxDevice -match '^/dev/sdd(?:/|$)') {
            Fail-Recovery "Attach evidence does not prove the fixed Symphony VHD and one attributable Linux device"
        }
        return [pscustomobject]@{ Status = "attached"; Evidence = $evidence }
    }
    # Existing Attach intentionally refuses cached/ambiguous attachment
    # identity.  Only that typed reconciliation route may fall through to a
    # read-only accepted-mount proof; every other Attach failure is fatal.
    # Native child-process formatting preserves the reviewed terminating
    # message, but not PowerShell's in-process ErrorRecord metadata.  The
    # exact message is therefore the bounded cross-process representation of
    # VhdxReconciliationRequired here; no broader error text is accepted.
    $reconciliationMessage = $child.Stderr -match
        "attachment reconciliation required before Attach can proceed"
    $messageReconciliation = $diagnostic -match
        "untracked exact-size Linux disk is a conflicting attachment"
    if ($reconciliationMessage -or $messageReconciliation) {
        return [pscustomobject]@{ Status = "reconciliation-required"; Evidence = $null }
    }
    Fail-Recovery "the reviewed VHDX Attach failed"
}

function Invoke-DeployedProvisioner {
    param([Parameter(Mandatory)][string]$LinuxDevice)
    $arguments = @("--distribution", $Distribution, "--user", "root", "--", "/bin/sh", $Provisioner, $LinuxDevice)
    $output = (& $Wsl @arguments 2>&1 | Out-String).Trim()
    if ([int]$LASTEXITCODE -ne 0) {
        Fail-Recovery "the frozen deployed Linux storage provisioner failed"
    }
}

function Invoke-QuotaPoolVerification {
    $result = Invoke-FixedLinuxCommand @($QuotaHelper, "--operation", "verify-pool")
    if ($result.ExitCode -ne 0) {
        Fail-Recovery "the quota helper verify-pool operation failed"
    }
}

function Invoke-StorageRecovery {
    Assert-Elevated
    Assert-FixedOperatorPaths
    $attach = Invoke-ReviewedAttach
    if ($attach.Status -eq "attached") {
        $device = [string]$attach.Evidence.LinuxDevice
    } else {
        $mounted = Get-MountedPoolEvidence -RequireIdentity
        if ($null -eq $mounted) {
            Fail-Recovery "Attach requires reconciliation, but the accepted pool is not mounted"
        }
        $device = $mounted.LinuxDevice
    }
    Invoke-DeployedProvisioner $device
    $final = Get-MountedPoolEvidence -RequireIdentity
    if ($null -eq $final -or $final.LinuxDevice -ne $device) {
        Fail-Recovery "the fixed workspace pool was not mounted on the attributed device"
    }
    Invoke-QuotaPoolVerification
    [ordered]@{
        Schema = "symphony-pilot-storage-recovery/v1"
        VhdPath = $ExpectedPath
        LinuxDevice = $device
        FilesystemUuid = $final.FilesystemUuid
        AttachmentRoute = $attach.Status
        Provisioner = $Provisioner
        MountTarget = $PoolRoot
        QuotaVerification = "verify-pool"
    } | ConvertTo-Json -Compress
}

if ($MyInvocation.InvocationName -ne ".") {
    Invoke-StorageRecovery
}
