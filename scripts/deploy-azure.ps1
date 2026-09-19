param(
    [string]$ResourceGroup = "rg-cimenergy-hackathon",
    [string]$Location = "australiaeast",
    [string]$AppName = "cimenergy",
    [string]$AcrName = "",
    [string]$KeyVaultName = "",
    [string]$EnvironmentName = "",
    [string]$IdentityName = "",
    [string]$LogAnalyticsName = "",
    [string]$ImageTag = "latest",
    [switch]$SkipTwilioSync
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Require-Command($Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "$Name is required but was not found on PATH."
    }
}

function Read-DotEnv($Path) {
    $values = @{}
    if (-not (Test-Path $Path)) {
        return $values
    }
    foreach ($line in Get-Content $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#") -or -not $trimmed.Contains("=")) {
            continue
        }
        $parts = $trimmed.Split("=", 2)
        $key = $parts[0].Trim()
        $value = $parts[1].Trim().Trim('"').Trim("'")
        if ($key) {
            $values[$key] = $value
        }
    }
    return $values
}

function Get-Setting($Name, $Values, $Default = "") {
    $processValue = [Environment]::GetEnvironmentVariable($Name, "Process")
    if ($processValue) {
        return $processValue
    }
    if ($Values.ContainsKey($Name)) {
        return $Values[$Name]
    }
    return $Default
}

function To-KeyVaultSecretName($Name) {
    return $Name.ToLowerInvariant().Replace("_", "-")
}

Require-Command az

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$envValues = Read-DotEnv (Join-Path $root ".env")
$suffix = ($AppName.ToLowerInvariant() -replace "[^a-z0-9]", "")
if ($suffix.Length -gt 14) {
    $suffix = $suffix.Substring(0, 14)
}
if (-not $AcrName) { $AcrName = "$($suffix)acr$(Get-Random -Minimum 1000 -Maximum 9999)" }
if (-not $KeyVaultName) { $KeyVaultName = "$($suffix)-kv-$(Get-Random -Minimum 1000 -Maximum 9999)" }
if (-not $EnvironmentName) { $EnvironmentName = "$AppName-env" }
if (-not $IdentityName) { $IdentityName = "$AppName-mi" }
if (-not $LogAnalyticsName) { $LogAnalyticsName = "$AppName-logs" }

$secretEnvNames = @(
    "DEEPGRAM_API_KEY",
    "DEEPGRAM_PROJECT_ID",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_PHONE_NUMBER",
    "TEMPORAL_API",
    "TEMPORAL_NAMESPACE",
    "TEMPORAL_ADDRESS",
    "TEMPORAL_ID",
    "AZURE_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_ENDPOINT",
    "AZURE_LLM_MODEL",
    "TINYFISH_API",
    "TINYFISH_JOURNEY_URL",
    "MONGO_URI",
    "MONGODB_URI",
    "ELEVENLABS_API_KEY"
)

