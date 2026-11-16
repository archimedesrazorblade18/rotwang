// Keeps the app opening when offline. The job list itself is always fetched fresh.
const SHELL = "rotwang-shell-v2";
const FILES = ["./", "index.html", "manifest.webmanifest", "icon.svg", "icon-192.png", "icon-512.png",
  "unbounded-latin-600-normal.woff2", "instrument-sans-latin-400-normal.woff2",
  "instrument-sans-latin-500-normal.woff2", "instrument-sans-latin-600-normal.woff2"];
self.addEventListener("install", e => { e.waitUntil(caches.open(SHELL).then(c => c.addAll(FILES)).then(() => self.skipWaiting())) });
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== SHELL).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.origin !== location.origin || u.pathname.endsWith("data.json")) return;
  // network first, so a new version of the app shows up as soon as it is published
  e.respondWith(fetch(e.request).then(r => {
    const copy = r.clone(); caches.open(SHELL).then(c => c.put(e.request, copy)); return r;
  }).catch(() => caches.match(e.request, { ignoreSearch: true })));
});
