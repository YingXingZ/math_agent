param(
    [string]$AgentUrl = 'http://127.0.0.1:8000',
    [string]$WorkbenchUrl = 'http://127.0.0.1:8014'
)

$ErrorActionPreference = 'Stop'

function Assert-Endpoint([string]$Name, [string]$Url) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 10
        if ($response.StatusCode -ne 200) { throw "HTTP $($response.StatusCode)" }
        Write-Host "[PASS] $Name — $Url" -ForegroundColor Green
    } catch {
        Write-Host "[FAIL] $Name — $Url — $($_.Exception.Message)" -ForegroundColor Red
        throw
    }
}

Assert-Endpoint '8000 agent capabilities' "$AgentUrl/api/agent/capabilities"
Assert-Endpoint '8014 workbench health' "$WorkbenchUrl/api/health"
Assert-Endpoint '8014 OCR repair page' "$WorkbenchUrl/ocr-repair"
Write-Host 'Local health check passed. Remote VLM is used only when grading or OCR needs it.' -ForegroundColor Cyan
