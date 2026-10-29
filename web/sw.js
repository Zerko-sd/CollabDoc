/**
 * Service worker: makes CollabDoc installable and lets the shell load offline.
 *
 * The static shell (HTML, scripts, styles, icons) is network-first: a fresh
 * copy whenever the server answers, so a deploy is live at once, and the
 * cached copy when it does not. The API and the sockets are never cached --
 * a document must not be served stale to someone who is editing it.
 */
const CACHE = "collabdoc-shell-v1";
const SHELL = [
  "/",
  "/static/styles.css",
  "/static/app.js",
  "/static/editor.js",
  "/static/extras.js",
  "/static/ot.js",
  "/static/units.js",
  "/static/icons/icon-192.png",
  "/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  const isShell = url.pathname === "/" || url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest";
  if (!isShell) return; // API, files, published pages: always the network

  event.respondWith(
    caches.open(CACHE).then(async (cache) => {
      const key = url.pathname === "/" ? "/" : request;
      try {
        const response = await fetch(request);
        if (response.ok) cache.put(key, response.clone());
        return response;
      } catch {
        const cached = await cache.match(key);
        if (cached) return cached;
        throw new Error("offline and not cached");
      }
    })
  );
});
