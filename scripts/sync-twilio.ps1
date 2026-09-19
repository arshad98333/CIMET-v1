param(
    [Parameter(Mandatory = $true)]
    [string]$BaseUrl
)

$ErrorActionPreference = "Stop"
$clean = $BaseUrl.TrimEnd("/")
Invoke-RestMethod -Method Post -Uri "$clean/api/ops/configure-twilio"
Write-Host "Twilio sync requested for $clean"
