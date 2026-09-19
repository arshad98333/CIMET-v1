# CIMEnergy Workflow 2: Consent Decline and Opt-Out

## Use case

Test that CIMEnergy stops immediately when a customer does not consent to recording or asks not to be contacted. No journey information may be collected.

**Test lead:** `energy-lead-007`  
**DNC status:** `clear`  
**Expected outcome:** `declined`

## Preconditions

- CIMEnergy is running with synthetic data.
- `energy-lead-007` is available in MongoDB.
- The test call simulator is enabled.

## Step-by-step workflow

### 1. Select the lead

1. Open the CIMEnergy lead list.
2. Find `energy-lead-007`.
3. Confirm it is synthetic and has status `dropped_off`.
4. Confirm no consent has been recorded.

**Expected:** The lead detail view shows `consent_status: not_requested`.

### 2. Run the DNC gate

1. Click **Start recovery call**.
2. Confirm the backend returns `clear`.
3. Confirm the test call starts.

**Expected:** `dnc_clear` occurs before `call_connected`.

### 3. Disclose recording and request consent

Agent says:

> “Hi, this is the Energy recovery team calling about a comparison journey you started. This call may be recorded for quality and training. Is that okay with you?”

Test response:

> “No, I don’t want to be recorded.”

Agent says:

> “No problem. Thank you for your time.”

**Expected:** The backend records `consent_declined`. The call ends immediately.

### 4. Verify no collection occurred

Check the dashboard and MongoDB event log.

**Expected:**

- No postcode, property or provider question was asked.
- No journey field was added.
- No payload was submitted.
- The outcome is `declined`.
- The call contains `call_ended`.

### 5. Repeat the opt-out test

Run the same workflow with a fresh test call or reset fixture.

At any point after consent, test response:

> “Stop calling me. I’m not interested.”

Agent says:

> “Understood. I won’t continue. Thank you for your time.”

**Expected:**

- The call ends without another question.
- The event log contains `customer_declined`.
- The lead is marked suppressed for the prototype.
- No retry or pressure loop is created.

## Validation checklist

- Consent was required before collection.
- A refusal ended the call.
- “Stop calling” was treated as a terminal decision.
- No journey data or payload was created.
- No repeated offer or persuasion occurred.
- The dashboard shows the decline reason.

## Pass condition

Pass only if CIMEnergy ends the interaction politely and prevents further collection or recovery attempts for the declined test path.
