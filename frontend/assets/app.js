const titles = {
  callme: ["Call me", "Energy agent"],
  overview: ["Home", "Performance"],
  queue: ["Queue", "Records"],
  lab: ["Lab", "Conversation"],
  scripts: ["Scripts", "Ingest"],
  inbound: ["Calls", "Phone"],
  drafts: ["Drafts", "Approvals"],
};

const labels = {
  postcode: "Postcode",
  property_type: "Property type",
  current_provider: "Current provider",
  usage_pattern: "Usage pattern",
  plan_preferences: "Plan preferences",
};

const serviceLabels = {
  Twilio: "Inbound calling",
  ElevenLabs: "Customer voice",
  Deepgram: "Voice agent",
  Temporal: "Workflow",
  "Azure OpenAI": "Intent check",
  TinyFish: "Form hold",
};

let leads = [];
let selectedLeadId = null;
let detail = null;
let lastCallId = null;
let lab = { callId: null, agentLine: "", history: [] };
let liveSocket = null;
let liveSource = null;
let liveLeadId = null;
let globalSocket = null;
let globalSource = null;
let callMeDraftId = null;
let inboundFocusId = null;
let browserChatSocket = null;
let browserRecognition = null;
let browserVoiceSocket = null;
let browserVoiceStream = null;
let browserVoiceContext = null;
let browserVoiceSource = null;
let browserVoiceProcessor = null;
let browserVoiceSilentSink = null;
let browserVoicePlaybackTime = 0;
let deskDid = "+1 (937) 858-6417";
let seen = new Set();
const streams = new Map();

async function api(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch(path, { headers: { "Content-Type": "application/json" }, signal: controller.signal, ...options });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || payload.error || payload.message || `HTTP ${response.status}`);
    return payload;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("Timed out after 20s. The desk did not finish the request.");
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function setLive(on) {
  document.querySelector("#liveDot").classList.toggle("on", on);
  document.querySelector("#liveLabel").textContent = on ? "Live" : "Idle";
}

function showError(text) {
  const bar = document.querySelector("#alertBar");
  if (!bar) return;
  if (!text) {
    bar.classList.add("hidden");
    bar.textContent = "";
    return;
  }
  bar.textContent = text;
  bar.classList.remove("hidden");
}

function logOps(text, level = "info") {
  const box = document.querySelector("#opsLog");
  if (!box || !text) return;
  const row = document.createElement("div");
  row.className = level === "error" ? "err" : "ok";
  row.textContent = `${new Date().toISOString().slice(11, 19)}  ${text}`;
  box.prepend(row);
  while (box.children.length > 40) box.removeChild(box.lastChild);
}

function attachLiveChannel(kind, leadId) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${proto}://${location.host}/api/live/${encodeURIComponent(leadId)}`);
  socket.onopen = () => setLive(true);
  socket.onclose = () => {
    const sse = kind === "global" ? globalSource : liveSource;
    const other = kind === "global" ? liveSocket : globalSocket;
    setLive(Boolean((sse && sse.readyState === EventSource.OPEN) || (other && other.readyState === WebSocket.OPEN)));
  };
  socket.onmessage = (event) => {
    handleLiveEvent(JSON.parse(event.data));
  };
  const source = new EventSource(`/api/live/${encodeURIComponent(leadId)}/sse`);
  source.onopen = () => setLive(true);
  source.onerror = () => {
    const otherSse = kind === "global" ? liveSource : globalSource;
    const ws = kind === "global" ? globalSocket : liveSocket;
    const extra = kind === "global" ? liveSocket : globalSocket;
    setLive(
      Boolean(
        (ws && ws.readyState === WebSocket.OPEN) ||
          (extra && extra.readyState === WebSocket.OPEN) ||
          (otherSse && otherSse.readyState === EventSource.OPEN)
      )
    );
  };
  source.onmessage = (event) => {
    if (!event.data || event.data === "{}") return;
    handleLiveEvent(JSON.parse(event.data));
  };
  return { socket, source };
}

function connectGlobalLive() {
  if (globalSource && globalSocket) return;
  const channel = attachLiveChannel("global", "*");
  globalSocket = channel.socket;
  globalSource = channel.source;
}

function connectLive(leadId) {
  if (!leadId || leadId === "*") {
    connectGlobalLive();
    return;
  }
  connectGlobalLive();
  if (liveLeadId === leadId && liveSource) return;
  if (liveSocket) liveSocket.close();
  if (liveSource) liveSource.close();
  liveLeadId = leadId;
  const channel = attachLiveChannel("lead", leadId);
  liveSocket = channel.socket;
  liveSource = channel.source;
}

function formatDid(raw) {
  const digits = String(raw || "").replace(/\D/g, "");
  if (digits.length === 11 && digits.startsWith("1")) {
    return `+1 (${digits.slice(1, 4)}) ${digits.slice(4, 7)}-${digits.slice(7)}`;
  }
  if (digits.length === 10) {
    return `+1 (${digits.slice(0, 3)}) ${digits.slice(3, 6)}-${digits.slice(6)}`;
  }
  return String(raw || deskDid);
}

