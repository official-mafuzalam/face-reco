// Gate Face Service Worker for PWA and Mobile Web Push
self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('push', (event) => {
  if (!event.data) return;
  let data = {};
  try {
    data = event.data.json();
  } catch (e) {
    data = { title: 'Gate Face Alert', body: event.data.text() };
  }

  const title = data.title || 'Gate Face';
  const options = {
    body: data.body || '',
    icon: data.icon || '/static/icons/icon-192.svg',
    badge: '/static/icons/badge-96.svg',
    image: data.image || undefined,
    data: {
      url: data.url || '/'
    },
    tag: data.tag || 'gate-notification',
    renotify: true,
    requireInteraction: data.severity === 'critical',
    vibrate: data.severity === 'critical' ? [300, 150, 300, 150, 500] : [150, 75, 150]
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const targetUrl = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if ('focus' in client && client.url.includes(self.location.origin)) {
          client.navigate(targetUrl);
          return client.focus();
        }
      }
      if (clients.openWindow) {
        return clients.openWindow(targetUrl);
      }
    })
  );
});
