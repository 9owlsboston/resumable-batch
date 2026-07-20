<#
.SYNOPSIS  Remove a worktree created by new-workstream.ps1.
.EXAMPLE   scripts\end-workstream.ps1 login-redirect
#>
param([Parameter(Mandatory = $true)][string]$Topic)
$ErrorActionPreference = "Stop"
$root = (git rev-parse --show-toplevel).Trim()
$name = Split-Path $root -Leaf
$wt   = Join-Path (Split-Path $root -Parent) "$name.worktrees\$Topic"
git -C $root worktree remove $wt --force
git -C $root worktree prune
Write-Host "Removed worktree $wt." -ForegroundColor Green
Write-Host "After the PR is merged, delete the branch:  git branch -d <branch>"
