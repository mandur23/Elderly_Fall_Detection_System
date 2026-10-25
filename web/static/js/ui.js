// 화면 공통 도우미: 요소 찾기, 보이기/숨기기, 알림 문구(toast), 시각 표시, 상태·결과 한국어 이름, 머리글 메뉴.
//
// 보안 규칙: 서버에서 받은 글(이름, 이메일, 기기 이름 등)은 반드시 textContent로 넣는다.
// innerHTML에 넣으면 이름에 <script> 같은 글을 넣어 다른 사람 화면에서 코드를 실행할 수 있다(XSS).
// 그래서 요소를 만들 때는 아래 el()을 쓴다.

import { me, logout } from './api.js';

export function $(sel, root = document) {
  return root.querySelector(sel);
}

export function $$(sel, root = document) {
  return Array.from(root.querySelectorAll(sel));
}

function toElement(target) {
  return typeof target === 'string' ? $(target) : target;
}

export function show(target) {
  const node = toElement(target);
  if (node) node.classList.remove('hidden');
}

export function hide(target) {
  const node = toElement(target);
  if (node) node.classList.add('hidden');
}

// visible이 참이면 보이고 거짓이면 숨긴다
export function toggle(target, visible) {
  if (visible) show(target); else hide(target);
}

/**
 * 요소를 안전하게 만든다. text는 textContent로 들어가므로 서버 글을 그대로 넣어도 된다.
 *   el('li', { class: 'list-item' }, [el('span', { text: user.name })])
 * attrs: class, text, 그 밖의 속성(type, href, aria-label, data-id ...), on: { click: fn }
 */
export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = String(value);
    else if (key === 'on') {
      for (const [type, fn] of Object.entries(value)) node.addEventListener(type, fn);
    } else node.setAttribute(key, value === true ? '' : String(value));
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

// --- 알림 문구 ------------------------------------------------------------------

function toastArea() {
  let area = $('#toast-area');
  if (!area) {
    area = el('div', { id: 'toast-area', class: 'toast-area', role: 'status', 'aria-live': 'polite' });
    document.body.append(area);
  }
  return area;
}

/**
 * 화면 아래에 잠깐 문구를 띄운다.
 * @param {string} msg
 * @param {'info'|'success'|'error'|'warn'} kind  오류는 읽을 시간이 더 필요하므로 오래 남긴다
 * @param {number} [ms]  남겨 둘 시간. 긴 안내 문구는 더 길게 준다
 */
export function toast(msg, kind = 'info', ms = kind === 'error' ? 7000 : 3500) {
  const area = toastArea();
  const item = el('div', { class: `toast toast-${kind}`, text: msg });
  if (kind === 'error') item.setAttribute('role', 'alert');
  item.addEventListener('click', () => item.remove());   // 눌러서 바로 닫을 수도 있다
  area.append(item);
  // 너무 많이 쌓이지 않게 오래된 것부터 지운다
  while (area.children.length > 3) area.firstElementChild.remove();
  setTimeout(() => item.remove(), ms);
}

// 오류 객체(ApiError 등)에서 보여 줄 문장을 꺼낸다
export function errorText(err) {
  if (!err) return '알 수 없는 오류가 났습니다.';
  return err.detail || err.message || String(err);
}

// --- 시각 ---------------------------------------------------------------------

function pad(n) {
  return String(n).padStart(2, '0');
}

/**
 * 서버 시각('2026-10-25T14:03:12.345', 서버 PC 기준 현지 시각)을 '10월 25일 14:03'으로.
 * 서버(notify.format_time)와 같은 모양을 써서 푸시 알림과 화면의 시각이 같게 보이게 한다.
 * withSeconds: true면 '14:03:12'까지
 */
export function formatTime(iso, { withSeconds = false } = {}) {
  if (!iso) return '';
  const m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?/.exec(String(iso));
  if (!m) return String(iso);
  const [, , month, day, hour, minute, second] = m;
  const time = `${hour}:${minute}` + (withSeconds && second ? `:${second}` : '');
  return `${Number(month)}월 ${Number(day)}일 ${time}`;
}

