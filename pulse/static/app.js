const state = {
  config: null,
  authenticated: false,
  events: [],
  preferences: null,
  discovery: null,
  learning: null,
  audit: null,
  subscription: false,
  detail: null,
  serviceWorkerRegistration: null,
  serviceWorkerReady: null,
};

const app = document.querySelector("#app");
const toast = document.querySelector("#toast");
const connection = document.querySelector("#connection-status");

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) state.authenticated = false;
    throw new Error(payload.error || `request failed (${response.status})`);
  }
  return payload;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
}

function showToast(message, error = false) {
  toast.textContent = message;
  toast.className = `toast show${error ? " error" : ""}`;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => { toast.className = "toast"; }, 3600);
}

function formatDate(value) {
  if (!value) return "just now";
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }).format(new Date(value));
}

function topicLabel(topic) {
  return state.config?.topics?.find((item) => item.id === topic)?.label || topic;
}

function isStandalone() {
  return window.matchMedia?.("(display-mode: standalone)").matches || window.navigator.standalone === true;
}

function isIOS() {
  return /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
}

function urlBase64ToUint8Array(value) {
  const padding = "=".repeat((4 - value.length % 4) % 4);
  const base64 = (value + padding).replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(base64), (char) => char.charCodeAt(0));
}

async function registerServiceWorker() {
  if (!("serviceWorker" in navigator) || typeof navigator.serviceWorker.register !== "function") return null;
  try {
    state.serviceWorkerRegistration = await navigator.serviceWorker.register("/sw.js", { scope: "/" });
    return state.serviceWorkerRegistration;
  } catch (error) {
    console.warn("service worker unavailable", error);
    return null;
  }
}

async function getPushManager() {
  if (!("serviceWorker" in navigator) || !state.config?.vapid_public_key || !state.serviceWorkerReady) return null;
  try {
    const registration = state.serviceWorkerRegistration || await state.serviceWorkerReady;
    if (!registration?.pushManager) return null;
    return registration.pushManager;
  } catch (error) {
    console.warn("push manager unavailable", error);
    return null;
  }
}

async function checkSubscription() {
  const pushManager = await getPushManager();
  if (!pushManager || typeof pushManager.getSubscription !== "function") return false;
  try {
    const subscription = await pushManager.getSubscription();
    state.subscription = Boolean(subscription);
  } catch (error) {
    console.warn("push subscription unavailable", error);
    state.subscription = false;
  }
  return state.subscription;
}

async function enableAlerts() {
  if (!state.config?.push_configured) {
    showToast("Push is not configured on the server yet.", true);
    return;
  }
  if (isIOS() && !isStandalone()) {
    showToast("On iPhone, add Pulse to the Home Screen first.", true);
    return;
  }
  if (!("Notification" in window) || typeof Notification.requestPermission !== "function") {
    showToast("This browser does not support web push.", true);
    return;
  }
  try {
    const pushManager = await getPushManager();
    if (!pushManager || typeof pushManager.subscribe !== "function") {
      showToast(isIOS() ? "Open Pulse from the Home Screen app to enable alerts." : "This browser does not support web push.", true);
      return;
    }
    const permission = await Notification.requestPermission();
    if (permission !== "granted") {
      showToast("Notifications remain off.", true);
      return;
    }
    const subscription = await pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(state.config.vapid_public_key) });
    await api("/api/subscriptions", { method: "POST", body: JSON.stringify(subscription) });
    state.subscription = true;
    renderHome();
    showToast("alerts are on");
  } catch (error) {
    showToast(error.message, true);
  }
}

async function loadAuthenticatedState() {
  const [events, preferences, discovery, learning, audit] = await Promise.all([api("/api/events?limit=50"), api("/api/preferences"), api("/api/discovery"), api("/api/learning"), api("/api/audit?near_threshold=1&limit=30")]);
  state.events = events.events || [];
  state.preferences = preferences.preferences;
  state.discovery = discovery;
  state.learning = learning;
  state.audit = audit;
  await checkSubscription();
}

function loginView() {
  return `<section class="login-card">
    <div class="eyebrow">private signal layer</div>
    <h1>only the things worth interrupting you for.</h1>
    <p class="lede">Pulse watches the sources you choose, keeps the noise out, and gives every alert a place to land.</p>
    <form id="login-form" class="login-form">
      <label for="password">passphrase</label>
      <div class="input-row"><input id="password" name="password" type="password" autocomplete="current-password" minlength="8" required placeholder="8+ characters"><button type="submit">open Pulse</button></div>
      <p class="fine-print">personal access only · your source credentials stay on the server</p>
    </form>
  </section>`;
}

