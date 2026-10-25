// 현장 화면 (/kiosk?device_id=N&key=K) - Pi에 붙은 화면에 Chromium 키오스크 모드로 띄운다.
//
// 로그인 대신 Pi의 기기 키로 서버에 묻는다. 1초마다 GET /api/devices/{id}/state 를 불러
// 정상(초록) / 확인 중(주황) / 경보(빨강) / 연결 문제(회색)를 화면 전체로 보여 주고,
// 확인 중·경보 때는 큰 '괜찮아요' 버튼으로 이벤트를 끝낸다.
//
// 안내 음성 재생과 녹음은 Pi 프로그램(pi_client)이 스피커·마이크로 한다. 이 화면은 보여 주기와 버튼만 맡는다.
//
// 기기 키는 비밀번호와 같다. 주소창에 남으면 화면을 본 사람이 옮겨 적을 수 있으므로
// 읽자마자 주소에서 지운다. 대신 이 탭의 sessionStorage에만 둔다: 화면이 새로 고쳐져도
// (Chromium 오류 뒤 자동 새로 고침, F5 등) 키 없이 '설정 필요'에 멈추지 않게 하려는 것이다.
// sessionStorage는 탭을 닫으면 지워지고 다른 탭과 나누지 않는다. localStorage에는 저장하지 않는다.

import { api, ApiError } from './api.js';
import { $, show, hide, formatTime } from './ui.js';

const POLL_MS = 1000;
const REQUEST_TIMEOUT_MS = 4000;
// 이 횟수만큼 연속으로 서버에 닿지 못하면 '서버 연결 끊김'으로 바꾼다(한 번 늦은 응답으로 깜빡이지 않게)
const FAILS_BEFORE_OFFLINE = 3;

const PROMPT_TEXT = "괜찮으신가요? 괜찮으시면 '괜찮아요'라고 말씀하시거나 아래 버튼을 눌러 주세요.";

const els = {
  title: $('#k-title'),
  message: $('#k-message'),
  ok: $('#k-ok'),
  feedback: $('#k-feedback'),
  device: $('#k-device'),
  clock: $('#k-clock'),
};

// --- 주소에서 기기 번호·키 읽기 -----------------------------------------------------

const params = new URLSearchParams(location.search);
const deviceId = Number(params.get('device_id'));
const KEY_STORAGE = `fall.kiosk.key.${deviceId}`;   // 기기마다 따로

function loadKey() {
  const fromUrl = params.get('key') || '';
  try {
    if (fromUrl) {
      sessionStorage.setItem(KEY_STORAGE, fromUrl);
      return fromUrl;
    }
    return sessionStorage.getItem(KEY_STORAGE) || '';
  } catch {
    return fromUrl;   // 저장소를 못 쓰면 이번 화면에서만 쓴다
  }
}

function forgetKey() {
  try { sessionStorage.removeItem(KEY_STORAGE); } catch { /* 무시 */ }
}

const deviceKey = loadKey();

if (params.has('key')) {
  // 키만 지우고 device_id는 남긴다(어느 기기 화면인지 알 수 있게)
  params.delete('key');
  const query = params.toString();
  history.replaceState(null, '', location.pathname + (query ? '?' + query : '') + location.hash);
}

// --- 상태 -------------------------------------------------------------------------

let activeEvent = null;     // 서버가 알려 준 진행 중 이벤트
let failures = 0;           // 연속 실패 횟수
let stopped = false;        // 키가 틀리는 등 다시 물어도 소용없을 때
let clearing = false;       // '괜찮아요' 요청 중
let pollTimer = null;
let lastShown = '';         // 같은 화면을 매번 다시 그리지 않으려고
let feedbackTimer = null;

// --- 화면 그리기 --------------------------------------------------------------------

/**
 * @param {'normal'|'suspect'|'alarm'|'offline'|'error'|'loading'} kind  배경색
 * @param {string} title  큰 제목
 * @param {string} message  설명
 * @param {boolean} withButton  '괜찮아요' 버튼을 보일지
 */
function render(kind, title, message, withButton) {
  const key = [kind, title, message, withButton].join('|');
  if (key === lastShown) return;
  lastShown = key;
  // 확인 중·경보로 새로 바뀌면 앞 상황의 안내('괜찮다고 전했습니다' 등)는 지운다
  if ((kind === 'suspect' || kind === 'alarm') && !document.body.classList.contains(`state-${kind}`)) {
    setFeedback('');
  }
  document.body.className = `kiosk state-${kind}`;
  els.title.textContent = title;
  els.message.textContent = message;
  if (withButton) {
    show(els.ok);
    els.ok.disabled = clearing;
  } else {
    hide(els.ok);
  }
}

function setFeedback(text, ms = 4000) {
  clearTimeout(feedbackTimer);
  els.feedback.textContent = text;
  if (text && ms) feedbackTimer = setTimeout(() => { els.feedback.textContent = ''; }, ms);
}

