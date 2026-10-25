// 서비스 워커: 웹 푸시 알림을 띄우고, 알림을 누르면 보호자 화면을 연다.
//
// 일부러 페이지를 캐시하지 않는다. 경보 화면이 오래된 내용으로 보이면 위험하기 때문이다.
// 서버 알림 내용: { title, body, url, event_id, kind: 'alert'|'clear', tag }

self.addEventListener('install', () => {
  // 새 버전을 기다리지 않고 바로 쓴다
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('push', (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { body: event.data ? event.data.text() : '' };
  }
  const isAlert = data.kind === 'alert';
  const title = data.title || (isAlert ? '[낙상 경보]' : '낙상 감지 알림');
  const options = {
    body: data.body || '',
    // 같은 이벤트의 해제 알림이 경보 알림을 덮어쓴다
    tag: data.event_id != null ? `event-${data.event_id}` : (data.tag || 'fall-alert'),
    renotify: true,                  // 덮어쓸 때도 다시 소리·진동을 낸다
    requireInteraction: isAlert,     // 경보는 보호자가 누를 때까지 화면에 남긴다
    vibrate: isAlert ? [500, 200, 500, 200, 500] : [200],
    data: { url: data.url || '/guardian' },
    lang: 'ko-KR',
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  // 같은 사이트 주소만 연다 (알림 내용에 다른 사이트 주소가 들어와도 따라가지 않게)
  let target = new URL('/guardian', self.location.origin);
  try {
    const url = new URL((event.notification.data && event.notification.data.url) || '/guardian', self.location.origin);
    if (url.origin === self.location.origin) target = url;
  } catch { /* 기본값 사용 */ }

  event.waitUntil((async () => {
    const tabs = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    // 이미 열린 보호자 화면이 있으면 그 탭을 앞으로 가져온다
    for (const tab of tabs) {
      const path = new URL(tab.url).pathname;
      if (path === target.pathname && 'focus' in tab) return tab.focus();
    }
    return self.clients.openWindow(target.href);
  })());
});
