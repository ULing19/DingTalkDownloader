[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RootDir,
    [string]$Version = '1.2.3'
)

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($RootDir).TrimEnd([IO.Path]::DirectorySeparatorChar)
$release = Join-Path $root "dist\DingTalkDownloader_$Version"
$portable = Join-Path $release "DingTalkDownloader_${Version}_Portable.zip"
$setup = Join-Path $release "DingTalkDownloader_${Version}_Setup.exe"

$required = @(
    'DingTalkDownloader.exe',
    'GoDingtalk_v2.5.2_windows_amd64.exe',
    'ffmpeg.exe',
    'mediago.exe'
)
$needsBuild = -not (Test-Path -LiteralPath $portable -PathType Leaf)
foreach ($name in $required) {
    if (-not (Test-Path -LiteralPath (Join-Path $release $name) -PathType Leaf)) {
        $needsBuild = $true
    }
}
if ($needsBuild) {
    Write-Host 'Release payload is incomplete; running build_exe.bat...'
    & (Join-Path $root 'build_exe.bat')
    if ($LASTEXITCODE -ne 0) {
        throw "build_exe.bat failed with exit code $LASTEXITCODE"
    }
}

& (Join-Path $root 'tools\copy_release_docs.ps1') -SourceDir $root -DestinationDir $release
foreach ($path in @($portable, $setup, (Join-Path $release 'SHA256SUMS.txt'))) {
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Force
    }
}
& (Join-Path $root 'tools\make_release_zip.ps1') -SourceDir $release -Destination $portable
if (-not (Test-Path -LiteralPath $portable -PathType Leaf)) {
    throw "Portable archive was not created: $portable"
}

$isccCandidates = @()
if ($env:INNOSETUP_PATH) {
    $isccCandidates += $env:INNOSETUP_PATH
}
$isccCandidates += @(
    (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
    (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
    (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 7\ISCC.exe'),
    (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 7\ISCC.exe'),
    (Join-Path $env:ProgramFiles 'Inno Setup 7\ISCC.exe')
)
$iscc = $isccCandidates |
    Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } |
    Select-Object -First 1
if (-not $iscc) {
    $command = Get-Command iscc -ErrorAction SilentlyContinue
    if ($command) {
        $iscc = $command.Source
    }
}
if (-not $iscc) {
    throw 'Inno Setup 6/7 was not found. Install it from https://jrsoftware.org/isdl.php'
}

$installerScript = Join-Path $root 'installer\setup.iss'
Write-Host "Compiling installer with: $iscc"
& $iscc $installerScript
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup failed with exit code $LASTEXITCODE"
}
if (-not (Test-Path -LiteralPath $setup -PathType Leaf)) {
    throw "Installer was not created: $setup"
}

& (Join-Path $root 'tools\write_release_checksums.ps1') -ReleaseDir $release -Version $Version
Write-Host "Installer created: $setup"
Write-Host "Portable archive: $portable"
Write-Host "Release folder: $release"
