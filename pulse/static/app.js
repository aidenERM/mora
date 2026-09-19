const state = {
  config: null,
  authenticated: false,
  events: [],
  preferences: null,
  subscription: false,
  detail: null,
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
  if (!("serviceWorker" in navigator)) return;
  try {
    await navigator.serviceWorker.register("/sw.js", { scope: "/" });
  } catch (error) {
    console.warn("service worker unavailable", error);
  }
}

async function checkSubscription() {
  if (!("serviceWorker" in navigator) || !state.config?.vapid_public_key) return false;
  const registration = await navigator.serviceWorker.ready;
  const subscription = await registration.pushManager.getSubscription();
  state.subscription = Boolean(subscription);
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
  if (!("Notification" in window) || !("serviceWorker" in navigator)) {
    showToast("This browser does not support web push.", true);
    return;
  }
  try {
    const permission = await Notification.requestPermission();
    if (permission !== "granted") {
      showToast("Notifications remain off.", true);
      return;
    }
    const registration = await navigator.serviceWorker.ready;
    const subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(state.config.vapid_public_key) });
    await api("/api/subscriptions", { method: "POST", body: JSON.stringify(subscription) });
    state.subscription = true;
    renderHome();
    showToast("alerts are on");
  } catch (error) {
    showToast(error.message, true);
  }
}

async function loadAuthenticatedState() {
  const [events, preferences] = await Promise.all([api("/api/events?limit=50"), api("/api/preferences")]);
  state.events = events.events || [];
  state.preferences = preferences.preferences;
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
  return `<button class="event-card" data-event-id="${escapeHtml(event.id)}">
    <div class="event-card-top"><span class="topic topic-${escapeHtml(event.topic)}">${escapeHtml(topicLabel(event.topic))}</span><span class="event-time">${escapeHtml(formatDate(event.published_at || event.discovered_at))}</span></div>
    <h3>${escapeHtml(event.title)}</h3>
    <p>${escapeHtml(event.summary || event.body || "Open for details")}</p>
    <div class="event-card-bottom"><span class="priority priority-${escapeHtml(event.priority)}">${escapeHtml(event.priority)}</span><span class="score">${event.score}/100</span></div>
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

function renderHome() {
  document.title = "Pulse · quiet signals";
  const events = state.events.length ? state.events.map(eventCard).join("") : `<div class="empty-state"><span class="empty-mark">·</span><h2>nothing worth interrupting you for</h2><p>That is the point. New events will appear here when they matter.</p></div>`;
  app.innerHTML = `<section class="page-heading"><div><div class="eyebrow">aiden · personal feed</div><h1>your pulse</h1></div><button class="icon-button" data-action="logout" aria-label="Log out">↗</button></section>${alertCard()}<section class="feed-head"><div><span class="eyebrow">recent signals</span><h2>history</h2></div><span class="muted">${state.events.length} saved</span></section><section class="event-list">${events}</section>${rulesCard()}${passwordCard()}<p class="footer-note">Pulse is quiet by default. source credentials never leave the server.</p>`;
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
    app.innerHTML = `<section class="detail-page"><button class="back-button" data-action="back">← history</button><div class="detail-kicker"><span class="topic topic-${escapeHtml(event.topic)}">${escapeHtml(topicLabel(event.topic))}</span><span>${escapeHtml(formatDate(event.published_at || event.discovered_at))}</span></div><h1>${escapeHtml(event.title)}</h1><p class="detail-summary">${escapeHtml(event.summary || event.body || "")}</p><div class="why-card"><span class="eyebrow">why Pulse surfaced this</span><p>${escapeHtml(event.body || "matched your relevance rules")}</p><div class="signal-meter"><span style="width:${Math.max(4, Math.min(100, event.score))}%"></span></div><div class="meter-label"><span>${escapeHtml(event.priority)} priority</span><span>${event.score}/100</span></div></div><div class="detail-actions"><a class="button" href="${escapeHtml(event.url)}" target="_blank" rel="noreferrer">open source</a><button class="button secondary" data-action="remind" data-event-id="${escapeHtml(event.id)}">remind me</button><button class="button ghost-button" data-action="mute" data-topic="${escapeHtml(event.topic)}">mute topic</button></div><p class="fine-print">event id ${escapeHtml(event.id)}</p></section>`;
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
    if (action === "logout") { await api("/api/auth/logout", { method: "POST", body: "{}" }); state.authenticated = false; render(); }
    if (action === "back") { history.pushState({}, "", "/"); await loadAuthenticatedState(); render(); }
    if (action === "reload") window.location.reload();
    if (action === "remind") { await api(`/api/events/${encodeURIComponent(event.target.closest("[data-event-id]")?.dataset.eventId || state.detail.id)}/remind`, { method: "POST", body: JSON.stringify({ minutes: 60 }) }); showToast("reminder set for one hour"); }
    if (action === "mute") { await api(`/api/topics/${encodeURIComponent(event.target.closest("[data-topic]")?.dataset.topic || state.detail.topic)}/mute`, { method: "POST", body: JSON.stringify({ days: 7 }) }); showToast("topic muted for seven days"); }
    if (action === "save-rules") {
      const thresholds = Object.fromEntries([...document.querySelectorAll("[data-threshold-topic]")].map((input) => [input.dataset.thresholdTopic, Number(input.value)]));
      const result = await api("/api/preferences", { method: "PUT", body: JSON.stringify({ quiet_start: document.querySelector("#quiet-start").value, quiet_end: document.querySelector("#quiet-end").value, topic_thresholds: thresholds }) });
      state.preferences = result.preferences; showToast("rules saved");
    }
  } catch (error) {
    showToast(error.message, true);
  }
});

document.addEventListener("input", (event) => {
  if (event.target.matches("[data-threshold-topic]")) event.target.nextElementSibling.value = event.target.value;
});

window.addEventListener("popstate", render);
registerServiceWorker();
bootstrap();
