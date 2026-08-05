<#
.SYNOPSIS
    Links this repo's Copilot configuration into %USERPROFILE%\.copilot.

.DESCRIPTION
    Directories are linked with NTFS junctions (no admin rights required, unlike
    symlinks). Loose files are copied, because git checkout replaces files in
    place and would break a hard link.

    Run this once on each machine after cloning the repo.

.PARAMETER Push
    Copy the loose files from this repo into ~/.copilot (default direction).

.PARAMETER Pull
    Copy the loose files from ~/.copilot back into this repo, so local edits can
    be committed.

.EXAMPLE
    .\link.ps1
    .\link.ps1 -Pull
#>
[CmdletBinding(DefaultParameterSetName = 'Push')]
param(
    [Parameter(ParameterSetName = 'Pull')]
    [switch]$Pull
)

$ErrorActionPreference = 'Stop'

$repo = $PSScriptRoot
$home_ = Join-Path $env:USERPROFILE '.copilot'

# Directories junctioned from repo -> ~/.copilot
$linkedDirs = @(
    'instructions'
    'agents'
    'skills\github-pr-diff-review'
    'skills\pr-description-style'
    'skills\pr-file-copy-diff-annotation'
)

# Files copied in both directions (junctions can't target a single file)
$copiedFiles = @(
    'copilot-instructions.md'
    'settings.json'
)

function Get-Backup([string]$path) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    return "$path.bak-$stamp"
}

if ($Pull) {
    foreach ($file in $copiedFiles) {
        $src = Join-Path $home_ $file
        if (Test-Path $src) {
            Copy-Item $src (Join-Path $repo $file) -Force
            Write-Host "pulled  $file"
        }
    }
    Write-Host "`nDone. Review with 'git diff' and commit."
    return
}

if (-not (Test-Path $home_)) {
    throw "Copilot config directory not found: $home_"
}

foreach ($dir in $linkedDirs) {
    $target = Join-Path $repo $dir
    $link = Join-Path $home_ $dir

    if (-not (Test-Path $target)) {
        Write-Warning "skipped $dir (not present in repo)"
        continue
    }

    $existing = Get-Item $link -Force -ErrorAction SilentlyContinue
    if ($existing) {
        if ($existing.LinkType -eq 'Junction') {
            if ($existing.Target -contains $target) {
                Write-Host "ok      $dir (already linked)"
                continue
            }
            Remove-Item $link -Force
        }
        else {
            $backup = Get-Backup $link
            Move-Item $link $backup
            Write-Host "backup  $dir -> $(Split-Path $backup -Leaf)"
        }
    }

    New-Item -ItemType Directory -Force -Path (Split-Path $link -Parent) | Out-Null
    cmd /c mklink /J "$link" "$target" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to create junction for $dir" }
    Write-Host "linked  $dir"
}

foreach ($file in $copiedFiles) {
    $src = Join-Path $repo $file
    $dst = Join-Path $home_ $file

    if (-not (Test-Path $src)) {
        Write-Warning "skipped $file (not present in repo)"
        continue
    }

    if ((Test-Path $dst) -and -not (Get-FileHash $src).Hash.Equals((Get-FileHash $dst).Hash)) {
        $backup = Get-Backup $dst
        Copy-Item $dst $backup
        Write-Host "backup  $file -> $(Split-Path $backup -Leaf)"
    }

    Copy-Item $src $dst -Force
    Write-Host "copied  $file"
}

Write-Host "`nDone. Restart Copilot to pick up the changes."
Write-Host "Plugins listed in settings.json reinstall themselves from their marketplace."
