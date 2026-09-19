# CIMEnergy Recovery

CIMEnergy Recovery is a live voice system for incomplete energy comparison journeys. It combines phone calling, a voice agent, durable workflow state, secure cloud deployment, operator approval, and audit evidence.

## Business Outcome

1. Recover dropped customer journeys through a guided voice conversation.
2. Capture only the fields needed to complete the energy form.
3. Respect consent, DNC, advice, payment, and handoff controls.
4. Resume from the last confirmed step if the call drops.
5. Store transcript, audio, field state, and KPI evidence per lead.

## Multi Agent System

| Tool | Business role |
|---|---|
| Twilio | Receives and routes the customer phone call |
| Deepgram | Runs the real time voice agent |
| FastAPI | Owns rules, tool calls, state changes, and dashboard updates |
| Temporal | Keeps workflow state durable across call drops and retries |
| Azure OpenAI | Supports KPI report analysis and intent decisions |
| TinyFish | Holds or submits the approved form journey |
| MongoDB Atlas | Stores leads, transcripts, events, audio, and reports |
| Azure Key Vault | Stores production secrets securely |

## Live Call Flow

1. Customer calls the Twilio number or starts a laptop voice call.
2. Twilio sends the inbound webhook to `/api/voice/inbound`.
3. FastAPI starts or resumes the Temporal workflow.
4. Twilio streams audio to `/api/voice/media`.
5. FastAPI bridges audio to Deepgram.
6. Deepgram speaks with the customer and calls FastAPI tools.
7. FastAPI validates consent, DNC, fields, advice, payment, and handoff rules.
8. MongoDB saves transcript, call events, form state, and audio.
9. TinyFish receives the approved form payload.
10. Azure OpenAI generates the executive KPI report.

## Required Credentials

Keep local values in `.env` for development. Use Azure Key Vault in production. Do not commit `.env`.

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

## Run Locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m backend.app.main
```

Open `http://127.0.0.1:8000`.

## Connect Twilio Locally

```powershell
ngrok http 8000
```

Set `PUBLIC_BASE_URL` to the ngrok HTTPS URL, restart the app, then sync Twilio:

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/ops/configure-twilio"
```

## Deploy To Azure

```powershell
az login
az account set --subscription "<subscription-id-or-name>"
.\scripts\deploy-azure.ps1 -ResourceGroup rg-cimenergy-hackathon -Location australiaeast -AppName cimenergy
```

Azure Container Apps hosts the UI and API. ACR stores the Docker image. Key Vault stores secrets. The deployment script updates `PUBLIC_BASE_URL` and can sync Twilio.

## Demo Flow

1. Open the Azure app URL.
2. Start a laptop voice call or call the Twilio number.
3. Give consent and answer the energy form questions.
4. Ask a normal process question to show conversation depth.
5. Ask for payment or advice to show guardrails.
6. Review and approve the TinyFish draft.
7. Open Calls to play and download audio.
8. Download the KPI PDF report.