function alertCard() {
  if (state.subscription) {
    return `<section class="alert-card enabled"><div><span class="eyebrow">delivery</span><h2>alerts are on</h2><p>Pulse can reach this device when something crosses your rules.</p></div><button class="button secondary" data-action="test-push">send test</button></section>`;
  }
  const iosNote = isIOS() && !isStandalone() ? "On iPhone: share this page, choose Add to Home Screen, then open Pulse there." : "Turn this on from the device where you want Pulse to reach you.";
  return `<section class="alert-card"><div><span class="eyebrow">first connection</span><h2>let Pulse reach you</h2><p>${iosNote}</p></div><button class="button" data-action="enable-alerts">enable alerts</button></section>`;
}

function eventCard(event) {
  const confidence = event.metadata?.confidence || "direct";
  return `<button class="event-card" data-event-id="${escapeHtml(event.id)}">
    <div class="event-card-top"><span class="topic topic-${escapeHtml(event.topic)}">${escapeHtml(topicLabel(event.topic))}</span><span class="event-time">${escapeHtml(formatDate(event.published_at || event.discovered_at))}</span></div>
    <h3>${escapeHtml(event.title)}</h3>
    <p>${escapeHtml(event.summary || event.body || "Open for details")}</p>
    <div class="event-card-bottom"><span class="priority priority-${escapeHtml(event.priority)}">${escapeHtml(event.priority)}</span><span class="confidence confidence-${escapeHtml(confidence)}">${escapeHtml(confidence)}</span><span class="score">${event.score}/100</span></div>
  </button>`;
}

function rulesCard() {
  const prefs = state.preferences || { quiet_start: "23:00", quiet_end: "07:00", topic_thresholds: {} };
  const thresholdRows = Object.entries(prefs.topic_thresholds || {}).filter(([topic]) => topic !== "system").map(([topic, value]) => `<label class="threshold-row"><span>${escapeHtml(topicLabel(topic))}</span><input data-threshold-topic="${escapeHtml(topic)}" type="range" min="0" max="100" step="1" value="${Number(value)}"><output>${Number(value)}</output></label>`).join("");
  return `<details class="rules-card"><summary><span><span class="eyebrow">rules</span><strong>quiet hours and relevance</strong></span><span class="chevron">⌄</span></summary><div class="rules-body"><div class="time-row"><label>quiet from<input id="quiet-start" type="time" value="${escapeHtml(prefs.quiet_start)}"></label><label>until<input id="quiet-end" type="time" value="${escapeHtml(prefs.quiet_end)}"></label></div><div class="thresholds"><p class="field-note">notify only when a topic reaches its threshold</p>${thresholdRows}</div><button class="button secondary full" data-action="save-rules">save rules</button></div></details>`;
}

function passwordCard() {
  return `<details class="rules-card password-card"><summary><span><span class="eyebrow">security</span><strong>change passphrase</strong></span><span class="chevron">⌄</span></summary><div class="rules-body"><form id="password-form" class="password-form"><label for="current-password">current passphrase<input id="current-password" name="current_password" type="password" autocomplete="current-password" minlength="8" required></label><label for="new-password">new passphrase<input id="new-password" name="new_password" type="password" autocomplete="new-password" minlength="8" maxlength="256" required></label><label for="confirm-password">confirm new passphrase<input id="confirm-password" name="confirm_password" type="password" autocomplete="new-password" minlength="8" maxlength="256" required></label><button class="button secondary full" type="submit">change passphrase</button><p class="fine-print">minimum 8 characters · the old passphrase stops working after this succeeds</p></form></div></details>`;
}

function discoveryCard() {
  const discovery = state.discovery || { enabled: false, interval_minutes: 180, profiles: [], entities: [] };
  const profiles = escapeHtml(JSON.stringify(discovery.profiles || [], null, 2));
  const entities = escapeHtml(JSON.stringify(discovery.entities || [], null, 2));
  const status = discovery.enabled ? `broad search every ${discovery.interval_minutes} minutes` : "direct watchers only · add a Brave key on the server to enable search";
  return `<details class="rules-card discovery-card"><summary><span><span class="eyebrow">discovery</span><strong>search profiles and tracked things</strong></span><span class="chevron">⌄</span></summary><div class="rules-body"><p class="field-note">${escapeHtml(status)}. these settings are stored in Pulse and never include the search key.</p><label for="profiles-json">search profiles <textarea id="profiles-json" rows="12" spellcheck="false">${profiles}</textarea></label><label for="entities-json">tracked entities <textarea id="entities-json" rows="12" spellcheck="false">${entities}</textarea></label><button class="button secondary full" data-action="save-discovery">save discovery settings</button><p class="fine-print">advanced editor: profiles need id, label, topic, queries, keywords, active. entities need id, name, aliases, topic, boost.</p></div></details>`;
}

