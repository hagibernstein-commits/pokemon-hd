const CACHE_VERSION = 'v5.0.0';
const CACHE_NAME = `pokemon-hd-151-${CACHE_VERSION}`;

const PRECACHE_ASSETS = [
  './',
  './index.html',
  './manifest.json',
  './icons/icon-192.png',
  './icons/icon-512.png'
];

self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(PRECACHE_ASSETS))
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key !== CACHE_NAME)
          .map((oldKey) => caches.delete(oldKey))
      )
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  event.respondWith(
    caches.match(req).then((cached) => {
      if (cached) {
        // Refresh cached asset in background when online (stale-while-revalidate for same-origin)
        if (req.url.startsWith(self.location.origin)) {
          fetch(req)
            .then((res) => {
              if (res && res.ok) {
                caches.open(CACHE_NAME).then((c) => c.put(req, res.clone())).catch(() => {});
              }
            })
            .catch(() => {});
        }
        return cached;
      }

      return fetch(req)
        .then((res) => {
          if (res && (res.ok || res.type === 'opaque')) {
            const copy = res.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(req, copy)).catch(() => {});
          }
          return res;
        })
        .catch(() => {
          if (req.mode === 'navigate') {
            return caches.match('./index.html') || caches.match('./');
          }
          return cached;
        });
    })
  );
});
