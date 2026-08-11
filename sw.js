// Service worker de Biwenger Intel.
// - App shell: cache-first (funciona offline y arranca al instante).
// - data.json: network-first (siempre intenta los datos más recientes).
const VERSION = "v10";
const SHELL = "shell-" + VERSION;
const SHELL_FILES = [
  "./", "./index.html", "./manifest.webmanifest",
  "./icon-192.png", "./icon-512.png", "./apple-touch-icon.png"
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  const isData = url.pathname.endsWith("data.json");

  if (isData) {
    // network-first: datos frescos si hay red; si no, el último que cacheamos
    e.respondWith(
      fetch(e.request).then((res) => {
        const copy = res.clone();
        caches.open(SHELL).then((c) => c.put(e.request, copy));
        return res;
      }).catch(() => caches.match(e.request))
    );
    return;
  }

  // resto: cache-first
  e.respondWith(caches.match(e.request).then((hit) => hit || fetch(e.request)));
});
