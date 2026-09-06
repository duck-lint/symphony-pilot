param(
    [Parameter(Mandatory = $true)]
    [string] $OutputPath,
    [switch] $IncludeWsl
)

$ErrorActionPreference = "Stop"
$os = Get-CimInstance Win32_OperatingSystem
$current = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion"
$buildFamily = "10.0.$($current.CurrentBuild)"
$build = "$buildFamily.$($current.UBR)"
$powershell = $PSVersionTable
$windowsArchitecture = if ([string]$os.OSArchitecture -match "64") { "AMD64" } else { [string]$os.OSArchitecture }
$product = Get-ComputerInfo -Property WindowsProductName

$observed = [ordered]@{
    windows = [ordered]@{
        architecture = $windowsArchitecture
        build_family = $buildFamily
        build = $build
        product_label = [string]$product.WindowsProductName
        windows_version = [string]$current.ReleaseId
    }
    powershell = [ordered]@{
        edition = [string]$powershell.PSEdition
        version = [string]$powershell.PSVersion
        platform = [string]$powershell.Platform
    }
}

if ($IncludeWsl) {
    $wslVersion = (& "$env:SystemRoot\System32\wsl.exe" --version 2>&1 | Out-String).Trim()
    $wslStatus = (& "$env:SystemRoot\System32\wsl.exe" --status 2>&1 | Out-String).Trim()
    $distro = (& "$env:SystemRoot\System32\wsl.exe" -d Ubuntu-24.04 -- cat /etc/os-release 2>&1 | Out-String).Trim()
    $uname = (& "$env:SystemRoot\System32\wsl.exe" -d Ubuntu-24.04 -- uname -r 2>&1 | Out-String).Trim()
    $cc = (& "$env:SystemRoot\System32\wsl.exe" -d Ubuntu-24.04 -- /usr/bin/cc --version 2>&1 | Out-String).Trim()
    $glibc = (& "$env:SystemRoot\System32\wsl.exe" -d Ubuntu-24.04 -- ldd --version 2>&1 | Out-String).Trim()
    $e2fs = (& "$env:SystemRoot\System32\wsl.exe" -d Ubuntu-24.04 -- mke2fs -V 2>&1 | Out-String).Trim()
    $mount = (& "$env:SystemRoot\System32\wsl.exe" -d Ubuntu-24.04 -- mount --version 2>&1 | Out-String).Trim()
    $findmnt = (& "$env:SystemRoot\System32\wsl.exe" -d Ubuntu-24.04 -- findmnt --version 2>&1 | Out-String).Trim()
    $quotactl = (& "$env:SystemRoot\System32\wsl.exe" -d Ubuntu-24.04 -- grep -R -h -m 1 "#define __NR_quotactl_fd" /usr/include/x86_64-linux-gnu/asm /usr/include/asm-generic 2>&1 | Out-String).Trim()
    $versionId = ($distro -split "`n" | Where-Object { $_ -like "VERSION_ID=*" } | Select-Object -First 1) -replace '^VERSION_ID=', '' -replace '"', ''
    $pretty = ($distro -split "`n" | Where-Object { $_ -like "PRETTY_NAME=*" } | Select-Object -First 1) -replace '^PRETTY_NAME=', '' -replace '"', ''
    $codename = ($distro -split "`n" | Where-Object { $_ -like "VERSION_CODENAME=*" } | Select-Object -First 1) -replace '^VERSION_CODENAME=', '' -replace '"', ''
    $wslDefault = (($wslStatus -split "`n" | Where-Object { $_ -match "Default Version" } | Select-Object -First 1) -replace '.*:\s*', '').Trim()
    $distroVersion = (& "$env:SystemRoot\System32\wsl.exe" -l -v 2>&1 | Out-String).Trim()
    $distributionVersion = if ($distroVersion -match "Ubuntu-24\.04\s+\S+\s+(\d+)") { $Matches[1] } else { "" }
    $wslVersionValue = if ($wslVersion -match "WSL version:\s*([0-9.]+)") { $Matches[1] } else { "" }
    $kernelPackage = if ($wslVersion -match "Kernel version:\s*([0-9.\-]+)") { $Matches[1] } else { "" }
    $wslg = if ($wslVersion -match "WSLg version:\s*([0-9.]+)") { $Matches[1] } else { "" }
    $msrdc = if ($wslVersion -match "MSRDC version:\s*([0-9.]+)") { $Matches[1] } else { "" }
    $direct3d = if ($wslVersion -match "Direct3D version:\s*([^`r`n]+)") { $Matches[1].Trim() } else { "" }
    $dxcore = if ($wslVersion -match "DXCore version:\s*([^`r`n]+)") { $Matches[1].Trim() } else { "" }
    $reportedWindows = if ($wslVersion -match "Windows version:\s*([0-9.]+)") { $Matches[1] } else { "" }
    $compilerLine = ($cc -split "`n" | Select-Object -First 1).Trim()
    $glibcLine = if ($glibc -match "(\d+\.\d+-[^\s\)]+)") { $Matches[1] } else { ($glibc -split "`n" | Select-Object -First 1).Trim() }
    $e2fsLine = if ($e2fs -match "mke2fs\s+(\d+\.\d+\.\d+)") { $Matches[1] } else { ($e2fs -split "`n" | Select-Object -First 1).Trim() }
    $mountLine = if ($mount -match "util-linux\s+(\d+\.\d+\.\d+)") { $Matches[1] } else { ($mount -split "`n" | Select-Object -First 1).Trim() }
    $findmntLine = if ($findmnt -match "util-linux\s+(\d+\.\d+\.\d+)") { $Matches[1] } else { ($findmnt -split "`n" | Select-Object -First 1).Trim() }
    $quotactlValue = if ($quotactl -match "quotactl_fd\s+(\d+)") { [int]$Matches[1] } else { $null }
    $observed.wsl = [ordered]@{
        version = $wslVersionValue
        kernel_package_version = $kernelPackage
        uname_kernel = $uname
        wslg = $wslg
        msrdc = $msrdc
        direct3d = $direct3d
        dxcore = $dxcore
        reported_windows = $reportedWindows
        default_version = $wslDefault
        distribution = "Ubuntu-24.04"
        distribution_version = $distributionVersion
    }
    $observed.ubuntu = [ordered]@{
        distro = "Ubuntu"
        version = $pretty
        version_id = $versionId
        codename = $codename
        architecture = "x86_64"
        compiler = "/usr/bin/cc"
        compiler_version = $compilerLine
        glibc = $glibcLine
        e2fsprogs = $e2fsLine
        util_linux = [ordered]@{ mount = $mountLine; findmnt = $findmntLine }
        quotactl_fd = $quotactlValue
    }
    $observed.paths = [ordered]@{
        pilot_source_root = "F:\\PROJECT-REPOS\\SYMPHONY\\symphony-pilot"
        runtime_source_root = "F:\\PROJECT-REPOS\\SYMPHONY\\symphony-runtime"
        canary_source_root = "F:\\PROJECT-REPOS\\SYMPHONY\\symphony-canary"
        wsl_distribution = "Ubuntu-24.04"
        wsl_user = "duck-lint"
        deployed_pilot_root = "/home/duck-lint/.local/share/symphony-pilot/deployments/symphony-canary"
        workspace_root = "/home/duck-lint/symphony-workspaces"
    }
}

$parent = Split-Path -Parent $OutputPath
New-Item -ItemType Directory -Force -Path $parent | Out-Null
$observed | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $OutputPath -Encoding utf8NoBOM