function learningCard() {
  const summary = state.learning?.summary || {};
  const metrics = state.learning?.metrics || {};
  const followed = Object.keys(state.learning?.followed_entities || {}).length;
  const lessLike = Object.keys(state.learning?.less_like_entities || {}).length + Object.keys(state.learning?.less_like_topics || {}).length;
  const average = metrics.average_time_to_open_seconds == null ? "—" : `${Math.round(metrics.average_time_to_open_seconds / 60)}m`;
  return `<details class="rules-card"><summary><span><span class="eyebrow">feedback</span><strong>what Pulse is learning</strong></span><span class="chevron">⌄</span></summary><div class="rules-body"><p class="field-note">useful, not useful, too late, follow, and less-like choices change scoring gradually. opening and source clicks are recorded as signals.</p><div class="learning-stats"><span><strong>${Number(summary.delivered || 0)}</strong> delivered</span><span><strong>${Number(summary.opened || 0)}</strong> opened</span><span><strong>${followed}</strong> followed</span><span><strong>${lessLike}</strong> less like</span></div><p class="fine-print">average time to open: ${average} · delivered but not opened: ${Number(metrics.delivered_not_opened || 0)}</p><button class="button ghost-button full" data-action="reset-learning">reset feedback history</button></div></details>`;
}

function auditCard() {
  const decisions = state.audit?.decisions || [];
  const suppressed = decisions.filter((item) => !item.allowed);
  const reasons = [...new Set(suppressed.map((item) => item.reason))].slice(0, 5).join(" · ") || "none recently";
  const rows = decisions.slice(0, 6).map((item) => `<a class="source-row" href="/event/${encodeURIComponent(item.event_id)}"><span>${escapeHtml(item.reason)}</span><span>${item.score}/100 · ${escapeHtml(item.event_id.slice(0, 10))}</span></a>`).join("");
  return `<details class="rules-card"><summary><span><span class="eyebrow">debug</span><strong>notification audit and simulator</strong></span><span class="chevron">⌄</span></summary><div class="rules-body"><p class="field-note">${decisions.length} near-threshold decisions recorded · suppressed reasons: ${escapeHtml(reasons)}</p><div class="source-list">${rows || "<p class='fine-print'>no near-threshold decisions yet</p>"}</div><form id="simulator-form" class="password-form"><label>existing event id<input id="simulator-event-id" placeholder="leave blank for a sample"></label><label>sample title<input id="simulator-title" placeholder="only used when event id is blank"></label><label>sample topic<input id="simulator-topic" value="watcher"></label><label>sample summary<textarea id="simulator-summary" rows="3" placeholder="what happened and why it might matter"></textarea></label><button class="button secondary full" type="submit">simulate without sending</button></form><pre id="simulation-result" class="debug-output">no simulation run</pre><p class="fine-print">simulation uses the live scoring, freshness, cooldown, trust, interest, and quiet-hour rules. it never sends a push.</p></div></details>`;
}

function detailQuality(event) {
  const metadata = event.metadata || {};
  const confidence = metadata.confidence || "direct";
  const sources = Array.isArray(metadata.sources) ? metadata.sources : [];
  const sourceLinks = sources.map((source) => `<a class="source-row" data-action="source-click" data-event-id="${escapeHtml(event.id)}" href="${escapeHtml(source.url || event.url)}" target="_blank" rel="noreferrer"><span>${escapeHtml(source.title || source.url || "source")}</span><span>${escapeHtml(source.trust || "source")}</span></a>`).join("");
  const tracked = (metadata.entity_names || []).filter(Boolean).join(", ");
  return `<div class="quality-card"><span class="eyebrow">evidence quality</span><div class="quality-row"><span class="confidence confidence-${escapeHtml(confidence)}">${escapeHtml(confidence)}</span><span class="muted">${sources.length || 1} source${sources.length === 1 ? "" : "s"}</span><span class="muted">${escapeHtml(metadata.verification || "direct watcher")}</span></div>${tracked ? `<p class="fine-print">tracked: ${escapeHtml(tracked)}</p>` : ""}${sourceLinks ? `<div class="source-list">${sourceLinks}</div>` : ""}</div>`;
}

