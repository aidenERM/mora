const CACHE = "pulse-shell-v3";
const SHELL = ["/", "/static/styles.css", "/static/app.js?v=pulse-discovery-1", "/manifest.webmanifest", "/icon.svg"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET" || new URL(event.request.url).origin !== self.location.origin) return;
  event.respondWith(fetch(event.request).then((response) => {
    const copy = response.clone();
    caches.open(CACHE).then((cache) => cache.put(event.request, copy));
    return response;
  }).catch(() => caches.match(event.request).then((cached) => cached || caches.match("/"))));
});

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (_) { data = { title: "Pulse", body: event.data?.text() || "New signal" }; }
  const url = data.url || "/";
  event.waitUntil(self.registration.showNotification(data.title || "Pulse", {
    body: data.body || "A new signal crossed your rules.",
    tag: data.tag || `pulse-${data.id || Date.now()}`,
    renotify: true,
    requireInteraction: data.priority === "critical",
    icon: "/icon.svg",
    badge: "/icon.svg",
    data: { url, eventId: data.id || null },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || "/", self.location.origin).href;
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const sameOrigin = windows.find((client) => new URL(client.url).origin === self.location.origin);
    if (sameOrigin) {
      try { await sameOrigin.navigate(target); } catch (_) { /* opening below is the fallback */ }
      await sameOrigin.focus();
      return;
    }
    if (self.clients.openWindow) await self.clients.openWindow(target);
  })());
});
