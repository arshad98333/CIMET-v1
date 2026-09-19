# CIMEnergy Workflow 1: Successful Energy Journey Recovery

## Use case

Recover a synthetic Energy lead that abandoned the journey after entering the postcode. Collect the remaining fields, submit the test payload, and close the call.

**Test lead:** `energy-lead-001`  
**DNC status:** `clear`  
**Expected outcome:** `completed`

## Preconditions

- CIMEnergy frontend, FastAPI backend and MongoDB are running.
- Synthetic leads are seeded.
- The test voice number or simulator is enabled.
- No real customer data is used.

## Step-by-step workflow

### 1. Select the lead

1. Open the CIMEnergy lead list.
2. Find `energy-lead-001`.
3. Confirm the lead is labelled synthetic.
4. Confirm the next step is `property_type`.
5. Open the lead detail view.

**Expected:** The dashboard shows postcode `3000`, pending status and no call started.

### 2. Run the DNC gate

1. Click **Start recovery call**.
2. The backend checks `dnc_status` before dialling.
3. Confirm the result is `clear`.
4. Allow the test call to start.

**Expected:** The event log contains `dnc_clear` before `call_connected`.

### 3. Disclose recording and obtain consent

Agent says:

> “Hi, this is the Energy recovery team calling about a comparison journey you started. This call may be recorded for quality and training. Is that okay with you?”

Test response:

> “Yes, that’s okay.”

**Expected:** The backend records `consent_granted`. No journey field is collected before this event.

### 4. Confirm the unfinished journey

Agent says:

> “You previously entered postcode 3000. I’ll help complete the remaining Energy comparison questions. Is that right?”

Test response:

> “Yes.”

**Expected:** The call state becomes `in_progress`.

### 5. Collect the remaining fields

Ask one question at a time:

1. “Is the property a house, unit or something else?”  
   **Response:** `house`
2. “Who is your current energy provider?”  
   **Response:** `synthetic-provider-a`
3. “How would you describe your usual energy usage?”  
   **Response:** `standard`
4. “Do you have any additional details you want included in the journey?”  
   **Response:** `none`

**Expected:** Each answer is validated and shown as a structured field. Do not ask for card or payment information.

### 6. Confirm the collected information

Agent says:

> “I have postcode 3000, property type house, current provider synthetic-provider-a and standard usage. Is that correct?”

Test response:

> “Yes.”

**Expected:** The fields are marked confirmed. The agent does not recommend a provider or plan.

### 7. Submit the test payload

1. The backend validates the completed Pydantic payload.
2. Submit it to the journey sandbox or mock.
3. Confirm the response is successful.

**Expected:** The event log contains `test_payload_submitted` and `journey_completed`.

### 8. Close the call

Agent says:

> “Your Energy journey has been completed using the test information provided. Thank you for your time.”

**Expected:** The call ends with `call_ended`. The lead outcome is `completed`.

## Validation checklist

- DNC check happened before dialling.
- Recording disclosure happened before data collection.
- Only synthetic values were used.
- No advice was given.
- No payment data was requested.
- MongoDB contains the updated lead and call events.
- The dashboard shows completion and the submitted test payload.

## Pass condition

Pass only if the journey completes and every expected event appears in the correct order.
