param([Parameter(Mandatory=$true)][string]$Project)
$ErrorActionPreference = 'Stop'
function Convert-ToWslDrivePath([string]$WindowsPath) {
    $absolute = [System.IO.Path]::GetFullPath($WindowsPath)
    if ($absolute -notmatch '^([A-Za-z]):[\\/]') { throw 'Extract the package on a local drive.' }
    return '/mnt/' + $Matches[1].ToLowerInvariant() + '/' + $absolute.Substring(3).Replace('\', '/')
}
$scriptFile = Join-Path $PSScriptRoot 'run_pipeline.py'
if (-not (Test-Path -LiteralPath $scriptFile -PathType Leaf)) { throw 'Extract the whole ZIP before running.' }
$runner = if ($env:ROBOMEMO_WSL_PYTHON) { $env:ROBOMEMO_WSL_PYTHON } else { 'python3' }  # set ROBOMEMO_WSL_PYTHON to your WSL venv python
Write-Host 'RoboMemo fho_transfer_v1: extract frames, compute features on the GPU, train small heads.'
Write-Host 'No download, upload or API call. Ego4D images stay on this computer.'
& wsl.exe --exec $runner -B -u (Convert-ToWslDrivePath $scriptFile) --project (Convert-ToWslDrivePath $Project)
$code = $LASTEXITCODE
$runs = Join-Path $Project 'fho_transfer_v1\runs'
if (Test-Path -LiteralPath $runs -PathType Container) { Start-Process -FilePath explorer.exe -ArgumentList ('"{0}"' -f $runs) }
if ($code -ne 0) { throw "Pipeline needs attention (exit $code). Send the RETURN_ZIP if printed, otherwise the last error." }
Write-Host 'Send RoboMemo_fho_transfer_v1_results.zip from the newest run_ folder.'
