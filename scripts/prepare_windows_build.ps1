#Requires -Version 5.1
<#
.SYNOPSIS
Prepare the Windows build output folder before PyInstaller runs.

.DESCRIPTION
DSH marks files created in a sandboxed workspace with a Low mandatory
integrity label and capability SID access entries. Files built under
that marking inherit it; a onefile executable then cannot unpack its
payload into the normal temporary directory and exits before showing a
window.

This preflight checks the current process integrity first. A Low
integrity terminal cannot change labels, so it stops with a clear
message (run from a normal terminal or switch DSH to full access).
Otherwise it sets the dist directory and its existing contents to the
Medium integrity level, so freshly built artifacts inherit a normal
label. The final artifact is cleaned again by
clear_windows_sandbox_acls.ps1 after the build.

.PARAMETER DistDir
The build output directory, normally <repo>\dist.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$DistDir
)

$ErrorActionPreference = "Stop"

$groups = (& whoami.exe /groups 2>&1) -join [Environment]::NewLine
if ($groups -match "(?i)low mandatory level|低强制级别|低完整性|S-1-16-4096") {
    throw (
        "Current terminal is running at Low integrity. Build artifacts " +
        "would inherit the DSH sandbox marker. Run packaging from a normal " +
        "Windows terminal, or switch DSH to full access, then retry."
    )
}

$dist = [System.IO.Path]::GetFullPath($DistDir)
New-Item -ItemType Directory -Force -Path $dist | Out-Null

& icacls.exe $dist /setintegritylevel "(OI)(CI)M" /T /C | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Unable to set the Medium integrity label on $dist"
}

Write-Output ("WINDOWS_BUILD_PREPARED=" + $dist)