$plainEnv = @(
    @{ name = "CIMENERGY_ENABLE_LIVE_CALLS"; value = "true" },
    @{ name = "CIMENERGY_ENABLE_DEEPGRAM"; value = "true" },
    @{ name = "CIMENERGY_ENABLE_TEMPORAL"; value = "true" },
    @{ name = "CIMENERGY_ENABLE_AZURE"; value = "true" },
    @{ name = "CIMENERGY_ENABLE_TINYFISH"; value = "true" },
    @{ name = "MONGODB_DATABASE"; value = (Get-Setting "MONGODB_DATABASE" $envValues "cimenergy") },
    @{ name = "MONGODB_APP_NAME"; value = (Get-Setting "MONGODB_APP_NAME" $envValues "CIMEnergy") },
    @{ name = "DEEPGRAM_LISTEN_MODEL"; value = (Get-Setting "DEEPGRAM_LISTEN_MODEL" $envValues "flux-general-en") },
    @{ name = "DEEPGRAM_SPEAK_MODEL"; value = (Get-Setting "DEEPGRAM_SPEAK_MODEL" $envValues "aura-2-asteria-en") },
    @{ name = "DEEPGRAM_TELEPHONY_SPEAK_MODEL"; value = (Get-Setting "DEEPGRAM_TELEPHONY_SPEAK_MODEL" $envValues "flux-alexis-en") },
    @{ name = "DEEPGRAM_AGENT_LLM_PROVIDER"; value = (Get-Setting "DEEPGRAM_AGENT_LLM_PROVIDER" $envValues "open_ai") },
    @{ name = "DEEPGRAM_AGENT_LLM_MODEL"; value = (Get-Setting "DEEPGRAM_AGENT_LLM_MODEL" $envValues "gpt-4o-mini") },
    @{ name = "DEEPGRAM_AGENT_ENDPOINT"; value = (Get-Setting "DEEPGRAM_AGENT_ENDPOINT" $envValues "wss://agent.deepgram.com/v1/agent/converse") },
    @{ name = "TEMPORAL_TASK_QUEUE"; value = (Get-Setting "TEMPORAL_TASK_QUEUE" $envValues "cimenergy-recovery") },
    @{ name = "AZURE_OPENAI_API_VERSION"; value = (Get-Setting "AZURE_OPENAI_API_VERSION" $envValues "2024-10-21") },
    @{ name = "ELEVENLABS_VOICE_ID"; value = (Get-Setting "ELEVENLABS_VOICE_ID" $envValues "21m00Tcm4TlvDq8ikWAM") },
    @{ name = "ELEVENLABS_MODEL_ID"; value = (Get-Setting "ELEVENLABS_MODEL_ID" $envValues "eleven_multilingual_v2") }
)

$secretEnv = @()
$secretNames = @()
foreach ($name in $secretEnvNames) {
    $value = Get-Setting $name $envValues ""
    if (-not $value) {
        continue
    }
    $secretName = To-KeyVaultSecretName $name
    $secretEnv += @{ envName = $name; secretName = $secretName }
    $secretNames += @{ secretName = $secretName }
}

if (-not ($secretEnv | Where-Object { $_.envName -eq "MONGO_URI" -or $_.envName -eq "MONGODB_URI" })) {
    throw "MONGO_URI or MONGODB_URI is required in .env before deployment."
}

az extension add --name containerapp --upgrade --only-show-errors | Out-Null
az group create --name $ResourceGroup --location $Location --only-show-errors | Out-Null

$acr = az acr show --name $AcrName --resource-group $ResourceGroup --only-show-errors 2>$null | ConvertFrom-Json
if (-not $acr) {
    $acr = az acr create --name $AcrName --resource-group $ResourceGroup --sku Basic --admin-enabled false --only-show-errors | ConvertFrom-Json
}

$vault = az keyvault show --name $KeyVaultName --resource-group $ResourceGroup --only-show-errors 2>$null | ConvertFrom-Json
if (-not $vault) {
    $vault = az keyvault create --name $KeyVaultName --resource-group $ResourceGroup --location $Location --only-show-errors | ConvertFrom-Json
}

$identity = az identity show --name $IdentityName --resource-group $ResourceGroup --only-show-errors 2>$null | ConvertFrom-Json
if (-not $identity) {
    $identity = az identity create --name $IdentityName --resource-group $ResourceGroup --location $Location --only-show-errors | ConvertFrom-Json
}

$account = az account show --only-show-errors | ConvertFrom-Json
$signedInUser = $account.user.name
$signedInObjectId = az ad signed-in-user show --query id -o tsv --only-show-errors
if ($signedInObjectId) {
    az role assignment create --assignee-object-id $signedInObjectId --assignee-principal-type User --role "Key Vault Secrets Officer" --scope $vault.id --only-show-errors 2>$null | Out-Null
}
az role assignment create --assignee-object-id $identity.principalId --assignee-principal-type ServicePrincipal --role "Key Vault Secrets User" --scope $vault.id --only-show-errors 2>$null | Out-Null
az role assignment create --assignee-object-id $identity.principalId --assignee-principal-type ServicePrincipal --role AcrPull --scope $acr.id --only-show-errors 2>$null | Out-Null

