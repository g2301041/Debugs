self.addEventListener('push', event => {
  let data = {};
  try { data = event.data.json(); } catch (_) {}
  event.waitUntil(self.registration.showNotification(data.title || 'ベアウェザー', {
    body: data.body || '新しい熊情報があります。',
    tag: data.tag || 'bear-weather',
    data: { url: '/' }
  }));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil((async () => {
    const tabs = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const tab of tabs) {
      if (new URL(tab.url).origin === self.location.origin) return tab.focus();
    }
    return self.clients.openWindow('/');
  })());
});
