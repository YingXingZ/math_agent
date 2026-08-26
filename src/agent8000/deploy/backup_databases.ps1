param(
    [string]$RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path,
    [string]$BackupDirectory = ''
)

$ErrorActionPreference = 'Stop'
if (-not $BackupDirectory) { $BackupDirectory = Join-Path $RepositoryRoot 'backups' }
New-Item -ItemType Directory -Force -Path $BackupDirectory | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$sources = @(
    (Join-Path $RepositoryRoot 'api.workbench.db'),
    (Join-Path $RepositoryRoot 'src\agent8000\data\homework.db')
)
foreach ($source in $sources) {
    if (-not (Test-Path -LiteralPath $source)) { throw "数据库不存在: $source" }
    $target = Join-Path $BackupDirectory ("{0}-{1}" -f ([IO.Path]::GetFileName($source)), $stamp)
    Copy-Item -LiteralPath $source -Destination $target -ErrorAction Stop
    Write-Host "[BACKUP] $target" -ForegroundColor Green
}