function applyDid(paths) {
  const raw = (paths && (paths.twilio_number_display || paths.twilio_number)) || deskDid;
  deskDid = formatDid(raw);
  const header = document.querySelector("#headerDid");
  const queue = document.querySelector("#queueDid");
  const inboundTitle = document.querySelector("#inboundDidTitle");
  const hint = document.querySelector("#overviewHint");
  if (header) header.textContent = `Dial ${deskDid}`;
  const callmeDid = document.querySelector("#callmeDid");
  if (callmeDid) callmeDid.textContent = deskDid;
  if (queue) queue.textContent = `Dial ${deskDid}`;
  if (inboundTitle) inboundTitle.textContent = deskDid;
  if (hint && (!selectedLeadId || hint.textContent.includes("Call +") || hint.textContent.includes("Dial"))) {
    hint.textContent = `Call ${deskDid} from your mobile. The live thread fills as soon as the call connects.`;
  }
}

async function onInboundStarted(payload) {
  inboundFocusId = payload.lead_id || inboundFocusId;
  selectedLeadId = payload.lead_id || selectedLeadId;
  lastCallId = payload.call_id || lastCallId;
  setLive(true);
  const from = payload.from || "a mobile";
  logOps(`Inbound from ${from} on ${payload.twilio_number_display || deskDid}`);
  switchView("callme");
  connectLive(payload.lead_id);
  try {
    await loadAll();
  } catch (_error) {
    renderLeadRows();
  }
}

function handleLiveEvent(payload) {
  if (payload.id) {
    const eventKey = `e:${payload.id}`;
    if (seen.has(eventKey)) return;
    seen.add(eventKey);
  }
  if (payload.kind === "token" && payload.delta) {
    applyToken(payload);
    setLive(true);
    return;
  }
  if (payload.kind === "transcript" && payload.text) {
    if (looksLikeOpsError(payload.text)) return;
    finishMessage(payload);
    setLive(true);
    return;
  }
  if (payload.kind === "tinyfish") {
    renderCallMeForm(payload);
    setLive(true);
    logOps("Energy form updated");
    return;
  }
  if (payload.kind === "twilio_inbound") {
    onInboundStarted(payload);
    return;
  }
  if (payload.kind === "ops" && payload.text) {
    logOps(payload.text, payload.level || "info");
    if (payload.level === "error") showError(payload.text);
    if (payload.inbound && payload.lead_id) {
      inboundFocusId = payload.lead_id;
      setLive(true);
      loadAll().catch(() => renderLeadRows());
    }
  }
}

function threads() {
  return ["callmeTranscript", "homeTranscript", "queueTranscript", "labTranscript", "inboundTranscript"]
    .map((id) => document.querySelector(`#${id}`))
    .filter(Boolean);
}

function applyToken(payload) {
  const role = payload.role === "user" ? "customer" : "agent";
  const id = payload.message_id || `open-${role}`;
  threads().forEach((root) => {
    const mapKey = `${root.id}:${id}`;
    let node = streams.get(mapKey);
    if (!node) {
      node = makeBubble(role, "", true);
      root.appendChild(node);
      streams.set(mapKey, node);
    }
    const copy = node.querySelector(".copy");
    copy.textContent += payload.delta;
    copy.classList.add("typing");
    root.scrollTop = root.scrollHeight;
  });
}

function looksLikeOpsError(text) {
  const value = String(text || "");
  return /HTTP \d+|unverified|Unable to create record|Trial accounts|live_error/i.test(value);
}

function finishMessage(payload) {
  if (looksLikeOpsError(payload.text)) return;
  const role = payload.role === "user" ? "customer" : "agent";
  const lineKey = payload.text ? `line:${payload.role}:${payload.text}` : "";
  if (lineKey && seen.has(lineKey)) return;
  if (lineKey) seen.add(lineKey);
  const id = payload.message_id || `done-${payload.ts}`;
  threads().forEach((root) => {
    const mapKey = `${root.id}:${id}`;
    let node = streams.get(mapKey);
    if (!node) {
      node = makeBubble(role, payload.text, false);
      root.appendChild(node);
      streams.set(mapKey, node);
    } else {
      const copy = node.querySelector(".copy");
      copy.textContent = payload.text;
      copy.classList.remove("typing");
    }
    root.scrollTop = root.scrollHeight;
  });
}

function makeBubble(role, text, typing) {
  const node = document.createElement("div");
  node.className = `bubble ${role}`;
  node.innerHTML = `<strong>${role === "agent" ? "Agent" : "Caller"}</strong><div class="copy${typing ? " typing" : ""}"></div>`;
  node.querySelector(".copy").textContent = text;
  return node;
}

function display(value) {
  if (!value) return "-";
  return labels[value] || String(value).replaceAll("_", " ");
}

function showScript(text) {
  const box = document.querySelector("#scriptBox");
  if (!box) return;
  if (!text) {
    box.classList.add("hidden");
    box.textContent = "";
    return;
  }
  box.textContent = text;
  box.classList.remove("hidden");
}

