[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ReleaseDir,
    [Parameter(Mandatory = $true)]
    [string]$Version
)

$ErrorActionPreference = 'Stop'
$release = [IO.Path]::GetFullPath($ReleaseDir)
$names = @(
    "DingTalkDownloader_${Version}_Setup.exe",
    "DingTalkDownloader_${Version}_Portable.zip"
)
$lines = foreach ($name in $names) {
    $path = Join-Path $release $name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Release asset is missing: $path"
    }
    $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $name"
}

$target = Join-Path $release 'SHA256SUMS.txt'
[IO.File]::WriteAllLines($target, $lines, (New-Object Text.UTF8Encoding($false)))
Write-Host "Release checksums created: $target"
