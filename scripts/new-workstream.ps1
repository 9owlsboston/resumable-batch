<#
.SYNOPSIS  Create an isolated git worktree for one workstream (parallel-agent workflow).
.DESCRIPTION
  One terminal <-> one worktree <-> one branch <-> one PR. Bases off
  origin/<default> (fetched first) so you never branch off a stale local anchor.
.EXAMPLE  scripts\new-workstream.ps1 fix/login-redirect
.EXAMPLE  scripts\new-workstream.ps1 feat/x origin/main
#>
param(
  [Parameter(Mandatory = $true)][string]$Branch,
  [string]$Base = ""
)
$ErrorActionPreference = "Stop"

$root  = (git rev-parse --show-toplevel).Trim()
$name  = Split-Path $root -Leaf
$topic = ($Branch -split '/')[-1]
$wt    = Join-Path (Split-Path $root -Parent) "$name.worktrees\$topic"

git -C $root fetch origin --quiet
if (-not $Base) {
  $def = (git -C $root symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>$null)
  if ($def) { $def = $def -replace '^origin/', '' } else { $def = 'main' }
  $Base = "origin/$def"
}
if (Test-Path $wt) { throw "worktree path already exists: $wt" }
New-Item -ItemType Directory -Force -Path (Split-Path $wt -Parent) | Out-Null
git -C $root worktree add $wt -b $Branch $Base

# Share JS deps from the main checkout via junctions, where present.
$pkgDirs = (git -C $root ls-files '*package.json' 'package.json' 2>$null) |
  Where-Object { $_ -notmatch 'node_modules' } | ForEach-Object { Split-Path $_ -Parent } |
  Sort-Object -Unique
foreach ($d in $pkgDirs) {
  $src = Join-Path $root ($d ? "$d\node_modules" : "node_modules")
  $dst = Join-Path $wt   ($d ? "$d\node_modules" : "node_modules")
  if ((Test-Path $src) -and -not (Test-Path $dst)) {
    cmd /c mklink /J "$dst" "$src" | Out-Null
    Write-Host "  junctioned $d/node_modules"
  }
}

$port = 3001 + ([Math]::Abs($topic.GetHashCode()) % 999)
Write-Host ""
Write-Host "Worktree ready: $wt" -ForegroundColor Green
Write-Host "  branch : $Branch  (off $Base)"
Write-Host ""
Write-Host "Next steps (in a DEDICATED terminal):" -ForegroundColor Cyan
Write-Host "  cd `"$wt`""
Write-Host "  `$env:PORT = '$port'"
Write-Host "  # then launch your agent / editor here"
Write-Host ""
Write-Host "When merged:  scripts\end-workstream.ps1 $topic" -ForegroundColor DarkGray