function detailDiagnostics(event) {
  const trace = event.decision_trace || {};
  const components = trace.score_components || event.metadata?.score_components || {};
  const rows = Object.entries(components).map(([key, value]) => `<div class="diagnostic-row"><span>${escapeHtml(key)}</span><strong>${escapeHtml(value)}</strong></div>`).join("");
  const provenance = event.provenance || {};
  return `<div class="quality-card diagnostics"><span class="eyebrow">decision trace</span><div class="diagnostic-row"><span>canonical event</span><strong>${escapeHtml(event.canonical_event_id || "—")}</strong></div><div class="diagnostic-row"><span>story cluster</span><strong>${escapeHtml(event.cluster_id || "—")}</strong></div><div class="diagnostic-row"><span>development</span><strong>${escapeHtml(event.development_id || "—")}</strong></div><div class="diagnostic-row"><span>tier</span><strong>${escapeHtml(trace.notification_tier || event.priority || "—")}</strong></div><div class="diagnostic-row"><span>freshness</span><strong>${escapeHtml(trace.freshness?.state || "not evaluated")}</strong></div><div class="diagnostic-row"><span>cooldown</span><strong>${trace.cooldown?.active ? `active until ${escapeHtml(trace.cooldown.until || "later")}` : "clear"}</strong></div><div class="diagnostic-row"><span>source trust</span><strong>${escapeHtml(trace.source_trust || event.metadata?.source_trust || "—")} (${escapeHtml(trace.source_trust_contribution ?? "0")})</strong></div><div class="diagnostic-row"><span>personal interest</span><strong>${escapeHtml(trace.personalized_interest_contribution ?? components.personal_interest ?? "0")}</strong></div><div class="diagnostic-row"><span>local relevance</span><strong>${escapeHtml(trace.local_relevance || "none")}</strong></div><div class="diagnostic-row"><span>push state</span><strong>${trace.pushed ? "pushed" : escapeHtml(event.notification_reason || "not pushed")}</strong></div><div class="diagnostic-components">${rows}</div><details><summary>provenance</summary><pre class="debug-output">${escapeHtml(JSON.stringify(provenance, null, 2))}</pre></details></div>`;
}

function detailFeedback(event) {
  const metadata = event.metadata || {};
  const ids = metadata.entities || [];
  const names = metadata.entity_names || [];
  const followButtons = ids.map((id, index) => `<button class="button ghost-button" data-action="follow" data-event-id="${escapeHtml(event.id)}" data-entity-id="${escapeHtml(id)}">follow ${escapeHtml(names[index] || id)}</button>`).join("");
  return `<div class="feedback-row"><button class="button ghost-button" data-action="feedback" data-feedback="useful" data-event-id="${escapeHtml(event.id)}">useful</button><button class="button ghost-button" data-action="feedback" data-feedback="not_useful" data-event-id="${escapeHtml(event.id)}">not useful</button><button class="button ghost-button" data-action="feedback" data-feedback="too_late" data-event-id="${escapeHtml(event.id)}">too late</button>${followButtons}<button class="button ghost-button" data-action="less-like" data-event-id="${escapeHtml(event.id)}">less like this</button></div>`;
}

function renderHome() {
  document.title = "Pulse · quiet signals";
  const events = state.events.length ? state.events.map(eventCard).join("") : `<div class="empty-state"><span class="empty-mark">·</span><h2>nothing worth interrupting you for</h2><p>That is the point. New events will appear here when they matter.</p></div>`;
  app.innerHTML = `<section class="page-heading"><div><div class="eyebrow">aiden · personal feed</div><h1>your pulse</h1></div><button class="icon-button" data-action="logout" aria-label="Log out">↗</button></section>${alertCard()}<section class="feed-head"><div><span class="eyebrow">recent signals</span><h2>history</h2></div><span class="muted">${state.events.length} saved</span></section><section class="event-list">${events}</section>${rulesCard()}${discoveryCard()}${learningCard()}${auditCard()}${passwordCard()}<p class="footer-note">Pulse is quiet by default. source credentials never leave the server.</p>`;
}

