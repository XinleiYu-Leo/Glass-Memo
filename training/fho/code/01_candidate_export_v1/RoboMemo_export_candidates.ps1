param(
    [Parameter(Mandatory=$true)][string]$Project,
    [Parameter(Mandatory=$true)][string]$Annotations
)
$ErrorActionPreference = 'Stop'
function Convert-ToWslDrivePath([string]$WindowsPath) {
    $absolute = [System.IO.Path]::GetFullPath($WindowsPath)
    if ($absolute -notmatch '^([A-Za-z]):[\\/]') { throw 'Extract the package on a local drive.' }
    return '/mnt/' + $Matches[1].ToLowerInvariant() + '/' + $absolute.Substring(3).Replace('\', '/')
}
$scriptFile = Join-Path $PSScriptRoot 'run_export.py'
if (-not (Test-Path -LiteralPath $scriptFile -PathType Leaf)) { throw 'Extract the whole ZIP before running.' }
$runner = if ($env:ROBOMEMO_WSL_PYTHON) { $env:ROBOMEMO_WSL_PYTHON } else { 'python3' }  # set ROBOMEMO_WSL_PYTHON to your WSL venv python
Write-Host 'Exporting eligible annotation rows (read-only). No download, upload or training.'
& wsl.exe --exec $runner -B -u (Convert-ToWslDrivePath $scriptFile) --project (Convert-ToWslDrivePath $Project) --annotations (Convert-ToWslDrivePath $Annotations)
$code = $LASTEXITCODE
$resultRoot = Join-Path $Project 'candidate_exports'
if (Test-Path -LiteralPath $resultRoot -PathType Container) { Start-Process -FilePath explorer.exe -ArgumentList ('"{0}"' -f $resultRoot) }
if ($code -ne 0) { throw "Export needs attention (exit $code). Send the RETURN_ZIP if printed, otherwise the last error." }
Write-Host 'Send RoboMemo_candidates_return.zip from the newest export_ folder.'
