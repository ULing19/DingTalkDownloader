param(
    [Parameter(Mandatory = $true)]
    [string]$SourceDir,
    [Parameter(Mandatory = $true)]
    [string]$DestinationDir
)

New-Item -ItemType Directory -Force -Path $DestinationDir | Out-Null
$guides = Get-ChildItem -LiteralPath $SourceDir -File -Filter '*.txt' |
    Where-Object { $_.Name -notlike 'requirements-*' }
if (-not $guides) {
    Write-Error "No text guide found in $SourceDir"
    exit 1
}

foreach ($file in $guides) {
    Copy-Item -LiteralPath $file.FullName -Destination (Join-Path $DestinationDir $file.Name) -Force
}

foreach ($name in @('LICENSE', 'THIRD_PARTY_NOTICES.md')) {
    $source = Join-Path $SourceDir $name
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination (Join-Path $DestinationDir $name) -Force
    }
}
