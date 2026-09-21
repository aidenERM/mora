const state = {
  config: null,
  authenticated: false,
  events: [],
  preferences: null,
  discovery: null,
  learning: null,
  audit: null,
  integrations: [],
  context: [],
  devices: [],
  subscription: false,
  detail: null,
  serviceWorkerRegistration: null,
  serviceWorkerReady: null,
  gameEvents: [],
  showAllHistory: false,
  activeTopic: "all",
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

function topicGlyph(topic) {
  return { weather: "☼", earthquake: "≈", warzone: "✦", rocket_league: "◎", unstable_smp: "◇", apple: "⌘", ios: "◐", openai: "✳", instagram: "◎", discord: "◈", github: "⌁", package: "▣", purchase: "$", security: "!", colombia: "•", service_status: "↯" }[topic] || "·";
}

function greeting() {
  const hour = new Date().getHours();
  return hour < 12 ? "good morning" : hour < 18 ? "good afternoon" : "good evening";
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
  const [events, preferences, discovery, learning, audit, integrations, gameEvents] = await Promise.all([api("/api/events?limit=50"), api("/api/preferences"), api("/api/discovery"), api("/api/learning"), api("/api/audit?near_threshold=1&suppressed_only=1&limit=30"), api("/api/integrations"), api("/api/game-events")]);
  state.events = events.events || [];
  state.preferences = preferences.preferences;
  state.discovery = discovery;
  state.learning = learning;
  state.audit = audit;
  state.integrations = integrations.integrations || [];
  state.context = integrations.context || [];
  state.devices = integrations.devices || [];
  state.gameEvents = gameEvents.events || [];
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
  return `<button class="event-card" data-event-id="${escapeHtml(event.id)}"><span class="event-glyph topic-${escapeHtml(event.topic)}">${topicGlyph(event.topic)}</span><span class="event-card-copy"><span class="event-card-top"><span class="topic topic-${escapeHtml(event.topic)}">${escapeHtml(topicLabel(event.topic))}</span><span class="event-time">${escapeHtml(formatDate(event.published_at || event.discovered_at))}</span></span><h3>${escapeHtml(event.title)}</h3><p>${escapeHtml(event.body || event.summary || "Open for details")}</p><span class="event-card-bottom"><span class="priority priority-${escapeHtml(event.priority)}">${escapeHtml(event.priority)}</span><span class="confidence confidence-${escapeHtml(confidence)}">${escapeHtml(confidence)}</span></span></span></button>`;
}

function rulesCard() {
  const prefs = state.preferences || { quiet_start: "23:00", quiet_end: "07:00", topic_thresholds: {} };
  const thresholdRows = Object.entries(prefs.topic_thresholds || {}).filter(([topic]) => topic !== "system").map(([topic, value]) => `<label class="threshold-row"><span>${escapeHtml(topicLabel(topic))}</span><input data-threshold-topic="${escapeHtml(topic)}" type="range" min="0" max="100" step="1" value="${Number(value)}"><output>${Number(value)}</output></label>`).join("");
  const priorityTopics = ["apple", "ios", "openai", "warzone", "rocket_league", "unstable_smp", "minecraft", "roblox", "github", "package", "purchase", "weather", "school", "security", "service_status", "travel", "discord", "instagram"];
  const priorityRows = priorityTopics.map((topic) => `<label class="threshold-row"><span>${escapeHtml(topicLabel(topic))}</span><input data-priority-topic="${topic}" type="range" min="-20" max="20" step="1" value="${Number(prefs.personal_priorities?.[topic] || 0)}"><output>${Number(prefs.personal_priorities?.[topic] || 0)}</output></label>`).join("");
  const temp = prefs.temporary_priority || {};
  const activeTemp = Object.entries(temp).find(([, value]) => value?.expires_at && new Date(value.expires_at) > new Date());
  const gameEvents = (state.gameEvents || []).map((item) => `<div class="source-row"><span><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.game)} · ${escapeHtml(item.starts_at)}</small></span><button class="button ghost-button" data-action="delete-game-event" data-game-event-id="${escapeHtml(item.id)}">remove</button></div>`).join("") || `<p class="fine-print">no countdowns configured</p>`;
  return `<details class="rules-card"><summary><span><span class="eyebrow">rules</span><strong>quiet hours and relevance</strong></span><span class="chevron">⌄</span></summary><div class="rules-body"><div class="time-row"><label>quiet from<input id="quiet-start" type="time" value="${escapeHtml(prefs.quiet_start)}"></label><label>until<input id="quiet-end" type="time" value="${escapeHtml(prefs.quiet_end)}"></label></div><div class="thresholds"><p class="field-note">notify only when a topic reaches its threshold</p>${thresholdRows}</div><div class="thresholds"><p class="field-note">personal priority, from less to more important</p>${priorityRows}</div><div class="time-row"><label>temporary topic<select id="temporary-topic"><option value="">none</option>${priorityTopics.map((topic) => `<option value="${topic}" ${activeTemp?.[0] === topic ? "selected" : ""}>${escapeHtml(topicLabel(topic))}</option>`).join("")}</select></label><label>boost<input id="temporary-boost" type="number" min="-20" max="20" value="${Number(activeTemp?.[1]?.boost || 10)}"></label><label>hours<input id="temporary-hours" type="number" min="1" max="336" value="24"></label></div><button class="button secondary full" data-action="save-rules">save rules</button><div class="eyebrow section-eyebrow">gaming countdowns</div><form id="game-event-form" class="password-form"><label>game<input id="game-event-game" placeholder="Warzone" required></label><label>event<input id="game-event-title" placeholder="season update" required></label><label>starts at<input id="game-event-starts" type="datetime-local" required></label><button class="button ghost-button full" type="submit">add countdown</button></form><div class="source-list">${gameEvents}</div></div></details>`;
}

function passwordCard() {
  return `<details class="rules-card password-card"><summary><span><span class="eyebrow">security</span><strong>change passphrase</strong></span><span class="chevron">⌄</span></summary><div class="rules-body"><form id="password-form" class="password-form"><label for="current-password">current passphrase<input id="current-password" name="current_password" type="password" autocomplete="current-password" minlength="8" required></label><label for="new-password">new passphrase<input id="new-password" name="new_password" type="password" autocomplete="new-password" minlength="8" maxlength="256" required></label><label for="confirm-password">confirm new passphrase<input id="confirm-password" name="confirm_password" type="password" autocomplete="new-password" minlength="8" maxlength="256" required></label><button class="button secondary full" type="submit">change passphrase</button><p class="fine-print">minimum 8 characters · the old passphrase stops working after this succeeds</p></form></div></details>`;
}

function discoveryCard() {
  const discovery = state.discovery || { enabled: false, interval_minutes: 180, profiles: [], entities: [] };
  const profiles = escapeHtml(JSON.stringify(discovery.profiles || [], null, 2));
  const entities = escapeHtml(JSON.stringify(discovery.entities || [], null, 2));
  const status = discovery.enabled ? `${discovery.provider === "google_news_rss" ? "public RSS discovery" : "broad search"} every ${discovery.interval_minutes} minutes` : "direct watchers only";
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
  const rows = decisions.slice(0, 8).map((item) => `<a class="source-row" href="/event/${encodeURIComponent(item.event_id)}"><span><strong>${escapeHtml(item.title || item.event_id)}</strong><small>${escapeHtml(item.topic || "event")} · ${escapeHtml(item.reason)}</small></span><span>${item.score}/100 · ${item.distance_from_threshold >= 0 ? "+" : ""}${item.distance_from_threshold}${item.later_became_important ? " · later mattered" : ""}</span></a>`).join("");
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
  const frequency = trace.rolling_frequency || {};
  const cooling = trace.topic_cooling || {};
  const quiet = trace.quiet_hours || {};
  const finalScore = trace.effective_score ?? event.score;
  return `<div class="quality-card diagnostics"><span class="eyebrow">decision trace</span><div class="diagnostic-row"><span>final relevance</span><strong>${escapeHtml(finalScore)}/100 · threshold ${escapeHtml(trace.threshold ?? "—")} · distance ${escapeHtml(trace.distance_from_threshold ?? "—")}</strong></div><div class="diagnostic-row"><span>canonical event</span><strong>${escapeHtml(event.canonical_event_id || trace.canonical_event_id || "—")}</strong></div><div class="diagnostic-row"><span>story cluster</span><strong>${escapeHtml(event.cluster_id || trace.cluster_id || "—")}</strong></div><div class="diagnostic-row"><span>development</span><strong>${escapeHtml(event.development_id || trace.development_id || "—")}</strong></div><div class="diagnostic-row"><span>topic / tier</span><strong>${escapeHtml(trace.topic || event.topic || "—")} · ${escapeHtml(trace.notification_tier || event.priority || "—")}</strong></div><div class="diagnostic-row"><span>freshness</span><strong>${escapeHtml(trace.freshness?.state || "not evaluated")} · decay ${escapeHtml(components.freshness_decay ?? "0")}</strong></div><div class="diagnostic-row"><span>source trust</span><strong>${escapeHtml(trace.source_trust || event.metadata?.source_trust || "—")} · ${escapeHtml(trace.source_trust_contribution ?? "0")}</strong></div><div class="diagnostic-row"><span>interest / local / domain</span><strong>${escapeHtml(trace.personalized_interest_contribution ?? components.personal_interest ?? "0")} / ${escapeHtml(trace.local_relevance_contribution ?? components.local_relevance ?? "0")} / ${escapeHtml(trace.domain_contribution ?? components.domain ?? "0")}</strong></div><div class="diagnostic-row"><span>cooldown</span><strong>${cooldown.overridden_for_development ? "overridden for meaningful development" : cooldown.active ? `active until ${escapeHtml(cooldown.until || "later")}` : "clear"}</strong></div><div class="diagnostic-row"><span>rolling ceiling</span><strong>${frequency.bypassed_for_safety ? "bypassed for safety" : frequency.active ? `blocked by ${escapeHtml((frequency.blocked_by || []).join(", "))}` : "clear"}</strong></div><div class="diagnostic-row"><span>topic cooling</span><strong>${cooling.active ? `+${escapeHtml(cooling.threshold_bonus)} threshold` : "clear"}</strong></div><div class="diagnostic-row"><span>quiet hours</span><strong>${quiet.affected ? "suppressed" : quiet.bypassed ? `bypassed · ${escapeHtml(quiet.reason)}` : "not affected"}</strong></div><div class="diagnostic-row"><span>push decision</span><strong>${trace.pushed ? "pushed" : escapeHtml(event.notification_reason || "not pushed")} · near threshold ${trace.near_threshold ? "yes" : "no"}</strong></div><div class="diagnostic-row"><span>later outcome</span><strong>${trace.later_became_important ? `later mattered${trace.later_development_notified ? " · later development notified" : ""}` : "not observed"}</strong></div><div class="diagnostic-components">${rows}</div><details><summary>provenance</summary><pre class="debug-output">${escapeHtml(JSON.stringify(provenance, null, 2))}</pre></details></div>`;
}

function detailFeedback(event) {
  const metadata = event.metadata || {};
  const ids = metadata.entities || [];
  const names = metadata.entity_names || [];
  const followButtons = ids.map((id, index) => `<button class="button ghost-button" data-action="follow" data-event-id="${escapeHtml(event.id)}" data-entity-id="${escapeHtml(id)}">follow ${escapeHtml(names[index] || id)}</button>`).join("");
  const storyButton = event.canonical_event_id || event.cluster_id ? `<button class="button ghost-button" data-action="follow-story" data-event-id="${escapeHtml(event.id)}">follow this story</button>` : "";
  return `<div class="feedback-row"><button class="button ghost-button" data-action="feedback" data-feedback="useful" data-event-id="${escapeHtml(event.id)}">useful</button><button class="button ghost-button" data-action="feedback" data-feedback="not_useful" data-event-id="${escapeHtml(event.id)}">not useful</button><button class="button ghost-button" data-action="feedback" data-feedback="too_late" data-event-id="${escapeHtml(event.id)}">too late</button>${storyButton}${followButtons}<button class="button ghost-button" data-action="less-like" data-event-id="${escapeHtml(event.id)}">less like this</button></div>`;
}

function integrationsCard() {
  const active = state.context?.filter((item) => item.active) || [];
  const mode = active.find((item) => item.kind === "mode")?.value?.mode || "unknown";
  return `<section class="rules-card"><div class="rules-body"><div class="eyebrow">context and connections</div><h3>links</h3><p class="field-note">${state.integrations.filter((item) => item.connection_state === "connected").length} connected · ${escapeHtml(mode)}</p><a class="button secondary full" href="/integrations">manage connections</a></div></section>`;
}

function integrationSetupBanner() {
  const params = new URLSearchParams(location.search);
  const connected = params.get("connected");
  const oauthError = params.get("oauth_error");
  const googleConnected = state.integrations.some((entry) => entry.provider === "google" && entry.connection_state === "connected");
  const discordConnected = state.integrations.some((entry) => entry.provider === "discord" && entry.connection_state === "connected");
  if (!connected && !oauthError && googleConnected && discordConnected) return `<p class="integration-message success">Google and Discord are connected. Manage them below.</p>`;
  const message = connected === "google"
    ? "Google connected. Pulse can now sync Gmail, Calendar, and contacts."
    : connected === "discord"
      ? "Discord connected. Pulse can now read your account and selected server context."
      : oauthError
        ? "That connection did not finish. Try the button again, then approve the requested access."
        : "Choose an account below. Pulse will open the official authorization screen, then return here.";
  const messageClass = connected ? "integration-message success" : oauthError ? "integration-message error" : "integration-message";
  const actions = `${googleConnected ? "" : `<a class="button" href="/api/integrations/google/connect">connect Google</a>`}${discordConnected ? "" : `<a class="button secondary" href="/api/integrations/discord/connect">connect Discord</a>`}`;
  return `<section class="integration-setup"><div><span class="eyebrow">account setup</span><h2>${googleConnected || discordConnected ? "connect another account" : "connect your accounts"}</h2><p>${escapeHtml(message)}</p></div><div class="integration-setup-actions">${actions}</div></section><p class="${messageClass}">${connected ? "connection saved securely on the server" : oauthError ? "no credentials were changed" : "you can manage or disconnect accounts below"}</p>`;
}

function renderIntegrations() {
  document.title = "Pulse · integrations";
  const item = (provider) => (state.integrations || []).find((entry) => entry.provider === provider) || { provider, label: provider, metadata: {} };
  const status = (entry) => entry.connection_state === "connected" ? { label: "Connected", icon: "✓", className: "connected" } : entry.connection_state === "error" ? { label: "Needs attention", icon: "!", className: "attention" } : entry.configured ? { label: "Needs setup", icon: "○", className: "setup" } : { label: "Not configured", icon: "—", className: "muted" };
  const badge = (entry) => { const value = status(entry); return `<span class="provider-status ${value.className}"><span>${value.icon}</span>${value.label}</span>`; };
  const service = (entry, label) => `<span class="service-chip ${entry.connection_state === "connected" ? "is-connected" : ""}">${entry.connection_state === "connected" ? "✓" : "○"} ${escapeHtml(label)}</span>`;
  const disconnect = (provider) => `<button class="button ghost-button" data-action="integration-disconnect" data-provider="${provider}">disconnect</button>`;
  const sync = (provider) => `<button class="button secondary" data-action="integration-sync" data-provider="${provider}">sync now</button>`;
  const connect = (provider, label) => `<a class="button" href="/api/integrations/${provider}/connect">${label}</a>`;
  const apple = item("apple");
  const appleCalendar = item("apple-calendar");
  const appleContacts = item("apple-contacts");
  const appleMail = item("apple-mail");
  const appleMusic = item("apple-music");
  const google = item("google");
  const gmail = item("gmail");
  const calendar = item("calendar");
  const discord = item("discord");
  const aws = item("aws-bedrock");
  const location = (state.context || []).find((entry) => entry.kind === "location");
  const locationText = location ? `updated ${formatDate(location.observed_at)}` : "not shared";
  const cards = `<section class="provider-card"><div class="provider-card-top"><div><div class="provider-icon">G</div><div><h2>Google</h2>${badge(google)}</div></div>${google.connection_state === "connected" ? `<span class="provider-check">✓</span>` : ""}</div><div class="service-list">${service(gmail, "Gmail")}${service(calendar, "Calendar")}${service(google, "Contacts")}</div><div class="provider-actions">${google.connection_state === "connected" ? `${sync("google")}${disconnect("google")}` : connect("google", "connect Google")}</div></section>`
    + `<section class="provider-card"><div class="provider-card-top"><div><div class="provider-icon discord-icon">D</div><div><h2>Discord</h2>${badge(discord)}</div></div>${discord.connection_state === "connected" ? `<span class="provider-check">✓</span>` : ""}</div><p class="provider-note">Account and server access only. Pulse does not read general chat history.</p><div class="provider-actions">${discord.connection_state === "connected" ? disconnect("discord") : connect("discord", "connect Discord")}</div></section>`
    + `<section class="provider-card"><div class="provider-card-top"><div><div class="provider-icon apple-icon">⌘</div><div><h2>Apple</h2>${badge(apple.connection_state === "connected" ? apple : appleCalendar)}</div></div></div><div class="service-list">${service(appleCalendar, "Calendar")}${service(appleContacts, "Contacts")}${service(appleMail, "Mail")}${service(appleMusic, "Music")}${service(apple, "Shortcuts / context")}</div><div class="provider-actions"><a class="button" href="/integrations/apple">set up Apple access</a></div><p class="provider-note">Calendar, Contacts, Mail, and phone context use supported iCloud protocols and Apple Shortcuts. No native app is required.</p></section>`
    + `<section class="provider-card"><div class="provider-card-top"><div><div class="provider-icon aws-icon">AI</div><div><h2>AWS AI</h2>${badge(aws)}</div></div></div><p class="provider-note">GPT-5.6 Luna · ${escapeHtml(aws.metadata?.region || "us-east-1")}</p><div class="provider-actions"><button class="button secondary" data-action="integration-test" data-provider="aws-bedrock">test Luna</button></div><details class="provider-details"><summary>technical details</summary><p>model ${escapeHtml(aws.metadata?.model || state.config?.bedrock_model_id || "configured")}. Deterministic scoring remains active if AI fails.</p></details></section>`
    + `<section class="provider-card"><div class="provider-card-top"><div><div class="provider-icon location-icon">⌖</div><div><h2>Location context</h2><span class="provider-status ${location ? "connected" : "setup"}"><span>${location ? "✓" : "○"}</span>${location ? "Active" : "Not shared"}</span></div></div></div><p class="provider-note">Coarse, temporary location only. ${escapeHtml(locationText)}.</p><div class="provider-actions"><button class="button" data-action="location-permission">use current location</button>${location ? `<button class="button ghost-button" data-action="clear-location">clear</button>` : ""}</div></section>`;
  const contextRows = (state.context || []).filter((entry) => entry.kind !== "location").map((entry) => `<div class="diagnostic-row"><span>${escapeHtml(entry.kind)}<small>${escapeHtml(entry.source)}</small></span><strong>${escapeHtml(JSON.stringify(entry.value))}</strong></div>`).join("") || `<p class="fine-print">no other context has been received</p>`;
  app.innerHTML = `<section class="page-heading"><div><div class="eyebrow">pulse links</div><h1>connections</h1></div><a class="icon-button" href="/">←</a></section><p class="lede">accounts and phone context that make alerts personal.</p>${integrationSetupBanner()}<div class="provider-grid">${cards}</div><details class="quality-card diagnostics"><summary>debug and active context</summary>${contextRows}</details><a class="button ghost-button full" href="/">back to signals</a>`;
}

async function renderAppleSetup() {
  document.title = "Pulse · Apple setup";
  let shortcut = null;
  try { shortcut = await api("/api/shortcut/setup"); } catch (_) { /* show setup without token if unavailable */ }
  let people = [];
  try { people = (await api("/api/people")).people || []; } catch (_) { /* contacts are optional */ }
  const tokenField = shortcut ? `<label>Authorization token<input id="shortcut-token" type="password" value="${escapeHtml(shortcut.token)}" readonly autocomplete="off"></label><div class="provider-actions"><button class="button secondary" data-action="copy-text" data-copy-target="shortcut-token">copy token</button><button class="button ghost-button" data-action="rotate-shortcut">rotate token</button></div><p class="fine-print">kept hidden on screen; copy it into the local Shortcut only.</p>` : `<p class="provider-note">Shortcut token is not configured on the server yet.</p>`;
  const peopleMarkup = people.length ? people.slice(0, 40).map((person) => `<div class="diagnostic-row"><span>${escapeHtml(person.name || person.id)}<small>${escapeHtml(person.provider)}${person.emails?.[0] ? ` · ${escapeHtml(person.emails[0])}` : ""}</small></span><select data-person-id="${escapeHtml(person.id)}" data-action="person-importance"><option value="important" ${person.importance === "important" ? "selected" : ""}>important</option><option value="normal" ${person.importance === "normal" ? "selected" : ""}>normal</option><option value="ignore" ${person.importance === "ignore" ? "selected" : ""}>ignore</option></select></div>`).join("") : `<p class="fine-print">sync Contacts first to choose important people.</p>`;
  app.innerHTML = `<section class="page-heading"><div><div class="eyebrow">phone context</div><h1>apple bridge</h1></div><a class="icon-button" href="/integrations">←</a></section><p class="lede">shortcuts, Apple services, and the signals that make Pulse local.</p><section class="provider-card"><div class="eyebrow">iphone companion</div><h2>pair this phone</h2><p class="provider-note">Generate a one-time code for the companion bridge. The code expires in 10 minutes and the server keeps the real token private.</p><div class="provider-actions"><button class="button" data-action="pair-companion">generate pairing code</button></div><div id="pairing-code" hidden></div><p class="fine-print">Building, signing, and background delivery of the native companion still require Xcode and an iPhone.</p></section><section class="provider-card"><form id="apple-setup-form" class="password-form"><label>Apple / iCloud email<input id="apple-id" type="email" autocomplete="username" placeholder="you@icloud.com" required></label><label>app-specific password<input id="apple-app-password" type="password" autocomplete="new-password" placeholder="xxxx-xxxx-xxxx-xxxx" minlength="8" required></label><button class="button" type="submit">save Apple access</button></form><p class="provider-note">Use an app-specific password, never your main Apple ID password.</p></section><section class="provider-card"><div class="eyebrow">shortcut context</div><h2>phone signals</h2><p class="provider-note">Send mode, battery, charging, calendar, and location context to Pulse. The server keeps the logic.</p><label>Pulse URL<input value="${escapeHtml(shortcut?.endpoint || `${location.origin}/api/shortcut/context`)}" readonly></label>${tokenField}<div class="provider-actions"><button class="button secondary" data-action="shortcut-test">test Shortcut endpoint</button></div></section><section class="provider-card"><div class="eyebrow">Apple services</div><div class="provider-actions"><button class="button secondary" data-action="integration-test" data-provider="apple-calendar">test Calendar</button><button class="button secondary" data-action="integration-test" data-provider="apple-contacts">test Contacts</button><button class="button secondary" data-action="integration-test" data-provider="apple-mail">test Mail</button></div><p class="provider-note">Only connected services are used.</p></section><details class="provider-card people-disclosure"><summary><span><span class="eyebrow">important people</span><strong>contact relevance</strong></span><span class="muted">${people.length || 0} contacts</span></summary><p class="provider-note">Mark people whose messages should matter more.</p>${peopleMarkup}</details>`;
}

function renderHome() {
  document.title = "Pulse · quiet signals";
  // History is intentionally broader than the home feed. A high score alone
  // is not proof that Pulse pushed or that the event is still useful.
  const importantEvents = state.events.filter((item) => Boolean(item.notified_at));
  const baseEvents = state.showAllHistory ? state.events : importantEvents;
  const visibleEvents = state.activeTopic === "all" ? baseEvents : baseEvents.filter((item) => item.topic === state.activeTopic);
  const events = visibleEvents.length ? visibleEvents.map(eventCard).join("") : `<div class="empty-state"><span class="empty-mark">·</span><h2>nothing here yet</h2><p>Pulse keeps the feed quiet until something crosses your rules.</p></div>`;
  const activeMode = state.context?.find((item) => item.kind === "mode")?.value?.mode || "quiet mode";
  const urgent = importantEvents.length;
  const historyAction = state.events.length > importantEvents.length ? `<button class="button ghost-button full" data-action="show-history">${state.showAllHistory ? "show important only" : `show all ${state.events.length} saved events`}</button>` : "";
  const topics = ["all", ...new Set(importantEvents.map((item) => item.topic))];
  const topicFilters = topics.map((topic) => `<button class="topic-filter ${state.activeTopic === topic ? "active" : ""}" data-action="filter-topic" data-topic="${escapeHtml(topic)}">${topic === "all" ? "for you" : escapeHtml(topicLabel(topic))}</button>`).join("");
  app.innerHTML = `<section class="home-hero"><div><span class="eyebrow">${greeting()} · aiden</span><h1>your pulse</h1><p>only the signals worth your attention.</p></div><button class="icon-button" data-action="logout" aria-label="Log out">↗</button></section><section class="signal-strip"><span><strong>${urgent}</strong> important</span><span><strong>${state.events.length}</strong> saved</span><span class="mode-pill">${escapeHtml(activeMode)}</span></section>${alertCard()}<section class="feed-head"><div><span class="eyebrow">your signal layer</span><h2>${state.activeTopic === "all" ? "for you" : escapeHtml(topicLabel(state.activeTopic))}</h2></div><span class="muted">${state.showAllHistory ? "history" : "now"}</span></section><div class="topic-filters" aria-label="Filter signals">${topicFilters}</div><section class="event-list">${events}</section>${historyAction}<p class="footer-note">quiet by default · private by design</p>`;
}

function renderSettings() {
  document.title = "Pulse · controls";
  app.innerHTML = `<section class="page-heading"><span class="eyebrow">controls</span><h1>your Pulse</h1><p>quiet hours, interests, sources, feedback, and security.</p></section>${integrationsCard()}${rulesCard()}${discoveryCard()}${learningCard()}${auditCard()}${passwordCard()}<p class="footer-note">changes apply to the next 15-minute check.</p>`;
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
    const finalScore = event.decision_trace?.effective_score ?? event.score;
    const purchase = event.metadata?.lifecycle?.purchase;
    const purchaseAction = purchase?.id ? `<button class="button secondary" data-action="watch-purchase" data-purchase-id="${escapeHtml(purchase.id)}">watch purchase</button>` : "";
    const messageAction = event.metadata?.sender && (event.metadata?.gmail_message_id || event.metadata?.mail_message_id) ? `<button class="button secondary" data-action="draft-reply" data-event-id="${escapeHtml(event.id)}">draft reply</button>` : "";
    app.innerHTML = `<section class="detail-page"><button class="back-button" data-action="back">← history</button><div class="detail-kicker"><span class="topic topic-${escapeHtml(event.topic)}">${escapeHtml(topicLabel(event.topic))}</span><span>${escapeHtml(formatDate(event.published_at || event.discovered_at))}</span></div><h1>${escapeHtml(event.title)}</h1><p class="detail-summary">${escapeHtml(event.body || event.summary || "")}</p><div class="why-card"><span class="eyebrow">why Pulse surfaced this</span><p>${escapeHtml(event.body || "matched your relevance rules")}</p><div class="signal-meter"><span style="width:${Math.max(4, Math.min(100, finalScore))}%"></span></div><div class="meter-label"><span>${escapeHtml(event.priority)} priority</span><span>${finalScore}/100</span></div></div>${detailQuality(event)}${detailDiagnostics(event)}<div class="detail-actions"><a class="button" data-action="source-click" data-event-id="${escapeHtml(event.id)}" href="${escapeHtml(event.url)}" target="_blank" rel="noreferrer">open source</a>${purchaseAction}${messageAction}<button class="button secondary" data-action="remind" data-event-id="${escapeHtml(event.id)}">remind me</button><button class="button ghost-button" data-action="mute" data-topic="${escapeHtml(event.topic)}">mute topic</button></div><div id="reply-draft" hidden></div>${detailFeedback(event)}<p class="fine-print">event id ${escapeHtml(event.id)}</p></section>`;
  } catch (error) {
    app.innerHTML = `<section class="empty-state"><h2>signal unavailable</h2><p>${escapeHtml(error.message)}</p><button class="button" data-action="back">back to history</button></section>`;
  }
}

