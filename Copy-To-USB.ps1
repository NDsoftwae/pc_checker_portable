<#
  Copies the PC Hardware Test files onto a USB stick that already has SystemRescue written to it
  (written with Rufus in "ISO Image mode"). It only ADDS files - nothing on the stick is erased.

  Usage:  powershell -ExecutionPolicy Bypass -File .\Copy-To-USB.ps1 -Drive E
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$Drive
)

$ErrorActionPreference = 'Stop'
$letter = $Drive.TrimEnd(':', '\')
$root = "${letter}:\"
$src = Join-Path $PSScriptRoot 'usb'

if (-not (Test-Path $root)) { throw "Drive $root not found." }
if (-not (Test-Path (Join-Path $root 'sysresccd'))) {
    throw "Drive $root does not look like a SystemRescue USB stick (no 'sysresccd' folder). Write the SystemRescue ISO to it with Rufus first (ISO Image mode)."
}

$vol = Get-Volume -DriveLetter $letter
Write-Host "Target: $root  label=$($vol.FileSystemLabel)  fs=$($vol.FileSystem)  free=$([math]::Round($vol.SizeRemaining/1GB,1)) GB"
if ($vol.FileSystem -notin @('FAT32', 'FAT', 'exFAT')) {
    Write-Warning "File system is $($vol.FileSystem). Reports can only be saved if the stick is writable from Linux (FAT32 recommended)."
}

foreach ($dir in 'autorun', 'pctest', 'sysrescue.d') {
    $dest = Join-Path $root $dir
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Copy-Item -Path (Join-Path $src "$dir\*") -Destination $dest -Recurse -Force
    Write-Host "  copied $dir\"
}
New-Item -ItemType Directory -Force -Path (Join-Path $root 'PC-Reports') | Out-Null

Write-Host ""
Write-Host "Done. Boot a computer from this USB stick - the test starts automatically." -ForegroundColor Green
Write-Host "Reports will appear in ${root}PC-Reports\"