// --- 상태·결과 이름 -------------------------------------------------------------

// 이벤트 결과 (server/db.py의 결과 상수)
export const RESULT_LABELS = {
  CLEARED_VOICE: '음성으로 괜찮다고 응답',
  CLEARED_BUTTON: '버튼으로 해제',
  ALERT_HELP: '도움 요청',
  ALERT_NO_REPLY: '응답 없음',
  ALERT_VOICE_FAIL: '음성 확인 불가',
  INTERRUPTED: '서버 재시작으로 중단',
};

// 기기 감지 상태 (서버가 보내는 state)
export const STATE_LABELS = {
  NORMAL: '정상 감시 중',
  SUSPECT: '상태 확인 중',
  ALARM: '경보',
};

export function resultLabel(result) {
  if (!result) return '';
  return RESULT_LABELS[result] || result;
}

// 연결이 없으면 상태와 상관없이 '연결 없음'으로 보여 준다(오래된 상태를 믿으면 위험하다)
export function stateLabel(state, { online = true } = {}) {
  if (!online) return '연결 없음';
  return STATE_LABELS[state] || '알 수 없음';
}

// 상태 배지 클래스: badge-normal / badge-suspect / badge-alarm / badge-offline
export function stateClass(state, { online = true } = {}) {
  if (!online) return 'badge-offline';
  if (state === 'NORMAL') return 'badge-normal';
  if (state === 'SUSPECT') return 'badge-suspect';
  if (state === 'ALARM') return 'badge-alarm';
  return 'badge-offline';
}

// 상태 배지 요소를 만든다
export function stateBadge(state, opts = {}) {
  return el('span', { class: `badge ${stateClass(state, opts)}`, text: stateLabel(state, opts) });
}

// --- 머리글 메뉴 ----------------------------------------------------------------

const NAV_LINKS = [
  { href: '/mypage', label: '마이페이지' },
  { href: '/guardian', label: '보호자 화면' },
  { href: '/camera', label: '카메라' },
];

/**
 * 화면 맨 위 머리글을 그린다. 페이지에 <header class="site-header"></header>가 있으면 그 안에, 없으면 body 맨 앞에 만든다.
 * 로그인했으면 마이페이지·보호자 화면·카메라·로그아웃, 아니면 로그인·회원가입을 보여 준다.
 * @returns {Promise<object|null>} me()의 결과(로그인 안 했으면 null). 각 화면이 다시 부르지 않고 그대로 쓰면 된다.
 */
export async function renderNav() {
  let header = $('header.site-header');
  if (!header) {
    header = el('header', { class: 'site-header' });
    document.body.prepend(header);
  }
  let info = null;
  try {
    info = await me();
  } catch (err) {
    // 서버에 닿지 못해도 화면은 그린다(메뉴만 로그인 전 모양으로)
    console.warn('내 정보를 받지 못함', err);
  }

  const nav = el('nav', { class: 'site-nav', 'aria-label': '메뉴' });
  const here = location.pathname;
  const link = (href, label) =>
    el('a', { href, text: label, 'aria-current': here === href ? 'page' : null });

  if (info && info.user) {
    for (const item of NAV_LINKS) nav.append(link(item.href, item.label));
    const out = el('button', { type: 'button', text: '로그아웃' });
    out.addEventListener('click', async () => {
      out.disabled = true;
      try {
        await logout();
      } catch (err) {
        out.disabled = false;
        toast('로그아웃하지 못했습니다. ' + errorText(err), 'error');
      }
    });
    nav.append(out);
  } else {
    nav.append(link('/login', '로그인'), link('/signup', '회원가입'));
  }

  const bar = el('div', { class: 'bar' }, [
    el('a', { class: 'brand', href: '/', text: '낙상 감지' }),
    nav,
  ]);
  header.replaceChildren(bar);
  return info;
}
