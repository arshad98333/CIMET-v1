OPENING_SCRIPT = (
    "Hi, this is the Energy recovery team calling about a comparison journey you started. "
    "This call may be recorded for quality and training. Is that okay with you?"
)
CONSENT_CLARIFY_SCRIPT = (
    "To confirm, may I continue this recorded call and ask the Energy journey questions?"
)
JOURNEY_CONTEXT_SCRIPT = (
    "You previously started an Energy comparison journey. I can help complete the remaining questions. Is that right?"
)
NO_ADVICE_SCRIPT = (
    "I can collect the information needed to complete the comparison journey, but I can't recommend "
    "a provider or plan. I can connect you with a team member if you would like advice."
)
PAYMENT_SCRIPT = (
    "I understand. For your security, please don't share card or payment details over this call. "
    "I'll connect you with a team member through the approved secure process."
)
DECLINE_SCRIPT = "Of course. I understand. I won't continue. Thank you for your time."
HANDOFF_SCRIPT = (
    "I hear you. I'm going to connect you with a team member so you don't need to repeat what you've already told me. "
    "I'll pass on the progress and why the handoff is happening."
)
EMPATHY_SCRIPTS = {
    "frustrated": "I hear that this has been frustrating. I'll keep this simple.",
    "worried": "I understand this is important. I'll keep the next step clear.",
    "confused": "No problem. I'll slow down and take this one step at a time.",
    "repeated": "I understand you don't want to repeat yourself. I'll pass the context to a team member.",
    "urgent": "I hear that this is time-sensitive. I'll keep this brief and get you to the right team.",
}
SILENCE_SCRIPT = "Are you still there?"
COMPLETION_SCRIPT = (
    "Your Energy journey has been completed using the test information provided. Thank you for your time."
)
UNRECOGNISED_JOURNEY_SCRIPT = (
    "No problem. I won't share extra details. I can connect you with a team member, or we can end here."
)

FIELD_QUESTIONS = {
    "postcode": "What is the property postcode?",
    "property_type": "Is the property a house, unit or another type of property?",
    "current_provider": "Who is your current energy provider?",
    "usage_pattern": "How would you describe your usual energy usage: low, standard or high?",
    "plan_preferences": "Do you have any plan preferences to include, such as green energy, or none?",
}