function switchView(name) {
  document.querySelectorAll(".view").forEach((node) => node.classList.add("hidden"));
  document.querySelector(`#view-${name}`).classList.remove("hidden");
  document.querySelectorAll(".nav-item").forEach((node) => node.classList.toggle("active", node.dataset.view === name));
  document.querySelector("#viewKicker").textContent = titles[name][0];
  document.querySelector("#viewTitle").textContent = titles[name][1];
}

async function loadAll() {
  const [leadPayload, integrations, overview, paths, drafts, scripts, callme] = await Promise.all([
    api("/api/leads"),
    api("/api/integrations"),
    api("/api/ops/overview"),
    api("/api/ops/paths"),
    api("/api/drafts"),
    api("/api/scripts"),
    api("/api/call-me"),
  ]);
  leads = leadPayload;
  applyDid(paths);
  connectGlobalLive();
  renderHooks(overview);
  renderIntegrations(integrations.integrations);
  renderLeadRows();
  renderLeadSelects();
  renderInbound(paths);
  renderDrafts(drafts);
  renderScripts(scripts);
  renderCallMe(callme);
  connectLive("*");
  if (selectedLeadId) await loadDetail(selectedLeadId);
}

function renderCallMeForm(payload) {
  const root = document.querySelector("#callmeForm");
  const status = document.querySelector("#callmeStatus");
  const approve = document.querySelector("#callmeApprove");
  if (!root) return;
  const fields = (payload && payload.payload) || {};
  callMeDraftId = payload && payload.draft_id ? payload.draft_id : callMeDraftId;
  root.innerHTML = Object.entries(labels)
    .map(([key, label]) => `<div class="field-item"><span>${label}</span><strong>${fields[key] || "Open"}</strong></div>`)
    .join("");
  if (status) status.textContent = payload && payload.journey === "in_progress" ? "Live" : payload && payload.status ? payload.status : "Ready";
  if (approve) {
    const missing = (payload && payload.missing_fields) || Object.keys(labels).filter((key) => !fields[key]);
    approve.disabled = missing.length > 0 || !callMeDraftId;
  }
}

function renderCallMe(state) {
  if (!state) return;
  applyDid({ twilio_number_display: state.did_display, twilio_number: state.did });
  const status = document.querySelector("#callmeStatus");
  if (status) status.textContent = state.live ? "Live" : "Ready";
  setLive(Boolean(state.live));
  if (state.lead && state.lead.lead_id) {
    inboundFocusId = state.lead.lead_id;
    selectedLeadId = state.lead.lead_id;
    lastCallId = state.lead.active_call_id;
  }
  renderCallMeForm({
    payload: (state.draft && state.draft.payload) || {},
    draft_id: state.draft && state.draft.draft_id,
    missing_fields: state.draft && state.draft.missing_fields,
    journey: state.lead && state.lead.journey_status,
    status: state.draft && state.draft.status,
  });
  const thread = document.querySelector("#callmeTranscript");
  if (thread && state.lines && state.lines.length && !thread.children.length) {
    state.lines.forEach((line) => {
      if (!line.text) return;
      finishMessage({ role: line.role || "assistant", text: line.text, message_id: `cm-${line.text.slice(0, 12)}` });
    });
  }
}

function renderHooks(overview) {
  const items = [
    [overview.recovered, "Recovered"],
    [overview.drafts_pending, "Drafts"],
    [overview.handoffs, "Handoffs"],
    [overview.declined, "Declined"],
    [overview.dropped, "Open"],
    [overview.consent_granted, "Consent"],
  ];
  document.querySelector("#hooks").innerHTML = items
    .map(([value, label]) => `<div class="hook"><strong>${value}</strong><span>${label}</span></div>`)
    .join("");
}

function renderIntegrations(items) {
  document.querySelector("#integrationRows").innerHTML = items
    .map((item) => {
      const ready = item.configured && item.missing_packages.length === 0;
      const klass = item.enabled && ready ? "ok" : ready ? "warn" : "danger";
      const title = serviceLabels[item.name] || item.purpose;
      return `<div class="integration-card"><strong>${title}</strong><span class="tag ${klass}">${item.mode}</span><small>${item.purpose}</small></div>`;
    })
    .join("");
}

function renderLeadRows() {
  const root = document.querySelector("#leadRows");
  if (!root) return;
  const ordered = [...leads].sort((a, b) => {
    const aIn = a.lead_id === inboundFocusId || String(a.lead_id).startsWith("inbound-") ? 0 : 1;
    const bIn = b.lead_id === inboundFocusId || String(b.lead_id).startsWith("inbound-") ? 0 : 1;
    if (aIn !== bIn) return aIn - bIn;
    if (a.lead_id === inboundFocusId) return -1;
    if (b.lead_id === inboundFocusId) return 1;
    return 0;
  });
  root.innerHTML = ordered
    .map((lead) => {
      const dnc = lead.dnc_status === "clear" ? "ok" : lead.dnc_status === "unknown" ? "warn" : "danger";
      const inbound = lead.lead_id === inboundFocusId || String(lead.lead_id).startsWith("inbound-");
      const phone = inbound ? formatDid(lead.test_phone) : lead.test_phone;
      const title = inbound ? "Inbound caller" : lead.lead_id;
      return `<button class="lead-row ${lead.lead_id === selectedLeadId ? "active" : ""} ${lead.lead_id === inboundFocusId ? "inbound-live" : ""}" data-lead="${lead.lead_id}">
        <strong>${title}</strong>
        <span class="muted">${display(lead.next_step)}  ${phone}</span>
        <span class="tag ${dnc}">${lead.dnc_status}</span>
        <span class="tag">${display(lead.journey_status)}</span>
      </button>`;
    })
    .join("");
  root.querySelectorAll("[data-lead]").forEach((button) => button.addEventListener("click", () => loadDetail(button.dataset.lead)));
}

