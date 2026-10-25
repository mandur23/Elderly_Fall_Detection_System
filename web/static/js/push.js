// 웹 푸시 알림 켜기·끄기.
//
// 흐름: 서비스 워커(/sw.js) 등록 → 서버의 공개 키(VAPID) 받기 → 알림 권한 묻기
//       → 브라우저 푸시 서비스에 구독 → 구독 정보를 서버에 저장(POST /api/push/subscribe)
// 이후 경보가 나면 서버가 브라우저 회사의 푸시 서비스로 알림을 보내고, 브라우저를 닫아 두어도 sw.js가 알림을 띄운다.
//
// 조건: HTTPS 페이지(또는 http://localhost)여야 하고, 안드로이드 Chrome에서 가장 잘 된다.
// 아이폰(iOS 16.4 이상)은 Safari에서 '홈 화면에 추가'한 뒤 그 아이콘으로 열어야 푸시를 쓸 수 있다.

import { api } from './api.js';

// 오류 문장을 그대로 화면에 보여 줄 수 있게 한국어로 던진다
export class PushError extends Error {
  constructor(message) {
    super(message);
    this.name = 'PushError';
    this.detail = message;
  }
}

function isIOS() {
  return /iPhone|iPad|iPod/.test(navigator.userAgent)
    || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
}

function isStandalone() {
  return window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
}

// 이 브라우저에서 웹 푸시를 쓸 수 있는가
export function pushSupported() {
  return window.isSecureContext
    && 'serviceWorker' in navigator
    && 'PushManager' in window
    && 'Notification' in window;
}

// 쓸 수 없는 이유를 한국어로
function unsupportedReason() {
  if (!window.isSecureContext) {
    return '알림은 보안 연결(https://)로 접속했을 때만 켤 수 있습니다. 주소가 https로 시작하는지 확인해 주세요.';
  }
  if (isIOS() && !isStandalone()) {
    return '아이폰은 Safari의 공유 버튼 → "홈 화면에 추가"를 한 뒤, 홈 화면 아이콘으로 열어야 알림을 켤 수 있습니다.';
  }
  return '이 브라우저는 알림을 지원하지 않습니다. 안드로이드 휴대폰의 Chrome에서 열어 주세요.';
}

// 서버가 주는 공개 키(base64url 문자열)를 pushManager.subscribe가 받는 바이트 배열로
export function base64UrlToUint8Array(base64Url) {
  const padding = '='.repeat((4 - (base64Url.length % 4)) % 4);
  const base64 = (base64Url + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(base64);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i);
  return bytes;
}

function sameKey(a, b) {
  if (!a || !b) return false;
  const x = new Uint8Array(a);
  const y = new Uint8Array(b);
  if (x.length !== y.length) return false;
  return x.every((v, i) => v === y[i]);
}

// 서버의 푸시 설정. { key: 문자열|null, enabled: bool }
async function serverKey() {
  return api('/api/push/vapid-public-key', { noRedirect: true });
}

// 이미 등록된 서비스 워커만 찾는다. (navigator.serviceWorker.ready는 등록이 없으면 영원히 기다리므로 쓰지 않는다)
async function existingSubscription() {
  if (!pushSupported()) return null;
  const reg = await navigator.serviceWorker.getRegistration('/');
  if (!reg) return null;
  return reg.pushManager.getSubscription();
}

async function saveToServer(subscription) {
  // toJSON()은 { endpoint, expirationTime, keys: { p256dh, auth } } 모양이다. 서버가 받는 항목만 보낸다.
  const data = subscription.toJSON();
  await api('/api/push/subscribe', {
    method: 'POST',
    json: { endpoint: data.endpoint, keys: { p256dh: data.keys.p256dh, auth: data.keys.auth } },
  });
}

/**
 * 지금 알림 상태.
 * @returns {Promise<{state: 'on'|'off'|'denied'|'unsupported'|'server-off', message: string}>}
 *   on: 켜져 있음 / off: 꺼져 있음(켤 수 있음) / denied: 브라우저에서 알림을 차단함
 *   unsupported: 이 브라우저·접속 방식으로는 불가 / server-off: 서버에 푸시 설정이 없음
 */
