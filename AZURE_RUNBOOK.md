# CIMEnergy Azure Runbook

This runbook deploys the CIMEnergy recovery app to Azure Container Apps with secrets stored in Azure Key Vault.

## Prerequisites

1. Install Azure CLI.
2. Install Docker if you want to build locally. The deployment script uses Azure Container Registry build, so local Docker is optional.
3. Log in to Azure:

```powershell
az login
az account set --subscription "<subscription-id-or-name>"
```

4. Confirm `.env` contains the live values:

```text
MONGO_URI
DEEPGRAM_API_KEY
TWILIO_ACCOUNT_SID
TWILIO_AUTH_TOKEN
TWILIO_PHONE_NUMBER
TEMPORAL_API
TEMPORAL_NAMESPACE
TEMPORAL_ID
AZURE_API_KEY
AZURE_ENDPOINT or AZURE_OPENAI_ENDPOINT
AZURE_LLM_MODEL
TINYFISH_API
```

## Deploy

Run this from the repository root:

```powershell
.\scripts\deploy-azure.ps1 `
  -ResourceGroup rg-cimenergy-hackathon `
  -Location australiaeast `
  -AppName cimenergy
```

The script creates or updates:

- Azure Container Registry
- Azure Key Vault
- User-assigned managed identity
- Log Analytics workspace
- Azure Container Apps environment
- Azure Container App
- Twilio voice webhook

The script prints the application URL when it completes.

## Verify

Open:

```text
https://<app-fqdn>/
https://<app-fqdn>/api/healthz
https://<app-fqdn>/api/integrations
https://<app-fqdn>/api/ops/paths
```

Twilio should point to:

```text
https://<app-fqdn>/api/voice/inbound
```

If Twilio needs to be synced again:

```powershell
.\scripts\sync-twilio.ps1 -BaseUrl "https://<app-fqdn>"
```

## GitHub Actions CI/CD

The workflow is in `.github/workflows/azure-container-app.yml`.

Create these GitHub repository secrets:

```text
AZURE_CLIENT_ID
AZURE_TENANT_ID
AZURE_SUBSCRIPTION_ID
```

Create this GitHub repository variable:

```text
ACR_NAME
```

The Azure identity used by GitHub Actions needs permission to build in ACR and update the Container App.

After that, every push to `main` that changes backend, frontend, Docker, infra, or workflow files builds a new image and updates Azure Container Apps.

## Local Development

Run locally:

```powershell
pip install -r requirements.txt
python -m backend.app.main
```

Open:

```text
http://127.0.0.1:8000
```

For Twilio local testing, expose port 8000 with ngrok and set `PUBLIC_BASE_URL` to the ngrok HTTPS URL.
