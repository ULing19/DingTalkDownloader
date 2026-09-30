[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RootDir,
    [Parameter(Mandatory = $true)][string]$Version,
    [Parameter(Mandatory = $true)][string]$GuiExe
)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($RootDir)
$source = Join-Path $root "dist\DingTalkDownloader_$Version"
$destination = Join-Path $root "dist\DingTalkDownloader_${Version}_x86"
if (Test-Path -LiteralPath $destination) { throw "Destination already exists: $destination" }
$engines = Join-Path $root 'build\x86-experiment\engines'
foreach ($file in @($GuiExe, (Join-Path $engines 'GoDingtalk_x86.exe'), (Join-Path $engines 'mediago_x86.exe'), (Join-Path $engines 'ffmpeg_x86.exe'))) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Required file missing: $file" }
}
New-Item -ItemType Directory -Path $destination | Out-Null
foreach ($file in Get-ChildItem -LiteralPath $source -Force) {
    if ($file.Name -like 'DingTalkDownloader_*_Portable.zip' -or
        $file.Name -like 'DingTalkDownloader_*_Setup.exe' -or
        $file.Name -in @('SHA256SUMS.txt', 'GoDingtalk_v2.5.2_windows_amd64.exe')) { continue }
    Copy-Item -LiteralPath $file.FullName -Destination $destination -Recurse
}
Copy-Item -LiteralPath $GuiExe -Destination (Join-Path $destination 'DingTalkDownloader.exe') -Force
Copy-Item -LiteralPath (Join-Path $engines 'GoDingtalk_x86.exe') -Destination (Join-Path $destination 'GoDingtalk_x86.exe') -Force
Copy-Item -LiteralPath (Join-Path $engines 'mediago_x86.exe') -Destination (Join-Path $destination 'mediago.exe') -Force
Copy-Item -LiteralPath (Join-Path $engines 'ffmpeg_x86.exe') -Destination (Join-Path $destination 'ffmpeg.exe') -Force
& (Join-Path $root 'tools\make_release_zip.ps1') -SourceDir $destination -Destination (Join-Path $destination "DingTalkDownloader_${Version}_x86_Portable.zip")
$compiler = Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'
& $compiler "/DMyAppVersion=$Version" '/DX86Build' "/DReleaseDir=$destination" (Join-Path $root 'installer\setup.iss')
if ($LASTEXITCODE -ne 0) { throw '32-bit installer build failed' }
& (Join-Path $root 'tools\write_release_checksums.ps1') -ReleaseDir $destination -Version "${Version}_x86"
