// Minimal service worker so the site is installable as PWA / "APK"
const CACHE = 'Signor movie-dash-v1';
self.addEventListener('install', (e) => {
  self.skipWaiting();
});
self.addEventListener('activate', (e) => {
  e.waitUntil(clients.claim());
});
self.addEventListener('fetch', (e) => {
  // Network-first, no heavy caching needed for admin dashboard
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