function renderLeadSelects() {
  const options = leads
    .map((lead) => `<option value="${lead.lead_id}" ${lead.lead_id === selectedLeadId ? "selected" : ""}>${lead.lead_id}</option>`)
    .join("");
  ["labLead", "scriptLead", "inboundLead", "callmeLead"].forEach((id) => {
    const node = document.querySelector(`#${id}`);
    if (node) node.innerHTML = options;
  });
}

async function loadDetail(leadId) {
  selectedLeadId = leadId;
  connectLive(leadId);
  detail = await api(`/api/leads/${leadId}`);
  lastCallId = detail.lead.active_call_id;
  renderLeadRows();
  const { lead } = detail;
  document.querySelector("#emptyState").classList.add("hidden");
  document.querySelector("#detailState").classList.remove("hidden");
  document.querySelector("#leadTitle").textContent = lead.customer_label;
  document.querySelector("#leadPhone").textContent = String(lead.lead_id).startsWith("inbound-")
    ? formatDid(lead.test_phone)
    : lead.test_phone;
  document.querySelector("#journeyFacts").innerHTML = Object.entries({
    DNC: lead.dnc_status,
    State: lead.call_state,
    Consent: lead.consent_status,
    Next: lead.next_step,
    Outcome: lead.outcome || "Pending",
  })
    .map(([k, v]) => `<dt>${k}</dt><dd>${display(v)}</dd>`)
    .join("");
  document.querySelector("#capturedFields").innerHTML = Object.entries(labels)
    .map(([key, label]) => `<div class="field-item"><span>${label}</span><strong>${lead.captured_fields[key] || "Open"}</strong></div>`)
    .join("");
  document.querySelector("#events").innerHTML = detail.events.length
    ? detail.events
        .slice()
        .reverse()
        .slice(0, 12)
        .map((event) => `<div class="event"><strong>${display(event.event_type)}</strong><span>${event.message}</span></div>`)
        .join("")
    : `<p class="muted">Quiet</p>`;
  document.querySelector("#handoff").innerHTML = renderOutcome(detail);
  renderCallRecordings(detail);
  document.querySelector("#submitButton").disabled = detail.missing_fields.length > 0 || lead.consent_status !== "granted";
  const hint = document.querySelector("#overviewHint");
  if (hint) hint.textContent = `${deskDid}  ${lead.lead_id}`;
  try {
    const transcript = await api(`/api/leads/${leadId}/transcript`);
    for (const line of transcript.lines || []) {
      if (!line.text) continue;
      seen.add(`line:${line.role || "assistant"}:${line.text}`);
      finishMessage({
        role: line.role || "assistant",
        text: line.text,
        message_id: `hist-${line.ts}-${(line.text || "").slice(0, 16)}`,
      });
    }
  } catch (_error) {
    /* live sockets still attach */
  }
}

function renderOutcome(current) {
  const handoffs = current.handoffs.map((item) => `<div class="handoff-card"><strong>${item.reason}</strong><span>${item.missing_fields.map(display).join(", ") || "Complete"}</span></div>`).join("");
  const submissions = current.submissions.map((item) => `<div class="submission-card"><strong>${item.status}</strong></div>`).join("");
  return handoffs || submissions || `<p class="muted">None</p>`;
}

function artifactTitle(item) {
  const source = {
    twilio: "Twilio call recording",
    deepgram: "Agent audio",
    voice_bridge: "Caller audio",
    elevenlabs: "Customer lab audio",
    script: "Uploaded script",
  }[item.source] || display(item.source);
  const type = {
    recording: "Recording",
    caller_audio: "Caller side",
    agent_audio: "Agent side",
    transcript: "Transcript",
    metadata: "Session metadata",
  }[item.artifact_type] || display(item.artifact_type);
  return item.source === "twilio" ? source : `${source} - ${type}`;
}

function isPlayableAudio(item) {
  return ["audio/mpeg", "audio/mp3", "audio/wav", "audio/webm", "audio/ogg", "audio/mp4"].includes(item.content_type);
}

