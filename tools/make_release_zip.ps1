param(
    [Parameter(Mandatory = $true)]
    [string]$SourceDir,
    [Parameter(Mandatory = $true)]
    [string]$Destination
)

$files = Get-ChildItem -LiteralPath $SourceDir -File -Force |
    Select-Object -ExpandProperty FullName

if (-not $files) {
    Write-Error "Release directory contains no files: $SourceDir"
    exit 1
}

for ($attempt = 1; $attempt -le 5; $attempt++) {
    try {
        if (Test-Path -LiteralPath $Destination) {
            Remove-Item -LiteralPath $Destination -Force
        }
        Compress-Archive -LiteralPath $files -DestinationPath $Destination -CompressionLevel Optimal -ErrorAction Stop
        Write-Host "Release ZIP created: $Destination"
        exit 0
    }
    catch {
        if ($attempt -eq 5) {
            Write-Error $_
            exit 1
        }
        Start-Sleep -Seconds 2
    }
}