async function renderDetail(eventId) {
  const cached = state.events.find((item) => item.id === eventId);
  app.innerHTML = `<section class="loading-state"><span class="loader"></span><p>opening signal</p></section>`;
  try {
    const response = await api(`/api/events/${encodeURIComponent(eventId)}`);
    state.detail = response.event || cached;
    const event = state.detail;
    if (!event) throw new Error("event not found");
    await api(`/api/events/${encodeURIComponent(event.id)}/opened`, { method: "POST", body: "{}" }).catch(() => {});
    document.title = `${event.title} · Pulse`;
    app.innerHTML = `<section class="detail-page"><button class="back-button" data-action="back">← history</button><div class="detail-kicker"><span class="topic topic-${escapeHtml(event.topic)}">${escapeHtml(topicLabel(event.topic))}</span><span>${escapeHtml(formatDate(event.published_at || event.discovered_at))}</span></div><h1>${escapeHtml(event.title)}</h1><p class="detail-summary">${escapeHtml(event.summary || event.body || "")}</p><div class="why-card"><span class="eyebrow">why Pulse surfaced this</span><p>${escapeHtml(event.body || "matched your relevance rules")}</p><div class="signal-meter"><span style="width:${Math.max(4, Math.min(100, event.score))}%"></span></div><div class="meter-label"><span>${escapeHtml(event.priority)} priority</span><span>${event.score}/100</span></div></div>${detailQuality(event)}${detailDiagnostics(event)}<div class="detail-actions"><a class="button" data-action="source-click" data-event-id="${escapeHtml(event.id)}" href="${escapeHtml(event.url)}" target="_blank" rel="noreferrer">open source</a><button class="button secondary" data-action="remind" data-event-id="${escapeHtml(event.id)}">remind me</button><button class="button ghost-button" data-action="mute" data-topic="${escapeHtml(event.topic)}">mute topic</button></div>${detailFeedback(event)}<p class="fine-print">event id ${escapeHtml(event.id)}</p></section>`;
  } catch (error) {
    app.innerHTML = `<section class="empty-state"><h2>signal unavailable</h2><p>${escapeHtml(error.message)}</p><button class="button" data-action="back">back to history</button></section>`;
  }
}

function render() {
  if (!state.authenticated) {
    app.innerHTML = loginView();
    return;
  }
  if (location.pathname.startsWith("/event/")) {
    renderDetail(decodeURIComponent(location.pathname.slice("/event/".length)));
  } else {
    renderHome();
  }
}

async function bootstrap() {
  try {
    state.config = await api("/api/config");
    const auth = await api("/api/auth/status");
    state.authenticated = auth.authenticated;
    connection.innerHTML = `<span class="status-dot ${state.authenticated ? "active" : "muted-dot"}"></span><span>${state.authenticated ? "watching" : "locked"}</span>`;
    if (state.authenticated) await loadAuthenticatedState();
    render();
  } catch (error) {
    connection.innerHTML = `<span class="status-dot muted-dot"></span><span>offline</span>`;
    app.innerHTML = `<section class="empty-state"><h2>Pulse cannot connect</h2><p>${escapeHtml(error.message)}</p><button class="button" data-action="reload">try again</button></section>`;
  }
}

document.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (event.target.id === "simulator-form") {
    const eventId = document.querySelector("#simulator-event-id").value.trim();
    const payload = eventId ? { event_id: eventId } : { topic: document.querySelector("#simulator-topic").value.trim(), title: document.querySelector("#simulator-title").value.trim() || "sample event", summary: document.querySelector("#simulator-summary").value.trim() };
    try {
      const result = await api("/api/debug/simulate", { method: "POST", body: JSON.stringify(payload) });
      document.querySelector("#simulation-result").textContent = JSON.stringify({ allowed: result.allowed, reason: result.reason, trace: result.trace }, null, 2);
    } catch (error) {
      document.querySelector("#simulation-result").textContent = error.message;
    }
    return;
  }
  if (event.target.id === "password-form") {
    const form = event.target;
    const values = Object.fromEntries(new FormData(form).entries());
    try {
      await api("/api/auth/password", { method: "POST", body: JSON.stringify(values) });
      form.reset();
      showToast("passphrase changed");
    } catch (error) {
      const message = { invalid_current_password: "current passphrase is incorrect", password_confirmation_mismatch: "the new passphrases do not match", password_too_short: "use at least 8 characters", password_too_long: "use 256 characters or fewer", password_unchanged: "choose a different passphrase" }[error.message] || error.message;
      showToast(message, true);
    }
    return;
  }
  if (event.target.id !== "login-form") return;
  const password = new FormData(event.target).get("password");
  try {
    await api("/api/auth/login", { method: "POST", body: JSON.stringify({ password }) });
    state.authenticated = true;
    await loadAuthenticatedState();
    render();
  } catch (error) {
    showToast("that passphrase did not work", true);
  }
});