function renderCallRecordings(current) {
  const root = document.querySelector("#inboundRecordings");
  const count = document.querySelector("#recordingCount");
  if (!root) return;
  const artifacts = ((current && current.voice_artifacts) || [])
    .filter((item) => ["recording", "caller_audio", "agent_audio"].includes(item.artifact_type))
    .slice()
    .reverse();
  if (count) count.textContent = artifacts.length ? `${artifacts.length} saved` : "No recordings";
  root.innerHTML = artifacts.length
    ? artifacts
        .map((item) => {
          const url = `/api/voice/artifacts/${encodeURIComponent(item.artifact_id)}`;
          const downloadUrl = `${url}?download=true`;
          const meta = [
            item.call_id,
            item.content_type,
            item.size_bytes ? `${item.size_bytes} bytes` : item.storage === "external" ? "External recording" : "Stored in MongoDB",
          ]
            .filter(Boolean)
            .join("  ");
          return `<article class="recording-item">
            <div>
              <strong>${artifactTitle(item)}</strong>
              <span class="muted">${meta}</span>
            </div>
            ${isPlayableAudio(item) ? `<audio controls preload="none" src="${url}"></audio>` : `<span class="muted">Raw telephony audio. Download to inspect or convert.</span>`}
            <a class="download-link" href="${downloadUrl}" download="${item.filename}">Download</a>
          </article>`;
        })
        .join("")
    : `<p class="muted">No call audio has been saved for this record yet. Twilio recordings and Deepgram bridge audio appear here after a connected call ends.</p>`;
}

async function startCall() {
  if (!selectedLeadId) {
    showError("Select a record first.");
    logOps("Start ignored. No record selected.", "error");
    return;
  }
  showError("");
  logOps(`Start ${selectedLeadId}`);
  connectLive(selectedLeadId);
  try {
    const result = await api(`/api/leads/${selectedLeadId}/call`, { method: "POST" });
    lastCallId = result.call_id || result.lead.active_call_id;
    const note = result.error || result.message || "No response body.";
    logOps(note, result.error || result.ok === false ? "error" : "info");
    if (result.error || result.ok === false) showError(note);
    else showError("");
    showScript(result.script || result.message);
    if (result.ok !== false && result.script) {
      finishMessage({
        role: "assistant",
        text: result.script,
        message_id: `start-${Date.now()}`,
      });
    }
    await loadAll();
  } catch (error) {
    showError(error.message);
    logOps(error.message, "error");
  }
}

async function sendEvent(eventType, fields = {}) {
  if (!lastCallId) return showScript("Start a call first.");
  try {
    const result = await api(`/api/calls/${lastCallId}/event`, { method: "POST", body: JSON.stringify({ event_type: eventType, fields }) });
    showScript(result.script || result.message);
    await loadAll();
  } catch (error) {
    showScript(error.message);
  }
}

async function startLab() {
  const leadId = document.querySelector("#labLead").value;
  connectLive(leadId);
  const result = await api(`/api/lab/start?lead_id=${encodeURIComponent(leadId)}`, { method: "POST" });
  lab = { callId: result.call_id, agentLine: result.agent_line, history: [{ role: "assistant", content: result.agent_line }] };
  finishMessage({ role: "assistant", text: result.agent_line, message_id: "open" });
}

async function nextLabTurn() {
  if (!lab.callId) return;
  const result = await api("/api/lab/turn", {
    method: "POST",
    body: JSON.stringify({ call_id: lab.callId, agent_line: lab.agentLine, history: lab.history }),
  });
  finishMessage({ role: "user", text: result.customer_line, message_id: `c-${Date.now()}` });
  if (result.agent_line) finishMessage({ role: "assistant", text: result.agent_line, message_id: `a-${Date.now()}` });
  lab.agentLine = result.agent_line || lab.agentLine;
  lab.history.push({ role: "user", content: result.customer_line });
  if (result.agent_line) lab.history.push({ role: "assistant", content: result.agent_line });
  await loadAll();
}

async function runAutonomous() {
  const leadId = document.querySelector("#labLead").value;
  connectLive(leadId);
  await api("/api/lab/autonomous", {
    method: "POST",
    body: JSON.stringify({ lead_id: leadId, max_turns: 8, wait: false }),
  });
}

async function placeTwilioCall() {
  switchView("callme");
  logOps(`Call ${deskDid} from your mobile. The energy agent answers on this page.`);
  showError("");
}

function setBrowserChatState(text, active = false) {
  const state = document.querySelector("#browserChatState");
  const start = document.querySelector("#browserChatStart");
  const end = document.querySelector("#browserChatEnd");
  const voiceStart = document.querySelector("#browserVoiceStart");
  const voiceEnd = document.querySelector("#browserVoiceEnd");
  const input = document.querySelector("#browserCustomerText");
  if (state) state.textContent = text;
  const voiceActive = Boolean(browserVoiceSocket && browserVoiceSocket.readyState === WebSocket.OPEN);
  const chatActive = Boolean(browserChatSocket && browserChatSocket.readyState === WebSocket.OPEN);
  if (start) start.disabled = active || voiceActive;
  if (end) end.disabled = !chatActive;
  if (voiceStart) voiceStart.disabled = active || chatActive || voiceActive;
  if (voiceEnd) voiceEnd.disabled = !voiceActive;
  if (input) input.disabled = !active;
}

function selectedLaptopLead() {
  return document.querySelector("#callmeLead").value || selectedLeadId || inboundFocusId || "energy-lead-001";
}