export async function pushStatus() {
  if (!pushSupported()) return { state: 'unsupported', message: unsupportedReason() };
  let cfg = null;
  try {
    cfg = await serverKey();
  } catch {
    cfg = null;     // 서버에 닿지 못하면 아래에서 브라우저 상태만으로 판단한다
  }
  if (cfg && !cfg.enabled) {
    return { state: 'server-off', message: '서버에 푸시 알림이 설정되어 있지 않습니다. 보호자 화면을 열어 두면 경보를 받을 수 있습니다.' };
  }
  if (Notification.permission === 'denied') {
    return {
      state: 'denied',
      message: '이 브라우저에서 알림이 차단되어 있습니다. 주소창 왼쪽 자물쇠 → 권한 → 알림을 "허용"으로 바꿔 주세요.',
    };
  }
  const sub = await existingSubscription();
  if (sub && Notification.permission === 'granted') {
    return { state: 'on', message: '이 휴대폰에서 경보 알림을 받습니다.' };
  }
  return { state: 'off', message: '알림이 꺼져 있습니다. 켜 두면 브라우저를 닫아도 경보 알림이 옵니다.' };
}

/**
 * 알림 켜기. 버튼을 누른 직후(사용자 동작 안)에 불러야 권한 창이 뜬다.
 * 실패하면 이유를 담은 PushError를 던진다.
 */
export async function enablePush() {
  if (!pushSupported()) throw new PushError(unsupportedReason());

  const cfg = await serverKey();
  if (!cfg.enabled || !cfg.key) {
    throw new PushError('서버에 푸시 알림이 설정되어 있지 않아 켤 수 없습니다. 서버 담당자에게 VAPID 키 설정을 요청해 주세요.');
  }

  // 권한을 먼저 묻는다. (서비스 워커 등록을 기다리다 보면 '사용자 동작 직후' 조건이 깨지는 브라우저가 있다)
  const permission = await Notification.requestPermission();
  if (permission !== 'granted') {
    throw new PushError(permission === 'denied'
      ? '알림이 차단되었습니다. 주소창 왼쪽 자물쇠 → 권한 → 알림을 "허용"으로 바꾼 뒤 다시 눌러 주세요.'
      : '알림 권한을 허용해야 경보 알림을 받을 수 있습니다.');
  }

  let reg;
  try {
    reg = await navigator.serviceWorker.register('/sw.js', { scope: '/' });
    await navigator.serviceWorker.ready;
  } catch (err) {
    console.error('서비스 워커 등록 실패', err);
    throw new PushError('알림 기능을 준비하지 못했습니다. 페이지를 새로 고친 뒤 다시 시도해 주세요.');
  }

  const appKey = base64UrlToUint8Array(cfg.key);
  let sub = await reg.pushManager.getSubscription();
  // 서버 키가 바뀌었으면 예전 구독으로는 알림이 오지 않으므로 새로 만든다
  if (sub && !sameKey(sub.options && sub.options.applicationServerKey, appKey)) {
    await sub.unsubscribe().catch(() => {});
    sub = null;
  }
  if (!sub) {
    try {
      sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: appKey });
    } catch (err) {
      console.error('푸시 구독 실패', err);
      throw new PushError('알림 구독에 실패했습니다. 인터넷 연결을 확인하고 다시 시도해 주세요.');
    }
  }

  await saveToServer(sub);   // 실패하면 ApiError (한국어 detail)
  return pushStatus();
}

/**
 * 알림 끄기: 서버에서 구독을 지우고 브라우저 구독도 해제한다.
 * quiet: true면 오류를 던지지 않는다(로그아웃 중에 쓴다).
 */
export async function disablePush({ quiet = false } = {}) {
  try {
    const sub = await existingSubscription();
    if (!sub) return;
    try {
      await api('/api/push/unsubscribe', { method: 'POST', json: { endpoint: sub.endpoint }, noRedirect: true });
    } finally {
      // 서버에서 못 지웠더라도 브라우저 구독을 풀면 그 주소로는 더 이상 알림이 오지 않는다
      await sub.unsubscribe();
    }
  } catch (err) {
    if (!quiet) throw err;
    console.warn('푸시 해제 실패', err);
  }
}

/**
 * 이 브라우저에 구독이 남아 있으면 지금 로그인한 계정으로 서버에 다시 저장한다.
 * 서버가 만료된 구독을 지웠거나 DB를 새로 만든 경우를 바로잡는다. 화면을 열 때 불러도 된다.
 */
export async function syncPush() {
  try {
    if (!pushSupported() || Notification.permission !== 'granted') return;
    const sub = await existingSubscription();
    if (sub) await saveToServer(sub);
  } catch (err) {
    console.warn('푸시 구독 동기화 실패', err);
  }
}