document.addEventListener("click", async (event) => {
  const card = event.target.closest("[data-event-id]");
  if (card && card.classList.contains("event-card")) {
    history.pushState({}, "", `/event/${encodeURIComponent(card.dataset.eventId)}`);
    render();
    return;
  }
  const action = event.target.closest("[data-action]")?.dataset.action;
  if (!action) return;
  try {
    if (action === "enable-alerts") await enableAlerts();
    if (action === "test-push") { const result = await api("/api/push/test", { method: "POST", body: "{}" }); showToast(result.delivery?.sent ? "test sent" : "test event saved; delivery is not configured"); }
    if (action === "source-click") { void api(`/api/events/${encodeURIComponent(event.target.closest("[data-event-id]")?.dataset.eventId || state.detail?.id)}/source-clicked`, { method: "POST", body: "{}" }); return; }
    if (action === "logout") { await api("/api/auth/logout", { method: "POST", body: "{}" }); state.authenticated = false; render(); }
    if (action === "back") { history.pushState({}, "", "/"); await loadAuthenticatedState(); render(); }
    if (action === "reload") window.location.reload();
    if (action === "remind") { await api(`/api/events/${encodeURIComponent(event.target.closest("[data-event-id]")?.dataset.eventId || state.detail.id)}/remind`, { method: "POST", body: JSON.stringify({ minutes: 60 }) }); showToast("reminder set for one hour"); }
    if (action === "mute") { await api(`/api/topics/${encodeURIComponent(event.target.closest("[data-topic]")?.dataset.topic || state.detail.topic)}/mute`, { method: "POST", body: JSON.stringify({ days: 7 }) }); showToast("topic muted for seven days"); }
    if (action === "follow" || action === "less-like") {
      const eventId = event.target.closest("[data-event-id]")?.dataset.eventId || state.detail.id;
      const entityId = event.target.closest("[data-entity-id]")?.dataset.entityId;
      await api(`/api/events/${encodeURIComponent(eventId)}/feedback`, { method: "POST", body: JSON.stringify({ action: action === "follow" ? "follow" : "less_like", ...(entityId ? { entity_id: entityId } : {}) }) });
      state.learning = await api("/api/learning");
      showToast(action === "follow" ? "interest followed" : "we will show less like this");
    }
    if (action === "feedback") {
      const eventId = event.target.closest("[data-event-id]")?.dataset.eventId || state.detail.id;
      const feedback = event.target.closest("[data-feedback]")?.dataset.feedback;
      await api(`/api/events/${encodeURIComponent(eventId)}/feedback`, { method: "POST", body: JSON.stringify({ action: feedback }) });
      state.learning = await api("/api/learning");
      showToast(feedback === "useful" ? "saved as useful" : feedback === "too_late" ? "saved as too late" : "saved as not useful");
    }
    if (action === "save-rules") {
      const thresholds = Object.fromEntries([...document.querySelectorAll("[data-threshold-topic]")].map((input) => [input.dataset.thresholdTopic, Number(input.value)]));
      const result = await api("/api/preferences", { method: "PUT", body: JSON.stringify({ quiet_start: document.querySelector("#quiet-start").value, quiet_end: document.querySelector("#quiet-end").value, topic_thresholds: thresholds }) });
      state.preferences = result.preferences; showToast("rules saved");
    }
    if (action === "save-discovery") {
      let profiles;
      let entities;
      try {
        profiles = JSON.parse(document.querySelector("#profiles-json").value);
        entities = JSON.parse(document.querySelector("#entities-json").value);
      } catch (_error) {
        showToast("discovery settings must be valid JSON", true);
        return;
      }
      const result = await api("/api/discovery", { method: "PUT", body: JSON.stringify({ profiles, entities }) });
      state.discovery = { ...state.discovery, ...result };
      showToast("discovery settings saved");
    }
    if (action === "reset-learning") {
      await api("/api/learning", { method: "DELETE", body: "{}" });
      state.learning = await api("/api/learning");
      showToast("feedback history reset");
    }
  } catch (error) {
    showToast(error.message, true);
  }
});

document.addEventListener("input", (event) => {
  if (event.target.matches("[data-threshold-topic]")) event.target.nextElementSibling.value = event.target.value;
});

window.addEventListener("popstate", render);
state.serviceWorkerReady = registerServiceWorker();
bootstrap();