if ($signedInObjectId) {
    Write-Host "Waiting for Key Vault RBAC propagation..."
    Start-Sleep -Seconds 35
} else {
    Write-Warning "Could not resolve signed-in user object id for $signedInUser. Secret upload may require Key Vault RBAC assignment."
}

foreach ($item in $secretEnv) {
    $value = Get-Setting $item.envName $envValues ""
    az keyvault secret set --vault-name $KeyVaultName --name $item.secretName --value $value --only-show-errors | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to set Key Vault secret $($item.secretName). Confirm the signed-in user has Key Vault Secrets Officer on $KeyVaultName."
    }
}

$workspace = az monitor log-analytics workspace show --resource-group $ResourceGroup --workspace-name $LogAnalyticsName --only-show-errors 2>$null | ConvertFrom-Json
if (-not $workspace) {
    $workspace = az monitor log-analytics workspace create --resource-group $ResourceGroup --workspace-name $LogAnalyticsName --location $Location --only-show-errors | ConvertFrom-Json
}

$containerEnv = az containerapp env show --name $EnvironmentName --resource-group $ResourceGroup --only-show-errors 2>$null | ConvertFrom-Json
if (-not $containerEnv) {
    $customerId = az monitor log-analytics workspace show --resource-group $ResourceGroup --workspace-name $LogAnalyticsName --query customerId -o tsv
    $sharedKey = az monitor log-analytics workspace get-shared-keys --resource-group $ResourceGroup --workspace-name $LogAnalyticsName --query primarySharedKey -o tsv
    $containerEnv = az containerapp env create --name $EnvironmentName --resource-group $ResourceGroup --location $Location --logs-workspace-id $customerId --logs-workspace-key $sharedKey --only-show-errors | ConvertFrom-Json
}

$image = "$($acr.loginServer)/$AppName`:$ImageTag"
az acr build --registry $AcrName --image "$AppName`:$ImageTag" $root --only-show-errors | Out-Null

$params = @{
    location = @{ value = $Location }
    appName = @{ value = $AppName }
    managedEnvironmentId = @{ value = $containerEnv.id }
    userAssignedIdentityId = @{ value = $identity.id }
    keyVaultName = @{ value = $KeyVaultName }
    acrLoginServer = @{ value = $acr.loginServer }
    imageName = @{ value = $image }
    plainEnv = @{ value = $plainEnv }
    secretEnv = @{ value = $secretEnv }
    secretNames = @{ value = $secretNames }
}
$paramFile = Join-Path $env:TEMP "cimenergy-azure-params.json"
@{ '$schema' = "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#"; contentVersion = "1.0.0.0"; parameters = $params } |
    ConvertTo-Json -Depth 20 |
    Set-Content -Path $paramFile -Encoding utf8

$deployment = az deployment group create `
    --resource-group $ResourceGroup `
    --template-file (Join-Path $root "infra\main.bicep") `
    --parameters "@$paramFile" `
    --only-show-errors | ConvertFrom-Json

$fqdn = $deployment.properties.outputs.fqdn.value
$publicBaseUrl = "https://$fqdn"
az containerapp update `
    --name $AppName `
    --resource-group $ResourceGroup `
    --set-env-vars "PUBLIC_BASE_URL=$publicBaseUrl" "SERVER_EXTERNAL_URL=$publicBaseUrl" "PUBLIC_HOSTNAME=$publicBaseUrl" `
    --only-show-errors | Out-Null

if (-not $SkipTwilioSync) {
    Start-Sleep -Seconds 15
    try {
        Invoke-RestMethod -Method Post -Uri "$publicBaseUrl/api/ops/configure-twilio" | Out-Null
    } catch {
        Write-Warning "Azure deployment succeeded, but Twilio sync did not complete. Run scripts\sync-twilio.ps1 -BaseUrl $publicBaseUrl"
    }
}

Write-Host "CIMEnergy deployed."
Write-Host "Application URL: $publicBaseUrl"
Write-Host "Twilio inbound webhook: $publicBaseUrl/api/voice/inbound"
Write-Host "Twilio status webhook: $publicBaseUrl/api/voice/status"
