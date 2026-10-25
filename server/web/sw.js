/* Only the shell is cached. Authenticated API responses must never be cached. */
const CACHE = 'app01-shell-v4';
const SHELL = ['/app/', '/app/styles.css', '/app/app.js', '/app/rich-text.js', '/app/manifest.webmanifest', '/app/icon-192.png', '/app/icon-512.png'];
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith('app01-shell-') && key !== CACHE).map(key => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || !SHELL.includes(url.pathname) || event.request.method !== 'GET') return;
  event.respondWith(fetch(event.request).then(response => {
    if (response.ok) {
      const copy = response.clone();
      caches.open(CACHE).then(cache => cache.put(event.request, copy));
    }
    return response;
  }).catch(() => caches.match(event.request)));
});

function appWindows() {
  return self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(windows => windows.filter(client => {
    const url = new URL(client.url);
    return url.origin === self.location.origin && url.pathname.startsWith('/app/');
  }));
}
self.addEventListener('push', event => {
  let test = false;
  try { test = event.data?.json()?.type === 'test'; } catch (_) { /* Always show a generic alert. */ }
  event.waitUntil((async () => {
    await self.registration.showNotification('Agentic Compendium', {
      body: test ? 'Your test notification is working.' : 'New deliveries are ready. Open your private archive to read them.',
      icon: '/app/icon-192.png', badge: '/app/icon-192.png',
      tag: test ? 'compendium-test' : 'compendium-deliveries', renotify: false
    });
    if (!test) for (const client of await appWindows()) client.postMessage({type: 'compendium-deliveries'});
  })());
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil((async () => {
    const windows = await appWindows();
    if (windows.length) {
      const client = windows.find(window => window.focused) || windows[0];
      await client.focus();
      client.postMessage({type: 'compendium-deliveries'});
    } else await self.clients.openWindow('/app/');
  })());
});