function startBrowserChat() {
  if (browserChatSocket && browserChatSocket.readyState === WebSocket.OPEN) return;
  const leadId = selectedLaptopLead();
  selectedLeadId = leadId;
  connectLive(leadId);
  const proto = location.protocol === "https:" ? "wss" : "ws";
  browserChatSocket = new WebSocket(`${proto}://${location.host}/api/browser-chat/${encodeURIComponent(leadId)}`);
  setBrowserChatState("Connecting", true);
  showError("");
  browserChatSocket.onopen = () => {
    setBrowserChatState("Laptop chat live", true);
    logOps(`Laptop customer chat started for ${leadId}`);
  };
  browserChatSocket.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    if (payload.kind === "session_started") {
      lastCallId = payload.call_id;
      if (payload.opening) {
        finishMessage({ role: "assistant", text: payload.opening, message_id: `browser-open-${payload.call_id}` });
      }
      loadAll().catch(() => {});
    } else if (payload.kind === "transcript" && payload.text) {
      finishMessage({ role: payload.role, text: payload.text, message_id: `browser-${Date.now()}-${payload.role}` });
    } else if (payload.kind === "error") {
      showError(payload.message || "Laptop chat failed");
      logOps(payload.message || "Laptop chat failed", "error");
    }
  };
  browserChatSocket.onerror = () => {
    showError("Laptop chat connection failed.");
    logOps("Laptop chat connection failed.", "error");
  };
  browserChatSocket.onclose = () => {
    setBrowserChatState("Phone or laptop", false);
    browserChatSocket = null;
    loadAll().catch(() => {});
  };
}

function endBrowserChat() {
  if (!browserChatSocket) return;
  if (browserChatSocket.readyState === WebSocket.OPEN) {
    browserChatSocket.send(JSON.stringify({ type: "end" }));
  }
  browserChatSocket.close();
}

function sendBrowserCustomerText(text) {
  const value = String(text || "").trim();
  if (!value) return;
  if (!browserChatSocket || browserChatSocket.readyState !== WebSocket.OPEN) {
    showError("Start laptop chat first.");
    return;
  }
  browserChatSocket.send(JSON.stringify({ type: "message", text: value }));
}

function downsampleAudio(input, inputRate, outputRate = 24000) {
  if (inputRate === outputRate) return input;
  const ratio = inputRate / outputRate;
  const outputLength = Math.max(1, Math.round(input.length / ratio));
  const output = new Float32Array(outputLength);
  for (let i = 0; i < outputLength; i += 1) {
    const start = Math.floor(i * ratio);
    const end = Math.min(input.length, Math.floor((i + 1) * ratio));
    let sum = 0;
    let count = 0;
    for (let j = start; j < end; j += 1) {
      sum += input[j];
      count += 1;
    }
    output[i] = count ? sum / count : input[start] || 0;
  }
  return output;
}

function floatToPcm16(input) {
  const buffer = new ArrayBuffer(input.length * 2);
  const view = new DataView(buffer);
  for (let i = 0; i < input.length; i += 1) {
    const sample = Math.max(-1, Math.min(1, input[i]));
    view.setInt16(i * 2, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
  }
  return buffer;
}

async function playAgentPcm(arrayBuffer) {
  if (!browserVoiceContext) {
    browserVoiceContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 });
  }
  await browserVoiceContext.resume();
  const input = new Int16Array(arrayBuffer);
  if (!input.length) return;
  const audioBuffer = browserVoiceContext.createBuffer(1, input.length, 24000);
  const channel = audioBuffer.getChannelData(0);
  for (let i = 0; i < input.length; i += 1) {
    channel[i] = input[i] / 32768;
  }
  const source = browserVoiceContext.createBufferSource();
  source.buffer = audioBuffer;
  source.connect(browserVoiceContext.destination);
  const now = browserVoiceContext.currentTime;
  browserVoicePlaybackTime = Math.max(browserVoicePlaybackTime, now + 0.02);
  source.start(browserVoicePlaybackTime);
  browserVoicePlaybackTime += audioBuffer.duration;
}

function stopBrowserVoice() {
  if (browserVoiceSocket && browserVoiceSocket.readyState === WebSocket.OPEN) {
    browserVoiceSocket.send(JSON.stringify({ type: "end" }));
    browserVoiceSocket.close();
  }
  if (browserVoiceProcessor) browserVoiceProcessor.disconnect();
  if (browserVoiceSource) browserVoiceSource.disconnect();
  if (browserVoiceSilentSink) browserVoiceSilentSink.disconnect();
  if (browserVoiceStream) browserVoiceStream.getTracks().forEach((track) => track.stop());
  browserVoiceSocket = null;
  browserVoiceStream = null;
  browserVoiceSource = null;
  browserVoiceProcessor = null;
  browserVoiceSilentSink = null;
  setBrowserChatState("Phone or laptop", false);
  loadAll().catch(() => {});
}

