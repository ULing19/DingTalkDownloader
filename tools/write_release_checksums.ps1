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

function Get-Sha256 {
    param([string]$Path)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $stream = [IO.File]::OpenRead($Path)
        try {
            return ([BitConverter]::ToString($sha256.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
        }
        finally {
            $stream.Dispose()
        }
    }
    finally {
        $sha256.Dispose()
    }
}

$lines = foreach ($name in $names) {
    $path = Join-Path $release $name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Release asset is missing: $path"
    }
    $hash = Get-Sha256 -Path $path
    "$hash  $name"
}

$target = Join-Path $release 'SHA256SUMS.txt'
[IO.File]::WriteAllLines($target, $lines, (New-Object Text.UTF8Encoding($false)))
Write-Host "Release checksums created: $target"
