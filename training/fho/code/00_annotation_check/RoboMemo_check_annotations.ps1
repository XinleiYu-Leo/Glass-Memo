param(
    [Parameter(Mandatory=$true)][string]$Project,
    [Parameter(Mandatory=$true)][string]$Annotations
)
$ErrorActionPreference = 'Stop'
function Convert-ToWslDrivePath([string]$WindowsPath) {
    $absolute = [System.IO.Path]::GetFullPath($WindowsPath)
    if ($absolute -notmatch '^([A-Za-z]):[\\/]') {
        throw 'Extract the package on a local C: or D: drive.'
    }
    $drive = $Matches[1].ToLowerInvariant()
    return '/mnt/' + $drive + '/' + $absolute.Substring(3).Replace('\', '/')
}
if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    throw 'Run this package on the Windows training computer with WSL.'
}
$scriptFile = Join-Path $PSScriptRoot 'run_checks.py'
if (-not (Test-Path -LiteralPath $scriptFile -PathType Leaf)) {
    throw 'Extract the whole ZIP before running.'
}
$runner = if ($env:ROBOMEMO_WSL_PYTHON) { $env:ROBOMEMO_WSL_PYTHON } else { 'python3' }  # set ROBOMEMO_WSL_PYTHON to your WSL venv python
$runArgs = @('--exec', $runner, '-B', '-u', (Convert-ToWslDrivePath $scriptFile),
    '--project', (Convert-ToWslDrivePath $Project),
    '--annotations', (Convert-ToWslDrivePath $Annotations))
Write-Host 'Reading existing annotations and checking the installed GPU operators.'
Write-Host 'Large annotations are scanned in small chunks. Progress will appear below.'
Write-Host 'No videos are downloaded, uploaded or changed. No model training runs.'
& wsl.exe @runArgs
$resultCode = $LASTEXITCODE
$resultRoot = Join-Path $Project 'annotation_checks'
if (Test-Path -LiteralPath $resultRoot -PathType Container) {
    Start-Process -FilePath explorer.exe -ArgumentList ('"{0}"' -f $resultRoot)
}
if ($resultCode -ne 0) {
    throw "One check needs attention (exit $resultCode). If RETURN_ZIP was printed, send that ZIP; otherwise send the last error."
}
Write-Host 'Send RoboMemo_annotation_return.zip from the newest check folder to this chat.'