async function startBrowserVoice() {
  if (browserVoiceSocket && browserVoiceSocket.readyState === WebSocket.OPEN) return;
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    showError("Microphone access is not available in this browser.");
    return;
  }
  const leadId = selectedLaptopLead();
  selectedLeadId = leadId;
  connectLive(leadId);
  showError("");
  browserVoiceContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 });
  await browserVoiceContext.resume();
  browserVoiceStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
      channelCount: 1,
    },
  });

  const proto = location.protocol === "https:" ? "wss" : "ws";
  browserVoiceSocket = new WebSocket(`${proto}://${location.host}/api/browser-voice/${encodeURIComponent(leadId)}`);
  browserVoiceSocket.binaryType = "arraybuffer";
  setBrowserChatState("Connecting voice", true);

  browserVoiceSocket.onopen = () => {
    setBrowserChatState("Voice call live", true);
    logOps(`Laptop voice call started for ${leadId}`);
    browserVoiceSource = browserVoiceContext.createMediaStreamSource(browserVoiceStream);
    browserVoiceProcessor = browserVoiceContext.createScriptProcessor(4096, 1, 1);
    browserVoiceSilentSink = browserVoiceContext.createGain();
    browserVoiceSilentSink.gain.value = 0;
    browserVoiceProcessor.onaudioprocess = (event) => {
      if (!browserVoiceSocket || browserVoiceSocket.readyState !== WebSocket.OPEN) return;
      const input = event.inputBuffer.getChannelData(0);
      const pcm = floatToPcm16(downsampleAudio(input, browserVoiceContext.sampleRate, 24000));
      browserVoiceSocket.send(pcm);
    };
    browserVoiceSource.connect(browserVoiceProcessor);
    browserVoiceProcessor.connect(browserVoiceSilentSink);
    browserVoiceSilentSink.connect(browserVoiceContext.destination);
  };

  browserVoiceSocket.onmessage = (event) => {
    if (typeof event.data === "string") {
      const payload = JSON.parse(event.data);
      if (payload.kind === "session_started") {
        lastCallId = payload.call_id;
        loadAll().catch(() => {});
      } else if (payload.kind === "transcript" && payload.text) {
        finishMessage({ role: payload.role, text: payload.text, message_id: `voice-${payload.call_id}-${payload.role}-${Date.now()}` });
      } else if (payload.kind === "error") {
        showError(payload.message || "Voice call failed");
        logOps(payload.message || "Voice call failed", "error");
      }
      return;
    }
    playAgentPcm(event.data).catch((error) => logOps(error.message || "Agent audio playback failed", "error"));
  };

  browserVoiceSocket.onerror = () => {
    showError("Voice call connection failed.");
    logOps("Voice call connection failed.", "error");
  };
  browserVoiceSocket.onclose = () => stopBrowserVoice();
}

function startBrowserMic() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    showError("Speech recognition is not available in this browser. Type your customer message instead.");
    return;
  }
  if (!browserChatSocket || browserChatSocket.readyState !== WebSocket.OPEN) {
    startBrowserChat();
  }
  browserRecognition = new SpeechRecognition();
  browserRecognition.lang = "en-AU";
  browserRecognition.interimResults = false;
  browserRecognition.maxAlternatives = 1;
  browserRecognition.onresult = (event) => {
    const text = event.results && event.results[0] && event.results[0][0] ? event.results[0][0].transcript : "";
    document.querySelector("#browserCustomerText").value = text;
    sendBrowserCustomerText(text);
  };
  browserRecognition.onerror = (event) => showError(event.error || "Microphone recognition failed.");
  browserRecognition.start();
}

async function uploadScript(event) {
  event.preventDefault();
  const file = document.querySelector("#scriptFile").files[0];
  if (!file) return;
  const leadId = document.querySelector("#scriptLead").value;
  const body = new FormData();
  body.append("file", file);
  const response = await fetch(`/api/scripts/upload?lead_id=${encodeURIComponent(leadId)}`, { method: "POST", body });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "Upload failed");
  await loadAll();
}

async function uploadLeadData(event) {
  event.preventDefault();
  const file = document.querySelector("#leadImportFile").files[0];
  if (!file) return;
  const body = new FormData();
  body.append("file", file);
  const response = await fetch("/api/leads/import", { method: "POST", body });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || payload.message || "Lead import failed");
  logOps(payload.message || "Lead data imported");
  await loadAll();
}

function downloadReport(leadId) {
  const id = leadId || selectedLeadId || selectedLaptopLead();
  if (!id) {
    showError("Select a record first.");
    return;
  }
  const link = document.createElement("a");
  link.href = `/api/reports/${encodeURIComponent(id)}`;
  link.download = `cimenergy-${id}-executive-report.pdf`;
  document.body.appendChild(link);
  link.click();
  link.remove();
}

function renderScripts(scripts) {
  document.querySelector("#scriptList").innerHTML = scripts.length
    ? scripts
        .map((item) => `<article><strong>${item.filename}</strong><div class="muted">${item.size_bytes} bytes</div></article>`)
        .join("")
    : `<p class="muted">Empty</p>`;
}

