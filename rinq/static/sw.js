// Tina Phone Service Worker
// Minimal SW to enable PWA installation

self.addEventListener('install', (event) => {
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil(clients.claim());
});

// No fetch handler. It used to pass every request through
// event.respondWith(fetch(event.request)), which only added a hop that a
// request could stall in. On 2026-10-12 every warm-transfer hand-off froze in
// the browser without reaching the server; Chrome no longer needs a fetch
// handler to install the app, so the browser now talks to the server directly.
