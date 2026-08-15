param(
    [Parameter(Mandatory = $true)]
    [string]$SourceDir,
    [Parameter(Mandatory = $true)]
    [string]$DestinationDir
)

New-Item -ItemType Directory -Force -Path $DestinationDir | Out-Null
# Keep release payloads limited to reviewed user-facing documentation. This
# prevents local notes or experimental tool files from being redistributed.
# Use an ASCII wildcard here because Windows PowerShell 5.1 can decode a
# BOM-less script's literal Chinese filename with the active code page.
$guides = @(Get-ChildItem -LiteralPath $SourceDir -File -Filter '????.txt' |
    Where-Object { $_.BaseName.Length -eq 4 }
)
# The collector guide has a longer localized filename. Filter by the decoded
# filename length so Windows PowerShell 5.1 does not depend on a BOM-less
# script's active code page when matching Chinese literals.
$collectorGuide = @(Get-ChildItem -LiteralPath $SourceDir -File -Filter '??????????.txt' |
    Where-Object { $_.BaseName.Length -eq 10 }
)
if ($collectorGuide) {
    $guides += $collectorGuide
}
if (-not $guides) {
    Write-Error "No text guide found in $SourceDir"
    exit 1
}

foreach ($file in $guides) {
    Copy-Item -LiteralPath $file.FullName -Destination (Join-Path $DestinationDir $file.Name) -Force
}

foreach ($name in @('LICENSE', 'THIRD_PARTY_NOTICES.md', 'ZBAR-LICENSE.txt', 'PYZBAR-LICENSE.txt', 'LIBICONV-NOTICE.txt')) {
    $source = Join-Path $SourceDir $name
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination (Join-Path $DestinationDir $name) -Force
    }
}