// 서버가 준 {device, active_event}로 화면을 정한다. 진행 중 이벤트가 가장 중요하므로 먼저 본다.
function renderState(data) {
  const device = data.device || {};
  activeEvent = data.active_event || null;
  els.device.textContent = [device.owner_name, device.name].filter(Boolean).join(' · ');

  const offlineNote = device.online ? '' : ' (카메라 연결이 끊겨 있습니다)';
  if (activeEvent && activeEvent.state === 'ALARM') {
    render('alarm', '경보', '보호자에게 알렸습니다. 괜찮으시면 아래 버튼을 눌러 주세요.' + offlineNote, true);
  } else if (activeEvent && activeEvent.state === 'SUSPECT') {
    render('suspect', '상태 확인 중', PROMPT_TEXT, true);
  } else if (!device.online) {
    render('offline', '카메라 응답 없음',
      '카메라 프로그램이 서버에 연결되어 있지 않아 감지하지 못하고 있습니다. Pi 전원과 카메라 연결을 확인해 주세요.', false);
  } else if (!device.camera_ok) {
    render('offline', '카메라 응답 없음',
      '카메라 영상이 들어오지 않아 감지하지 못하고 있습니다. 카메라 연결을 확인해 주세요.', false);
  } else {
    render('normal', '정상 감시 중', '넘어짐을 살피고 있습니다.', false);
  }
}

// --- 서버에 묻기 -------------------------------------------------------------------

function authHeaders() {
  return { 'X-Device-Key': deviceKey };
}

async function poll() {
  if (stopped) return;
  const ctrl = new AbortController();
  const timeout = setTimeout(() => ctrl.abort(), REQUEST_TIMEOUT_MS);
  try {
    const data = await api(`/api/devices/${deviceId}/state`, {
      headers: authHeaders(), noRedirect: true, signal: ctrl.signal,
    });
    failures = 0;
    renderState(data);
  } catch (err) {
    handlePollError(err);
  } finally {
    clearTimeout(timeout);
    // 앞 요청이 끝난 뒤에 다음을 예약한다(서버가 느릴 때 요청이 겹겹이 쌓이지 않게)
    if (!stopped) pollTimer = setTimeout(poll, POLL_MS);
  }
}

function handlePollError(err) {
  const status = err instanceof ApiError ? err.status : 0;
  if (status === 401 || status === 403 || status === 404) {
    // 키가 틀렸거나 마이페이지에서 기기를 지웠다. 다시 물어도 같으므로 멈춘다.
    // 쓸모없어진 키는 이 탭에서도 지운다.
    stopped = true;
    forgetKey();
    activeEvent = null;
    render('error', '기기 확인 실패',
      '기기 번호나 기기 키가 맞지 않거나 기기가 삭제되었습니다. 마이페이지에서 기기를 확인하고 Pi 설정을 고친 뒤 화면을 다시 실행해 주세요.',
      false);
    return;
  }
  failures += 1;
  if (failures >= FAILS_BEFORE_OFFLINE) {
    // 서버에 닿지 못하면 지금 상태를 알 수 없다. 오래된 '정상'을 계속 보여 주면 위험하므로 회색으로 바꾼다.
    activeEvent = null;
    render('offline', '서버 연결 끊김',
      '서버에 연결할 수 없어 지금은 감지하지 못하고 있습니다. 와이파이와 서버 컴퓨터를 확인해 주세요. 자동으로 다시 연결합니다.',
      false);
  }
}

// --- '괜찮아요' 버튼 ---------------------------------------------------------------

async function onOk() {
  if (clearing || !activeEvent) return;
  clearing = true;
  els.ok.disabled = true;
  const eventId = activeEvent.id;
  try {
    await api(`/api/events/${eventId}/clear`, { method: 'POST', headers: authHeaders(), noRedirect: true });
    setFeedback('괜찮다고 전했습니다.');
  } catch (err) {
    const status = err instanceof ApiError ? err.status : 0;
    if (status === 409 || status === 404) {
      setFeedback('이미 끝난 알림입니다.');
    } else {
      setFeedback('전하지 못했습니다. 다시 눌러 주세요.', 6000);
    }
  } finally {
    clearing = false;
    els.ok.disabled = false;
    // 결과를 바로 반영한다(다음 1초를 기다리지 않게)
    clearTimeout(pollTimer);
    lastShown = '';
    poll();
  }
}

// --- 시계 ---------------------------------------------------------------------------
// 화면이 멈추지 않았다는 것을 보여 주는 용도이기도 하다.

function pad(n) {
  return String(n).padStart(2, '0');
}

function tickClock() {
  const now = new Date();
  const iso = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}`;
  els.clock.textContent = formatTime(iso);
}

// --- 시작 ---------------------------------------------------------------------------

function start() {
  tickClock();
  setInterval(tickClock, 10000);
  els.ok.addEventListener('click', onOk);
  // 키오스크에서 길게 누르면 뜨는 메뉴를 막는다(실수로 다른 화면으로 가지 않게)
  document.addEventListener('contextmenu', (event) => event.preventDefault());

  if (!Number.isInteger(deviceId) || deviceId <= 0 || !deviceKey) {
    stopped = true;
    render('error', '설정 필요',
      '주소에 기기 번호(device_id)와 기기 키(key)가 없습니다. Pi에서 현장 화면을 다시 실행해 주세요.', false);
    return;
  }
  render('loading', '연결하는 중…', '서버에 연결하고 있습니다.', false);
  poll();
}

start();
