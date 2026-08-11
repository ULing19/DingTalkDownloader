[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$DestinationDir,
    [string]$CacheDir = (Join-Path $PSScriptRoot '..\build\vendor'),
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

# These values are copied from the v0.3.0 release checksums.txt and are kept
# here so a build cannot silently substitute a different third-party binary.
$Version = '0.3.0'
$AssetName = 'mediago_0.3.0_windows_amd64.zip'
$AssetUrl = "https://github.com/Sophomoresty/mediago/releases/download/v$Version/$AssetName"
$ChecksumsUrl = "https://github.com/Sophomoresty/mediago/releases/download/v$Version/checksums.txt"
$ExpectedChecksumsSha256 = '2724e8ba55f33fe1d1d6b84a4bc061d934b9c13cdd974e434e9b408f3edeabdf'
$ExpectedAssetSha256 = '2d88d1741815382d6fc79cf1aeaadd261ae4ba9f3d44a56f9efa1f8d3379e98c'

function Resolve-FullPath {
    param([string]$Path)
    return [IO.Path]::GetFullPath($Path)
}

function Get-Sha256 {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Download-File {
    param(
        [string]$Url,
        [string]$Path
    )

    $partial = "$Path.part"
    $bitsCommand = Get-Command Start-BitsTransfer -ErrorAction SilentlyContinue
    if ($bitsCommand) {
        try {
            Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
            Write-Host "Downloading $Url with BITS"
            Start-BitsTransfer -Source $Url -Destination $partial -DisplayName "DingTalkDownloader MediaGo v$Version" -ErrorAction Stop
            if ((Get-Item -LiteralPath $partial).Length -lt 1) {
                throw "Downloaded file is empty: $Url"
            }
            Move-Item -LiteralPath $partial -Destination $Path -Force
            return
        }
        catch {
            Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
            Write-Warning "BITS download failed; falling back to HTTPS: $($_.Exception.Message)"
        }
    }

    for ($attempt = 1; $attempt -le 4; $attempt++) {
        try {
            Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
            Write-Host "Downloading $Url (attempt $attempt/4)"
            Invoke-WebRequest -Uri $Url -OutFile $partial -UseBasicParsing -TimeoutSec 180 -Headers @{
                'User-Agent' = 'DingTalkDownloader-build/1.1.0'
            }
            if (-not (Test-Path -LiteralPath $partial)) {
                throw "Download did not create a file: $Url"
            }
            if ((Get-Item -LiteralPath $partial).Length -lt 1) {
                throw "Downloaded file is empty: $Url"
            }
            Move-Item -LiteralPath $partial -Destination $Path -Force
            return
        }
        catch {
            Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
            if ($attempt -eq 4) {
                throw "Unable to download $Url after 4 attempts. $($_.Exception.Message)"
            }
            Start-Sleep -Seconds ($attempt * 2)
        }
    }
}

$DestinationDir = Resolve-FullPath $DestinationDir
$CacheDir = Resolve-FullPath $CacheDir
New-Item -ItemType Directory -Force -Path $DestinationDir, $CacheDir | Out-Null

$checksumsPath = Join-Path $CacheDir "mediago_v$Version-checksums.txt"
$archivePath = Join-Path $CacheDir $AssetName

if ((-not (Test-Path -LiteralPath $checksumsPath)) -or (Get-Sha256 $checksumsPath) -ne $ExpectedChecksumsSha256) {
    Download-File -Url $ChecksumsUrl -Path $checksumsPath
}

$checksumsHash = Get-Sha256 $checksumsPath
if ($checksumsHash -ne $ExpectedChecksumsSha256) {
    throw "Official checksums.txt hash mismatch. Expected $ExpectedChecksumsSha256, got $checksumsHash"
}

$linePattern = '^\s*([0-9a-fA-F]{64})\s+\*?' + [regex]::Escape($AssetName) + '\s*$'
$assetMatch = [regex]::Match((Get-Content -LiteralPath $checksumsPath -Raw -Encoding ASCII), $linePattern, [Text.RegularExpressions.RegexOptions]::Multiline)
if (-not $assetMatch.Success) {
    throw "The official checksums.txt does not contain $AssetName"
}
$listedAssetHash = $assetMatch.Groups[1].Value.ToLowerInvariant()
if ($listedAssetHash -ne $ExpectedAssetSha256) {
    throw "Pinned asset hash disagrees with official checksums.txt. Expected $ExpectedAssetSha256, got $listedAssetHash"
}

if ($Force -or -not (Test-Path -LiteralPath $archivePath) -or (Get-Sha256 $archivePath) -ne $ExpectedAssetSha256) {
    Download-File -Url $AssetUrl -Path $archivePath
}
$archiveHash = Get-Sha256 $archivePath
if ($archiveHash -ne $ExpectedAssetSha256) {
    Remove-Item -LiteralPath $archivePath -Force -ErrorAction SilentlyContinue
    throw "MediaGo archive SHA-256 mismatch. Expected $ExpectedAssetSha256, got $archiveHash"
}

$extractDir = Join-Path $CacheDir "mediago_v$Version-extracted"
if (Test-Path -LiteralPath $extractDir) {
    Remove-Item -LiteralPath $extractDir -Recurse -Force
}
Expand-Archive -LiteralPath $archivePath -DestinationPath $extractDir -Force
$mediaExe = Get-ChildItem -LiteralPath $extractDir -Recurse -File -Filter 'mediago.exe' |
    Select-Object -First 1
if (-not $mediaExe) {
    throw "The verified MediaGo archive does not contain mediago.exe"
}

$target = Join-Path $DestinationDir 'mediago.exe'
Copy-Item -LiteralPath $mediaExe.FullName -Destination $target -Force
Copy-Item -LiteralPath $checksumsPath -Destination (Join-Path $DestinationDir 'mediago_checksums.txt') -Force
$mediaLicense = Get-ChildItem -LiteralPath $extractDir -Recurse -File -Filter 'LICENSE' |
    Select-Object -First 1
if ($mediaLicense) {
    Copy-Item -LiteralPath $mediaLicense.FullName -Destination (Join-Path $DestinationDir 'MEDIAGO_LICENSE.txt') -Force
}
Write-Host "MediaGo v$Version installed: $target"
Write-Host "Archive SHA-256: $archiveHash"
