# AI Forge installer (Windows). Run from the shared folder:
#   powershell -ExecutionPolicy Bypass -File "<AI-Forge-Shared>\tool\install.ps1"
$ErrorActionPreference = "Stop"
$ForgeHome = Join-Path $env:USERPROFILE ".ai-forge"
$Venv = Join-Path $ForgeHome "venv"

function Need($cmd, $hint) {
  if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { Write-Host "Missing: $cmd  ->  $hint" -ForegroundColor Red; $script:missing = $true }
}
$missing = $false
Need "python" "Install Python 3.11+ (Company Portal or python.org), tick 'Add to PATH'"
Need "git" "Install Git for Windows"
Need "copilot" "Install Node 22+, then: npm install -g @github/copilot ; run 'copilot' once and log in"
if ($missing) { exit 1 }

$pyver = python -c "import sys; print(sys.version_info >= (3,11))"
if ($pyver -ne "True") { Write-Host "Python 3.11+ required" -ForegroundColor Red; exit 1 }

New-Item -ItemType Directory -Force -Path $ForgeHome | Out-Null
if (-not (Test-Path $Venv)) { python -m venv $Venv }
& "$Venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
# Build from a local copy: an in-tree pip build would write build/ and *.egg-info into the synced shared folder.
$Src = Join-Path $ForgeHome "tool-src"
if (Test-Path $Src) { Remove-Item -Recurse -Force $Src }
Copy-Item -Recurse "$PSScriptRoot" $Src
Get-ChildItem $Src -Recurse -Directory -Include build,*.egg-info,__pycache__ | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
& "$Venv\Scripts\python.exe" -m pip install --quiet "$Src"
Copy-Item "$PSScriptRoot\VERSION" "$ForgeHome\installed_version" -Force

& "$Venv\Scripts\forge.exe" setup
Write-Host "`nDone. Useful commands:  forge doctor --live   |   forge run --force   |   forge stats" -ForegroundColor Green
Write-Host "Tip: add $Venv\Scripts to your PATH to call 'forge' directly."
