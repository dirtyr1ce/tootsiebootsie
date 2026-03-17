// TootsieBootsie Service Worker v1
// Handles offline caching, background sync, push notifications

const VERSION = 'tb-v1';
const STATIC_CACHE = `${VERSION}-static`;
const API_CACHE    = `${VERSION}-api`;
const IMG_CACHE    = `${VERSION}-images`;

// Files to cache immediately on install
const STATIC_ASSETS = [
  '/',
  '/index.html',
  '/manifest.json',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  // Babylon.js is large — we cache it after first load
];

// External CDN assets to cache
const CDN_ASSETS = [
  'https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js',
  'https://cdn.babylonjs.com/babylon.js',
];

// ── Install ───────────────────────────────────────────────────
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then(cache => {
      console.log('[SW] Caching static assets');
      return cache.addAll(STATIC_ASSETS);
    }).then(() => self.skipWaiting())
  );
});

// ── Activate ──────────────────────────────────────────────────
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys
          .filter(k => k.startsWith('tb-') && k !== STATIC_CACHE &&
                       k !== API_CACHE && k !== IMG_CACHE)
          .map(k => {
            console.log('[SW] Deleting old cache:', k);
            return caches.delete(k);
          })
      )
    ).then(() => self.clients.claim())
  );
});

// ── Fetch strategy ────────────────────────────────────────────
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // API calls — network first, fall back to cache
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(networkFirstAPI(event.request));
    return;
  }

  // Google Places photos — cache first (they're expensive)
  if (url.hostname === 'maps.googleapis.com' && url.pathname.includes('/place/photo')) {
    event.respondWith(cacheFirstImage(event.request));
    return;
  }

  // CDN scripts (Three.js, Babylon) — cache first, very long TTL
  if (url.hostname.includes('cloudflare.com') || url.hostname.includes('babylonjs.com') ||
      url.hostname.includes('jsdelivr.net') || url.hostname.includes('googleapis.com')) {
    event.respondWith(cacheFirstStatic(event.request));
    return;
  }

  // Supabase trace photos — cache first
  if (url.hostname.includes('supabase.co') && url.pathname.includes('/storage/')) {
    event.respondWith(cacheFirstImage(event.request));
    return;
  }

  // Google fonts — cache first
  if (url.hostname.includes('fonts.googleapis.com') || url.hostname.includes('fonts.gstatic.com')) {
    event.respondWith(cacheFirstStatic(event.request));
    return;
  }

  // HTML / app shell — network first, offline fallback
  if (event.request.mode === 'navigate') {
    event.respondWith(networkFirstApp(event.request));
    return;
  }

  // Everything else — stale while revalidate
  event.respondWith(staleWhileRevalidate(event.request));
});

// ── Strategies ────────────────────────────────────────────────

// API: try network, cache on success, return cached on failure
async function networkFirstAPI(request) {
  try {
    const response = await fetch(request.clone());
    if (response.ok) {
      const cache = await caches.open(API_CACHE);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    const cached = await caches.match(request);
    if (cached) return cached;
    // Return offline JSON if no cache
    return new Response(JSON.stringify({
      places: [],
      offline: true,
      message: 'Offline — showing cached discoveries'
    }), { headers: { 'Content-Type': 'application/json' } });
  }
}

// App shell: network first, fall back to cached index.html
async function networkFirstApp(request) {
  try {
    const response = await fetch(request);
    const cache = await caches.open(STATIC_CACHE);
    cache.put(request, response.clone());
    return response;
  } catch {
    const cached = await caches.match(request) ||
                   await caches.match('/index.html');
    return cached || new Response('TootsieBootsie is offline', {
      status: 503, headers: { 'Content-Type': 'text/plain' }
    });
  }
}

// Images: cache first, fetch and cache on miss
async function cacheFirstImage(request) {
  const cached = await caches.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(IMG_CACHE);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    // Return a tiny transparent placeholder
    return new Response(
      '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"></svg>',
      { headers: { 'Content-Type': 'image/svg+xml' } }
    );
  }
}

// Static/CDN: cache first, very rarely update
async function cacheFirstStatic(request) {
  const cached = await caches.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request);
    const cache = await caches.open(STATIC_CACHE);
    cache.put(request, response.clone());
    return response;
  } catch {
    return new Response('', { status: 503 });
  }
}

// Stale while revalidate
async function staleWhileRevalidate(request) {
  const cache = await caches.open(STATIC_CACHE);
  const cached = await cache.match(request);
  const fetchPromise = fetch(request).then(response => {
    if (response.ok) cache.put(request, response.clone());
    return response;
  }).catch(() => cached);
  return cached || fetchPromise;
}

// ── Push notifications ────────────────────────────────────────
self.addEventListener('push', event => {
  if (!event.data) return;
  const data = event.data.json();
  event.waitUntil(
    self.registration.showNotification(data.title || 'TootsieBootsie', {
      body: data.body || 'Someone left a Trace near you!',
      icon: '/icons/icon-192.png',
      badge: '/icons/icon-96.png',
      vibrate: [100, 50, 100],
      data: { url: data.url || '/' },
      actions: [
        { action: 'view', title: 'See it' },
        { action: 'dismiss', title: 'Dismiss' }
      ]
    })
  );
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  if (event.action === 'view' || !event.action) {
    const url = event.notification.data?.url || '/';
    event.waitUntil(
      clients.matchAll({ type: 'window' }).then(windowClients => {
        const existing = windowClients.find(c => c.url === url && 'focus' in c);
        if (existing) return existing.focus();
        return clients.openWindow(url);
      })
    );
  }
});

// ── Background sync (for offline trace uploads) ───────────────
self.addEventListener('sync', event => {
  if (event.tag === 'sync-traces') {
    event.waitUntil(syncPendingTraces());
  }
});

async function syncPendingTraces() {
  // Get pending traces from IndexedDB and retry upload
  // Full implementation in the Capacitor native version
  console.log('[SW] Syncing pending traces...');
}
