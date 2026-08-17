[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RootDir,
    [string]$Version = '1.3.4'
)

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($RootDir).TrimEnd([IO.Path]::DirectorySeparatorChar)
$release = Join-Path $root "dist\DingTalkDownloader_$Version"
$stage = Join-Path $root 'build\portable_payload'
$releasePrefix = $root + [IO.Path]::DirectorySeparatorChar
foreach ($target in @($release, $stage)) {
    $full = [IO.Path]::GetFullPath($target)
    if (-not $full.StartsWith($releasePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify a path outside the project: $full"
    }
}

$guiExe = Join-Path $root 'dist\DingTalkDownloader.exe'
$goDingtalk = Join-Path $root 'GoDingtalk_v2.5.2_windows_amd64.exe'
$ffmpeg = Join-Path $root 'ffmpeg.exe'
if (-not (Test-Path -LiteralPath $ffmpeg -PathType Leaf)) {
    $ffmpeg = Join-Path $root 'ffmpeg_tmp\ffmpeg-9.0-essentials_build\bin\ffmpeg.exe'
}
foreach ($required in @($guiExe, $goDingtalk, $ffmpeg)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required release file is missing: $required"
    }
}

foreach ($target in @($release, $stage)) {
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
    New-Item -ItemType Directory -Path $target | Out-Null
}

function Copy-ToPayloads {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Source,
        [Parameter(Mandatory = $true)]
        [string]$Name
    )
    Copy-Item -LiteralPath $Source -Destination (Join-Path $release $Name) -Force
    Copy-Item -LiteralPath $Source -Destination (Join-Path $stage $Name) -Force
}

try {
    Copy-ToPayloads -Source $guiExe -Name 'DingTalkDownloader.exe'
    Copy-ToPayloads -Source $goDingtalk -Name 'GoDingtalk_v2.5.2_windows_amd64.exe'
    Copy-ToPayloads -Source $ffmpeg -Name 'ffmpeg.exe'

    $icon = Join-Path $root 'assets\download.ico'
    if (Test-Path -LiteralPath $icon -PathType Leaf) {
        Copy-ToPayloads -Source $icon -Name 'download.ico'
    }

    $websocketLicense = Join-Path $root 'WEBSOCKET_CLIENT_LICENSE.txt'
    if (Test-Path -LiteralPath $websocketLicense -PathType Leaf) {
        Copy-ToPayloads -Source $websocketLicense -Name 'WEBSOCKET_CLIENT_LICENSE.txt'
    }

    $ffmpegRoot = Join-Path $root 'ffmpeg_tmp\ffmpeg-9.0-essentials_build'
    $ffmpegLicense = Join-Path $ffmpegRoot 'LICENSE'
    $ffmpegBuildInfo = Join-Path $ffmpegRoot 'README.txt'
    if (Test-Path -LiteralPath $ffmpegLicense -PathType Leaf) {
        Copy-ToPayloads -Source $ffmpegLicense -Name 'FFMPEG_LICENSE.txt'
    }
    if (Test-Path -LiteralPath $ffmpegBuildInfo -PathType Leaf) {
        Copy-ToPayloads -Source $ffmpegBuildInfo -Name 'FFMPEG_BUILD_INFO.txt'
    }

    & (Join-Path $root 'tools\fetch_mediago.ps1') `
        -DestinationDir $release `
        -CacheDir (Join-Path $root 'build\vendor')
    foreach ($name in @('mediago.exe', 'mediago_checksums.txt', 'MEDIAGO_LICENSE.txt')) {
        $source = Join-Path $release $name
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            Copy-Item -LiteralPath $source -Destination (Join-Path $stage $name) -Force
        }
    }

    & (Join-Path $root 'tools\copy_release_docs.ps1') -SourceDir $root -DestinationDir $release
    & (Join-Path $root 'tools\copy_release_docs.ps1') -SourceDir $root -DestinationDir $stage

    # Windows PowerShell 5.1 treats BOM-less scripts as ANSI. Keep localized
    # release text in a UTF-8 data file and decode it explicitly.
    $quickStartTemplate = Join-Path $root 'installer\README-quickstart.txt'
    if (-not (Test-Path -LiteralPath $quickStartTemplate -PathType Leaf)) {
        throw "Quick-start template is missing: $quickStartTemplate"
    }
    $utf8NoBom = New-Object Text.UTF8Encoding($false)
    $quickStart = [IO.File]::ReadAllText($quickStartTemplate, $utf8NoBom)
    $quickStart = $quickStart.Replace('@VERSION@', $Version)
    foreach ($target in @($release, $stage)) {
        [IO.File]::WriteAllText(
            (Join-Path $target 'README.txt'),
            $quickStart,
            $utf8NoBom
        )
    }

    New-Item -ItemType Directory -Force -Path `
        (Join-Path $release 'video'), `
        (Join-Path $release '.goDingtalkConfig') | Out-Null

    $portable = Join-Path $release "DingTalkDownloader_${Version}_Portable.zip"
    & (Join-Path $root 'tools\make_release_zip.ps1') -SourceDir $stage -Destination $portable
    if (-not (Test-Path -LiteralPath $portable -PathType Leaf)) {
        throw "Portable archive was not created: $portable"
    }
    Write-Host "Release payload assembled: $release"
    Write-Host "Portable archive created: $portable"
}
finally {
    if (Test-Path -LiteralPath $stage) {
        Remove-Item -LiteralPath $stage -Recurse -Force
    }
}
