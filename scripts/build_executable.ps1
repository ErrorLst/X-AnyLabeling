#Requires -Version 5.1
<#
.SYNOPSIS
Build the Windows portable single-file X-AnyLabeling executable.

.DESCRIPTION
This is the Windows-native replacement for scripts/build_executable.sh.
It defaults to the win-cpu onefile target and can also build win-gpu.

The script does not use Bash. It:
  1. finds a Python that has PyInstaller and PyQt6;
  2. runs prepare_windows_build.ps1, which refuses a Low integrity
     terminal and sets the dist tree to Medium before PyInstaller;
  3. runs PyInstaller with the Windows single-file spec;
  4. runs clear_windows_sandbox_acls.ps1 on the output exe, which
     removes sandbox access entries and resets the integrity label to
     Medium;
  5. writes a .sha256 sidecar next to the exe.

.PARAMETER Target
win-cpu (default) or win-gpu.

.PARAMETER Variant
CPU for win-cpu; CUDA11, CUDA12 or CUDA13 for win-gpu.
Defaults to CPU or CUDA12.

.PARAMETER CheckOnly
Print the detected Python, spec and output path, then stop before the
long PyInstaller build. The permission preflight still runs.

.PARAMETER NoClean
Do not pass --clean to PyInstaller (reuse its cache for a faster build).

.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_executable.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_executable.ps1 -Target win-gpu -Variant CUDA12
#>
[CmdletBinding()]
param(
    [ValidateSet("win-cpu", "win-gpu")]
    [string]$Target = "win-cpu",

    [string]$Variant = "",

    [switch]$CheckOnly,

    [switch]$NoClean
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$rootDir = Split-Path -Parent $scriptDir
$specDir = Join-Path $rootDir "packaging\pyinstaller\specs"
$distDir = Join-Path $rootDir "dist"
$prepareScript = Join-Path $scriptDir "prepare_windows_build.ps1"
$clearScript = Join-Path $scriptDir "clear_windows_sandbox_acls.ps1"

function Fail([string]$Message) {
    throw $Message
}

function Find-BuildPython {
    $candidates = @()
    if ($env:PYTHON) { $candidates += $env:PYTHON }
    if ($env:CONDA_PREFIX) {
        $candidates += (Join-Path $env:CONDA_PREFIX "python.exe")
    }
    $candidates += (Join-Path $env:USERPROFILE "miniconda3\envs\xal-cpu\python.exe")
    $candidates += "python"

    foreach ($candidate in $candidates) {
        if (-not $candidate) { continue }
        if (Test-Path -LiteralPath $candidate) {
            $exe = $candidate
        } else {
            $command = Get-Command $candidate -ErrorAction SilentlyContinue
            if (-not $command) { continue }
            $exe = $command.Source
        }
        & $exe -c "import PyInstaller, PyQt6, cv2, numpy, onnxruntime, matplotlib, PIL" 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            return $exe
        }
    }
    return $null
}

if ($env:OS -ne "Windows_NT") {
    Fail "This script builds Windows artifacts only. Use scripts/build_executable.sh on Linux/macOS."
}

if (-not (Test-Path -LiteralPath $prepareScript)) {
    Fail "Missing helper: $prepareScript"
}
if (-not (Test-Path -LiteralPath $clearScript)) {
    Fail "Missing helper: $clearScript"
}

$appInfoPath = Join-Path $rootDir "anylabeling\app_info.py"
$appInfo = Get-Content -LiteralPath $appInfoPath -Raw
$versionMatch = [regex]::Match($appInfo, '(?m)^__version__\s*=\s*"([^"]+)"')
if (-not $versionMatch.Success) {
    Fail "Unable to read __version__ from $appInfoPath"
}
$version = $versionMatch.Groups[1].Value

if ($Target -eq "win-cpu") {
    if ($Variant -and $Variant -ne "CPU") {
        Fail "win-cpu only supports -Variant CPU"
    }
    $resolvedVariant = "CPU"
    $device = "CPU"
    $suffix = "CPU"
    $specName = "x-anylabeling-win-cpu.spec"
} else {
    if (-not $Variant) { $Variant = "CUDA12" }
    if ($Variant -notin @("CUDA11", "CUDA12", "CUDA13")) {
        Fail "win-gpu requires -Variant CUDA11, CUDA12 or CUDA13"
    }
    $resolvedVariant = $Variant
    $device = "GPU"
    $suffix = "GPU"
    $specName = "x-anylabeling-win-gpu.spec"
}

$specPath = Join-Path $specDir $specName
if (-not (Test-Path -LiteralPath $specPath)) {
    Fail "Missing PyInstaller spec: $specPath"
}

$python = Find-BuildPython
if (-not $python) {
    Fail (
        "No Python with PyInstaller and PyQt6 was found. Activate the " +
        "xal-cpu conda environment, or install them with: " +
        "python -m pip install pyinstaller PyQt6"
    )
}

$artifact = Join-Path $distDir ("X-AnyLabeling-v" + $version + "-" + $suffix + ".exe")
$workDir = Join-Path $rootDir ("build\pyinstaller-windows-" + $suffix)

Write-Output ("Python:   " + $python)
Write-Output ("Target:   " + $Target)
Write-Output ("Spec:     " + $specPath)
Write-Output ("Artifact: " + $artifact)

Write-Output "Preparing Windows build permissions..."
& $prepareScript -DistDir $distDir

$env:X_ANYLABELING_ROOT = $rootDir
$env:X_ANYLABELING_DEVICE = $device
$env:X_ANYLABELING_VARIANT = $resolvedVariant
Remove-Item Env:X_ANYLABELING_PACKAGE_MODE -ErrorAction SilentlyContinue

$pyInstallerArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--log-level", "INFO",
    "--distpath", $distDir,
    "--workpath", $workDir,
    $specPath
)
if (-not $NoClean) {
    $pyInstallerArgs = @("-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "INFO", "--distpath", $distDir, "--workpath", $workDir, $specPath)
}

if ($CheckOnly) {
    Write-Output "CheckOnly: permission preflight passed; build was not started."
    return
}

Write-Output "Running PyInstaller..."
& $python @pyInstallerArgs
if ($LASTEXITCODE -ne 0) {
    Fail "PyInstaller failed with exit code $LASTEXITCODE"
}
if (-not (Test-Path -LiteralPath $artifact)) {
    Fail "PyInstaller finished but the expected artifact is missing: $artifact"
}

Write-Output "Clearing Windows sandbox permission markers..."
& $clearScript -Path $artifact -AllowRoot $distDir

$hash = (Get-FileHash -LiteralPath $artifact -Algorithm SHA256).Hash.ToLowerInvariant()
$shaPath = $artifact + ".sha256"
($hash + "  " + (Split-Path $artifact -Leaf)) | Set-Content -LiteralPath $shaPath -Encoding ASCII

Write-Output "Build complete."
Write-Output ("Artifact: " + $artifact)
Write-Output ("SHA256:   " + $hash)
Write-Output ("Checksum: " + $shaPath)
