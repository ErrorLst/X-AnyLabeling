#Requires -Version 5.1
<#
.SYNOPSIS
Remove DSH sandbox permission markers from one build artifact file.

.DESCRIPTION
Builds produced inside a DSH sandbox inherit extra Windows access
entries (sandbox SIDs, CodexSandboxUsers and sometimes Deny entries).
Such an artifact can fail to start when it is later run normally.
This script resets the access permissions of the given file to a small
standard set:

  current user   : FullControl
  SYSTEM         : FullControl
  Administrators : FullControl
  BUILTIN\Users  : ReadAndExecute

Existing inherited and explicit entries on the artifact are removed;
the parent directory and its deny entries are not touched. The
mandatory integrity label is reset from Low (the DSH sandbox marker
that makes a onefile executable fail to unpack) to Medium. Run it from
a normal (not sandboxed) PowerShell; changing permissions and labels
needs access the sandbox token does not have. A backup of the original
permission set and of the icacls listing is written under
%TEMP%\xal-acl-backup-*.

.PARAMETER Path
One or more files. Directories are refused on purpose: the build
scripts only pass the final exe or Setup file.

.PARAMETER AllowRoot
Optional safety boundary. When set, every target must be that directory
or live below it; anything else is refused. The build scripts pass the
dist directory so a typo cannot touch another folder.

.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File clear_windows_sandbox_acls.ps1 -Path C:\build\dist\app.exe -AllowRoot C:\build\dist
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string[]]$Path,

    [string]$AllowRoot = ""
)

$ErrorActionPreference = "Stop"

$userSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$userGrant = "*" + $userSid + ":(F)"
$systemGrant = "*S-1-5-18:(F)"
$adminGrant = "*S-1-5-32-544:(F)"
$usersGrant = "*S-1-5-32-545:(RX)"

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupRoot = Join-Path $env:TEMP ("xal-acl-backup-" + $stamp)
New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null

$rootFull = ""
if ($AllowRoot) {
    $rootFull = [System.IO.Path]::GetFullPath($AllowRoot).TrimEnd("\")
}

function Convert-ToAceSid {
    param($Ace)
    try {
        return $Ace.IdentityReference.Translate(
            [System.Security.Principal.SecurityIdentifier]
        ).Value
    } catch {
        return [string]$Ace.IdentityReference
    }
}

function Assert-TargetPath {
    param([string]$Candidate)
    $full = [System.IO.Path]::GetFullPath($Candidate)
    if (-not (Test-Path -LiteralPath $full)) {
        throw "Target does not exist: $full"
    }
    if (Test-Path -LiteralPath $full -PathType Container) {
        throw "Refusing directory target (files only): $full"
    }
    if ($rootFull) {
        $prefix = $rootFull + "\"
        $inside = $full.Equals(
            $rootFull,
            [System.StringComparison]::OrdinalIgnoreCase
        ) -or $full.StartsWith(
            $prefix,
            [System.StringComparison]::OrdinalIgnoreCase
        )
        if (-not $inside) {
            throw "Refusing target outside AllowRoot: $full"
        }
    }
    return $full
}

function Get-BadAceProblems {
    param([string]$FullPath)
    $problems = @()
    $acl = Get-Acl -LiteralPath $FullPath
    $hasUserFull = $false
    foreach ($ace in $acl.Access) {
        $aceSid = Convert-ToAceSid $ace
        if ($ace.AccessControlType -eq "Deny") {
            $problems += ("deny entry for " + $aceSid)
        }
        if ($aceSid -match "^S-1-4-") {
            $problems += ("sandbox SID " + $aceSid)
        }
        if ($aceSid -match "^S-1-15-2-") {
            $problems += ("package SID " + $aceSid)
        }
        if ($aceSid -match "^S-1-15-3-") {
            $problems += ("appcontainer SID " + $aceSid)
        }
        if ([string]$ace.IdentityReference -like "*CodexSandboxUsers*") {
            $problems += "CodexSandboxUsers entry"
        }
        if (
            $aceSid -eq $userSid -and
            $ace.AccessControlType -eq "Allow" -and
            (($ace.FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::FullControl) -ne 0)
        ) {
            $hasUserFull = $true
        }
    }
    if (-not $hasUserFull) {
        $problems += "current user has no FullControl entry"
    }
    $labelText = [string]::Join(
        [Environment]::NewLine,
        @(& icacls.exe $FullPath)
    )
    if ($labelText -match "(?i)low mandatory level|低强制级别|低完整性") {
        $problems += "low integrity label"
    }
    if (-not ($labelText -match "(?i)medium mandatory level|中等强制级别|中等完整性")) {
        $problems += "medium integrity label missing"
    }
    return $problems
}

$cleared = 0
foreach ($item in $Path) {
    $full = Assert-TargetPath $item
    $safeName = ($full -replace "[^A-Za-z0-9._-]", "_")
    $backupFile = Join-Path $backupRoot ($safeName + ".acl.xml")
    $backupText = Join-Path $backupRoot ($safeName + ".icacls.txt")
    Get-Acl -LiteralPath $full | Export-Clixml -LiteralPath $backupFile
    & icacls.exe $full > $backupText

    & icacls.exe $full /inheritance:r /grant:r $userGrant $systemGrant $adminGrant $usersGrant | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "icacls failed with exit code $LASTEXITCODE for $full"
    }

    & icacls.exe $full /setintegritylevel M | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "icacls setintegritylevel failed with exit code $LASTEXITCODE for $full"
    }

    $problems = @(Get-BadAceProblems $full)
    if ($problems.Count -gt 0) {
        throw (
            "Permission cleanup failed for " + $full + ": " +
            ($problems -join "; ") + ". Backup: " + $backupFile
        )
    }
    $cleared += 1
    Write-Output ("ACL_CLEARED " + $full)
}

Write-Output ("ACL_BACKUP " + $backupRoot)
Write-Output ("ACL_CLEARED_TARGETS " + $cleared)