function renderInbound(paths) {
  applyDid(paths);
  const number = deskDid;
  const voice = (paths.console && paths.console.url) || paths.inbound_voice_url;
  const selected = detail && detail.lead ? detail.lead.lead_id : inboundFocusId || selectedLeadId;
  document.querySelector("#inboundFacts").innerHTML = `
    <dt>Number to dial</dt><dd>${number}</dd>
    <dt>Voice webhook</dt><dd>${voice}</dd>
    <dt>Live</dt><dd>All inbound calls (hub *)</dd>
    <dt>Selected record</dt><dd>${selected || "None"}</dd>
  `;
  renderCallRecordings(detail);
}

function renderDrafts(drafts) {
  document.querySelector("#draftList").innerHTML = drafts.length
    ? drafts
        .map((item) => `<article>
          <strong>${item.status}</strong>  ${item.lead_id}
          <div class="muted">${item.safe_summary || ""}</div>
          ${item.status === "draft" ? `<button data-approve="${item.draft_id}">Approve</button> <button class="ghost" data-deny="${item.draft_id}">Deny</button>` : ""}
        </article>`)
        .join("")
    : `<p class="muted">Empty</p>`;
  document.querySelectorAll("[data-approve]").forEach((button) =>
    button.addEventListener("click", async () => {
      await api(`/api/drafts/${button.dataset.approve}/approve`, { method: "POST" });
      await loadAll();
    })
  );
  document.querySelectorAll("[data-deny]").forEach((button) =>
    button.addEventListener("click", async () => {
      await api(`/api/drafts/${button.dataset.deny}/deny`, { method: "POST" });
      await loadAll();
    })
  );
}

document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
document.querySelector("#refreshButton").addEventListener("click", loadAll);
document.querySelector("#startCallButton").addEventListener("click", startCall);
document.querySelector("#submitButton").addEventListener("click", async () => {
  if (!selectedLeadId) return;
  const result = await api(`/api/journeys/${selectedLeadId}/submit`, { method: "POST" });
  showScript(result.message);
  switchView("drafts");
  await loadAll();
});
document.querySelector("#detailReportButton").addEventListener("click", () => downloadReport(selectedLeadId));
document.querySelectorAll("[data-event]").forEach((button) => button.addEventListener("click", () => sendEvent(button.dataset.event)));
document.querySelector("#fieldForm").addEventListener("submit", (event) => {
  event.preventDefault();
  const value = document.querySelector("#fieldValue").value.trim();
  if (!value) return;
  sendEvent("field_capture", { [document.querySelector("#fieldName").value]: value });
});
document.querySelector("#labStart").addEventListener("click", startLab);
document.querySelector("#labTurn").addEventListener("click", nextLabTurn);
document.querySelector("#labAuto").addEventListener("click", () => runAutonomous().catch((error) => alert(error.message)));
document.querySelector("#placeTwilioCall").addEventListener("click", () => placeTwilioCall().catch((error) => alert(error.message)));
document.querySelector("#syncTwilio").addEventListener("click", () => {
  api("/api/ops/configure-twilio", { method: "POST" })
    .then(() => loadAll())
    .catch((error) => alert(error.message));
});
document.querySelector("#browserChatStart").addEventListener("click", startBrowserChat);
document.querySelector("#browserChatEnd").addEventListener("click", endBrowserChat);
document.querySelector("#browserVoiceStart").addEventListener("click", () => startBrowserVoice().catch((error) => {
  showError(error.message);
  logOps(error.message, "error");
  stopBrowserVoice();
}));
document.querySelector("#browserVoiceEnd").addEventListener("click", stopBrowserVoice);
document.querySelector("#browserMic").addEventListener("click", startBrowserMic);
document.querySelector("#browserChatForm").addEventListener("submit", (event) => {
  event.preventDefault();
  const input = document.querySelector("#browserCustomerText");
  sendBrowserCustomerText(input.value);
  input.value = "";
});
document.querySelector("#inboundLead").addEventListener("change", (event) => {
  inboundFocusId = event.target.value;
  loadDetail(event.target.value)
    .then(() => switchView("inbound"))
    .catch((error) => {
      showError(error.message);
      logOps(error.message, "error");
    });
});
document.querySelector("#callmeApprove").addEventListener("click", () => {
  if (!callMeDraftId) return;
  api(`/api/drafts/${callMeDraftId}/approve`, { method: "POST" })
    .then((result) => {
      logOps(result.message || "Form sent");
      return loadAll();
    })
    .catch((error) => {
      showError(error.message);
      logOps(error.message, "error");
    });
});
document.querySelector("#callmeReport").addEventListener("click", () => downloadReport(selectedLaptopLead()));
document.querySelector("#scriptForm").addEventListener("submit", (event) => uploadScript(event).catch((error) => alert(error.message)));
document.querySelector("#leadImportForm").addEventListener("submit", (event) => uploadLeadData(event).catch((error) => alert(error.message)));

loadAll()
  .then(() => switchView("callme"))
  .catch((error) => {
  showError(error.message);
  logOps(error.message, "error");
  document.querySelector("#hooks").innerHTML = `<p class="muted">${error.message}</p>`;
  switchView("callme");
});
connectGlobalLive();

window.addEventListener("error", (event) => logOps(event.message || "Browser error", "error"));
window.addEventListener("unhandledrejection", (event) => logOps(String(event.reason || "Unhandled failure"), "error"));
