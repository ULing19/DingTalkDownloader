param(
    [Parameter(Mandatory = $true)]
    [string]$SourceDir,
    [Parameter(Mandatory = $true)]
    [string]$Destination
)

$ErrorActionPreference = 'Stop'
$source = [IO.Path]::GetFullPath($SourceDir).TrimEnd(
    [IO.Path]::DirectorySeparatorChar,
    [IO.Path]::AltDirectorySeparatorChar
)
$destinationPath = [IO.Path]::GetFullPath($Destination)
if (-not (Test-Path -LiteralPath $source -PathType Container)) {
    Write-Error "Release directory does not exist: $source"
    exit 1
}

$sourcePrefix = $source + [IO.Path]::DirectorySeparatorChar
$artifactPatterns = @(
    'DingTalkDownloader_*_Portable.zip',
    'DingTalkDownloader_*_Setup.exe',
    'SHA256SUMS.txt'
)
$files = @(Get-ChildItem -LiteralPath $source -File -Force -Recurse | Where-Object {
    $fullName = [IO.Path]::GetFullPath($_.FullName)
    if ($fullName.Equals($destinationPath, [StringComparison]::OrdinalIgnoreCase)) {
        return $false
    }
    if (-not $_.DirectoryName.Equals($source, [StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    foreach ($pattern in $artifactPatterns) {
        if ($_.Name -like $pattern) {
            return $false
        }
    }
    return $true
})

if (-not $files) {
    Write-Error "Release directory contains no files: $source"
    exit 1
}

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$destinationDir = Split-Path -Parent $destinationPath
if ($destinationDir) {
    New-Item -ItemType Directory -Force -Path $destinationDir | Out-Null
}

for ($attempt = 1; $attempt -le 5; $attempt++) {
    $archive = $null
    try {
        if (Test-Path -LiteralPath $destinationPath) {
            Remove-Item -LiteralPath $destinationPath -Force
        }
        $archive = [IO.Compression.ZipFile]::Open(
            $destinationPath,
            [IO.Compression.ZipArchiveMode]::Create
        )
        foreach ($file in $files) {
            $fullName = [IO.Path]::GetFullPath($file.FullName)
            if (-not $fullName.StartsWith($sourcePrefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Refusing to archive a path outside the release directory: $fullName"
            }
            $entryName = $fullName.Substring($sourcePrefix.Length).Replace('\', '/')
            if (-not $entryName -or $entryName.StartsWith('/') -or $entryName.Split('/') -contains '..') {
                throw "Unsafe ZIP entry name: $entryName"
            }
            [IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $archive,
                $fullName,
                $entryName,
                [IO.Compression.CompressionLevel]::Optimal
            ) | Out-Null
        }
        $archive.Dispose()
        $archive = $null
        Write-Host "Release ZIP created: $destinationPath"
        exit 0
    }
    catch {
        if ($archive) {
            $archive.Dispose()
            $archive = $null
        }
        if (Test-Path -LiteralPath $destinationPath) {
            Remove-Item -LiteralPath $destinationPath -Force -ErrorAction SilentlyContinue
        }
        if ($attempt -eq 5) {
            Write-Error $_
            exit 1
        }
        Start-Sleep -Seconds 2
    }
}