function render() {
  const nav = document.querySelector("#bottom-nav");
  if (nav) nav.hidden = !state.authenticated;
  if (!state.authenticated) {
    app.innerHTML = loginView();
    return;
  }
  if (location.pathname.startsWith("/event/")) {
    renderDetail(decodeURIComponent(location.pathname.slice("/event/".length)));
  } else if (location.pathname.startsWith("/integrations")) {
    if (location.pathname.startsWith("/integrations/apple")) renderAppleSetup();
    else renderIntegrations();
  } else if (location.pathname.startsWith("/settings")) {
    renderSettings();
  } else {
    renderHome();
  }
  const activeNav = location.pathname.startsWith("/integrations") ? "integrations" : location.pathname.startsWith("/settings") ? "settings" : "home";
  document.querySelectorAll("[data-nav]").forEach((item) => item.classList.toggle("active", item.dataset.nav === activeNav));
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
  if (event.target.id === "apple-setup-form") {
    try {
      await api("/api/integrations/apple/setup", { method: "POST", body: JSON.stringify({ apple_id: document.querySelector("#apple-id").value, app_password: document.querySelector("#apple-app-password").value }) });
      showToast("Apple access saved securely");
      await loadAuthenticatedState(); renderAppleSetup();
    } catch (error) { showToast(error.message, true); }
    return;
  }
  if (event.target.id === "game-event-form") {
    try {
      const starts = new Date(document.querySelector("#game-event-starts").value);
      if (Number.isNaN(starts.getTime())) throw new Error("choose a valid start time");
      await api("/api/game-events", { method: "POST", body: JSON.stringify({ game: document.querySelector("#game-event-game").value, title: document.querySelector("#game-event-title").value, starts_at: starts.toISOString(), kind: "event" }) });
      showToast("countdown added");
      await loadAuthenticatedState(); render();
    } catch (error) { showToast(error.message, true); }
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
    if (action === "open-settings") {
      history.pushState({}, "", "/settings");
      render();
      return;
    }
    if (action === "enable-alerts") await enableAlerts();
    if (action === "test-push") { const result = await api("/api/push/test", { method: "POST", body: "{}" }); showToast(result.delivery?.sent ? "test sent" : "test event saved; delivery is not configured"); }
    if (action === "source-click") { void api(`/api/events/${encodeURIComponent(event.target.closest("[data-event-id]")?.dataset.eventId || state.detail?.id)}/source-clicked`, { method: "POST", body: "{}" }); return; }
    if (action === "logout") { await api("/api/auth/logout", { method: "POST", body: "{}" }); state.authenticated = false; render(); }
    if (action === "back") { history.pushState({}, "", "/"); await loadAuthenticatedState(); render(); }
    if (action === "reload") window.location.reload();
    if (action === "show-history") { state.showAllHistory = !state.showAllHistory; renderHome(); return; }
    if (action === "filter-topic") { state.activeTopic = event.target.closest("[data-topic]")?.dataset.topic || "all"; renderHome(); return; }
    if (action === "remind") { await api(`/api/events/${encodeURIComponent(event.target.closest("[data-event-id]")?.dataset.eventId || state.detail.id)}/remind`, { method: "POST", body: JSON.stringify({ minutes: 60 }) }); showToast("reminder set for one hour"); }
    if (action === "watch-purchase") { await api(`/api/purchases/${encodeURIComponent(event.target.closest("[data-purchase-id]")?.dataset.purchaseId)}/watch`, { method: "PATCH", body: JSON.stringify({ priority: "high" }) }); showToast("purchase watch enabled"); }
    if (action === "draft-reply") {
      const target = document.querySelector("#reply-draft");
      target.hidden = false;
      target.innerHTML = `<div class="quality-card"><span class="eyebrow">review-only draft</span><p class="field-note">Pulse will not send this. Edit it before using it anywhere.</p><p>drafting…</p></div>`;
      const result = await api(`/api/events/${encodeURIComponent(event.target.closest("[data-event-id]")?.dataset.eventId || state.detail.id)}/draft-reply`, { method: "POST", body: JSON.stringify({ tone: "brief and friendly" }) });
      target.innerHTML = `<div class="quality-card"><span class="eyebrow">review-only draft</span><textarea id="reply-draft-text" rows="6">${escapeHtml(result.draft)}</textarea><div class="provider-actions"><button class="button secondary" data-action="copy-text" data-copy-target="reply-draft-text">copy draft</button></div><p class="fine-print">generated by ${escapeHtml(result.generated_by)} · Pulse never sends replies automatically</p></div>`;
      showToast("draft ready for review");
    }
    if (action === "delete-game-event") { await api(`/api/game-events/${encodeURIComponent(event.target.closest("[data-game-event-id]")?.dataset.gameEventId)}`, { method: "DELETE", body: "{}" }); await loadAuthenticatedState(); render(); showToast("countdown removed"); }
    if (action === "mute") { await api(`/api/topics/${encodeURIComponent(event.target.closest("[data-topic]")?.dataset.topic || state.detail.topic)}/mute`, { method: "POST", body: JSON.stringify({ days: 7 }) }); showToast("topic muted for seven days"); }
    if (action === "follow" || action === "less-like") {
      const eventId = event.target.closest("[data-event-id]")?.dataset.eventId || state.detail.id;
      const entityId = event.target.closest("[data-entity-id]")?.dataset.entityId;
      await api(`/api/events/${encodeURIComponent(eventId)}/feedback`, { method: "POST", body: JSON.stringify({ action: action === "follow" ? "follow" : "less_like", ...(entityId ? { entity_id: entityId } : {}) }) });
      state.learning = await api("/api/learning");
      showToast(action === "follow" ? "interest followed" : "we will show less like this");
    }
    if (action === "follow-story") {
      const eventId = event.target.closest("[data-event-id]")?.dataset.eventId || state.detail.id;
      await api(`/api/events/${encodeURIComponent(eventId)}/feedback`, { method: "POST", body: JSON.stringify({ action: "follow_story" }) });
      state.learning = await api("/api/learning");
      showToast("story followed for 30 days");
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
      const priorities = Object.fromEntries([...document.querySelectorAll("[data-priority-topic]")].map((input) => [input.dataset.priorityTopic, Number(input.value)]));
      const topic = document.querySelector("#temporary-topic").value;
      const temporary = topic ? {[topic]: { boost: Number(document.querySelector("#temporary-boost").value), expires_at: new Date(Date.now() + Number(document.querySelector("#temporary-hours").value) * 3600000).toISOString() }} : {};
      await api("/api/priorities", { method: "POST", body: JSON.stringify({ personal_priorities: priorities, temporary_priority: temporary }) });
      state.preferences = {...result.preferences, personal_priorities: priorities, temporary_priority: temporary}; showToast("rules saved");
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
    if (action === "integration-test") {
      const result = await api(`/api/integrations/${encodeURIComponent(event.target.closest("[data-provider]")?.dataset.provider)}/test`, { method: "POST", body: "{}" });
      showToast(result.ok ? "integration test passed" : "integration is not connected", !result.ok);
      await loadAuthenticatedState(); renderIntegrations();
    }
    if (action === "integration-sync") {
      const provider = event.target.closest("[data-provider]")?.dataset.provider;
      await api(`/api/integrations/${encodeURIComponent(provider)}/sync`, { method: "POST", body: "{}" });
      showToast("sync complete"); await loadAuthenticatedState(); renderIntegrations();
    }
    if (action === "integration-disconnect") {
      const provider = event.target.closest("[data-provider]")?.dataset.provider;
      await api(`/api/integrations/${encodeURIComponent(provider)}/disconnect`, { method: "POST", body: "{}" });
      showToast("integration disconnected"); await loadAuthenticatedState(); renderIntegrations();
    }
    if (action === "pair-companion") {
      const result = await api("/api/companion/pair/start", { method: "POST", body: "{}" });
      const target = document.querySelector("#pairing-code");
      if (target) {
        target.hidden = false;
        target.innerHTML = `<div class="pairing-code"><span class="eyebrow">one-time code · expires in ${escapeHtml(result.expires_in_minutes)} minutes</span><div class="input-row"><input id="pairing-code-value" value="${escapeHtml(result.code)}" readonly><button class="button secondary" data-action="copy-text" data-copy-target="pairing-code-value">copy</button></div><p class="fine-print">enter this code in the Pulse companion app, then send one battery or mode signal.</p></div>`;
      }
      showToast("pairing code ready");
    }
    if (action === "location-permission") {
      if (!navigator.geolocation) throw new Error("this browser does not provide location");
      navigator.geolocation.getCurrentPosition(async (position) => {
        try {
          await api("/api/location", { method: "POST", body: JSON.stringify({ latitude: position.coords.latitude, longitude: position.coords.longitude, accuracy: position.coords.accuracy }) });
          await loadAuthenticatedState(); renderIntegrations(); showToast("coarse location saved temporarily");
        } catch (error) { showToast(error.message, true); }
      }, (error) => showToast(error.code === 1 ? "location permission was not granted" : "location could not be read", true), { enableHighAccuracy: false, maximumAge: 300000, timeout: 10000 });
    }
    if (action === "clear-location") {
      await api("/api/location", { method: "DELETE", body: "{}" });
      await loadAuthenticatedState(); renderIntegrations(); showToast("location context cleared");
    }
    if (action === "apple-setup") showToast("Apple web access needs an app-specific password in the secure server environment.");
    if (action === "copy-text") {
      const input = document.querySelector(`#${event.target.closest("[data-copy-target]")?.dataset.copyTarget}`);
      if (input) { await navigator.clipboard.writeText(input.value); showToast("copied"); }
    }
    if (action === "rotate-shortcut") {
      if (!window.confirm("Rotate the Shortcut token? The old token will stop working.")) return;
      await api("/api/shortcut/token/rotate", { method: "POST", body: "{}" });
      renderAppleSetup(); showToast("Shortcut token rotated");
    }
    if (action === "shortcut-test") {
      const setup = await api("/api/shortcut/setup");
      const response = await fetch("/api/shortcut/context", { method: "POST", headers: { "Content-Type": "application/json", "X-Pulse-Shortcut-Token": setup.token }, body: JSON.stringify({ mode: "unknown", confidence: 0.6, signals: { shortcut: { test: "true" } } }) });
      if (!response.ok) throw new Error("Shortcut test failed");
      showToast("Shortcut connected ✓");
      await loadAuthenticatedState(); renderAppleSetup();
    }
    if (action === "person-importance") {
      const select = event.target.closest("[data-person-id]");
      await api(`/api/people/${encodeURIComponent(select.dataset.personId)}`, { method: "PATCH", body: JSON.stringify({ importance: select.value }) });
      showToast("contact importance saved");
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
