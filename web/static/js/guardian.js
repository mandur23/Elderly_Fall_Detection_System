// 보호자 화면 (/guardian)
//
// 하는 일
// 1) /ws/guardian에 접속해 처음에 snapshot(기기 상태 전체 + 진행 중 이벤트)을 받고,
//    그 뒤로 오는 device(기기 상태 바뀜) / event(이벤트 생김·바뀜·끝남) 메시지를 화면에 반영한다.
// 2) 새 경보(ALARM)가 오면 확인할 때까지 큰 소리(WebAudio)·진동·제목 깜빡임으로 알린다.
// 3) 진행 중인 경보는 확인 뒤 '경보 해제'(POST /api/events/{id}/clear)할 수 있다.
// 4) 최근 기록(GET /api/events)과 휴대폰 알림(웹 푸시) 켜기를 보여 준다.
//
// 화면의 상태는 늘 서버가 보낸 값으로만 그린다. 연결이 끊긴 동안에는 '끊김' 띠를 띄우고 흐리게 보여 주어
// 오래된 상태를 현재 상태로 믿지 않게 한다. 다시 연결되면 서버가 snapshot을 새로 보내 주므로
// 끊긴 동안 놓친 변화도 그것으로 바로잡힌다.

import { api, me, ApiError, goLogin } from './api.js';
import { renderNav, $, show, hide, toggle, el, toast, errorText, formatTime, resultLabel } from './ui.js';
import { connectSocket } from './ws.js';
import { pushStatus, enablePush, disablePush, syncPush } from './push.js';

// --- 화면 상태 --------------------------------------------------------------------

const state = {
  info: null,              // me() 결과 {user, guardians, wards, requests}
  devices: new Map(),      // device_id -> 기기 상태(server/devices.py status())
  active: new Map(),       // event_id -> 진행 중 이벤트(state SUSPECT 또는 ALARM)
  connected: false,        // snapshot을 받은 뒤부터 true
  stopped: false,          // 다시 연결하지 않는 이유로 닫힘(로그인 만료 등)
};

const KIND_LABELS = { pi: '라즈베리파이 카메라', phone: '휴대폰 카메라' };

// '확인했어요'를 누른 경보 id. 새로 고침해도 같은 경보로 다시 울리지 않게 이 탭(sessionStorage)에만 기억한다.
// (민감한 정보가 아니고, 저장이 안 되는 브라우저에서도 화면은 그대로 동작해야 하므로 try로 감싼다)
const ACK_KEY = 'guardian-acked-alarms';
const acked = new Set(loadAcked());

function loadAcked() {
  try {
    const raw = sessionStorage.getItem(ACK_KEY);
    const list = raw ? JSON.parse(raw) : [];
    return Array.isArray(list) ? list.filter(Number.isInteger) : [];
  } catch {
    return [];
  }
}

function saveAcked() {
  try {
    sessionStorage.setItem(ACK_KEY, JSON.stringify([...acked]));
  } catch { /* 저장할 수 없어도 이 화면이 열려 있는 동안은 기억한다 */ }
}

// --- 경보 소리·진동·제목 깜빡임 ---------------------------------------------------
//
// 브라우저는 사용자가 화면을 한 번 누르기 전에는 소리를 내지 못하게 한다(자동 재생 정책).
// 그래서 '알림 소리 켜기' 버튼(또는 화면 아무 곳 누르기)으로 AudioContext를 먼저 깨워 둔다.
// 파일을 받지 않고 WebAudio로 직접 사이렌 소리를 만들어서 오프라인에서도, 소리 파일이 없어도 울린다.

const alarm = {
  ctx: null,
  unlocked: false,
  soundTimer: null,
  titleTimer: null,
  cycles: 0,
  baseTitle: document.title,
};

function audioContextClass() {
  return window.AudioContext || window.webkitAudioContext || null;
}

// 사용자 동작 안에서 불러야 한다. 성공하면 true.
function unlockSound() {
  const AC = audioContextClass();
  if (!AC) return false;
  try {
    if (!alarm.ctx) alarm.ctx = new AC();
    if (alarm.ctx.state === 'suspended') alarm.ctx.resume().catch(() => {});
    alarm.unlocked = true;
  } catch (err) {
    console.warn('소리를 켜지 못함', err);
    return false;
  }
  renderSoundBox();
  updateAlarm();      // 이미 울려야 할 경보가 있으면 바로 소리를 낸다
  return true;
}

// 소리 하나: 주파수 freq로 duration초. 시작·끝을 부드럽게 해서 '딱' 소리가 나지 않게 한다.
function tone(freq, start, duration, volume) {
  const ctx = alarm.ctx;
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.type = 'square';            // 사각파는 작은 휴대폰 스피커에서도 잘 들린다
  osc.frequency.setValueAtTime(freq, start);
  gain.gain.setValueAtTime(0.0001, start);
  gain.gain.exponentialRampToValueAtTime(volume, start + 0.02);
  gain.gain.setValueAtTime(volume, start + duration - 0.03);
  gain.gain.exponentialRampToValueAtTime(0.0001, start + duration);
  osc.connect(gain).connect(ctx.destination);
  osc.start(start);
  osc.stop(start + duration + 0.01);
}

// 높고 낮은 소리를 번갈아 내는 사이렌 한 번(약 1.2초)
function playSiren() {
  if (!alarm.ctx) return;
  if (alarm.ctx.state === 'suspended') alarm.ctx.resume().catch(() => {});
  const t = alarm.ctx.currentTime + 0.02;
  for (let i = 0; i < 4; i += 1) tone(i % 2 ? 660 : 990, t + i * 0.3, 0.28, 0.5);
}

// 소리 켜기를 누르면 짧게 '띵' 하고 울려 실제로 소리가 나는지 알 수 있게 한다
function playChime() {
  if (!alarm.ctx) return;
  const t = alarm.ctx.currentTime + 0.02;
  tone(880, t, 0.15, 0.25);
  tone(1320, t + 0.17, 0.2, 0.25);
}

// 진동: 브라우저는 사용자가 화면을 한 번이라도 누른 뒤에만 진동을 허용한다(누르기 전에 부르면 콘솔 오류만 남는다).
function vibrate(pattern) {
  try {
    const activated = !navigator.userActivation || navigator.userActivation.hasBeenActive;
    if (navigator.vibrate && activated) navigator.vibrate(pattern);
  } catch { /* 진동이 없는 기기 */ }
}

// 확인하지 않은 경보 목록
function unackedAlarms() {
  return [...state.active.values()].filter((ev) => ev.state === 'ALARM' && !acked.has(ev.id));
}

function startRinging() {
  if (!alarm.soundTimer) {
    alarm.cycles = 0;
    const cycle = () => {
      if (alarm.unlocked) playSiren();
      // 진동은 2번에 1번만: 계속 떨면 오히려 알아채기 어렵고 배터리도 닳는다
      if (alarm.cycles % 2 === 0) vibrate([600, 200, 600, 200, 600]);
      alarm.cycles += 1;
    };
    cycle();
    alarm.soundTimer = setInterval(cycle, 1500);
  }
  if (!alarm.titleTimer) {
    let flip = false;
    alarm.titleTimer = setInterval(() => {
      flip = !flip;
      document.title = flip ? '[경보] 확인이 필요합니다' : alarm.baseTitle;
    }, 1000);
    document.title = '[경보] 확인이 필요합니다';
  }
}

function stopRinging() {
  if (!alarm.soundTimer && !alarm.titleTimer) return;   // 울리고 있지 않음
  clearInterval(alarm.soundTimer);
  clearInterval(alarm.titleTimer);
  alarm.soundTimer = null;
  alarm.titleTimer = null;
  document.title = alarm.baseTitle;
  vibrate(0);
}

// 경보 띠와 소리를 지금 상태에 맞춘다. 상태가 바뀔 때마다 부른다.
function updateAlarm() {
  const list = unackedAlarms();
  const bar = $('#alarm-bar');
  if (list.length === 0) {
    hide(bar);
    stopRinging();
    return;
  }
  const first = list[0];
  const who = first.owner_name || '보호하는 분';
  const where = first.device_name ? ` (${first.device_name})` : '';
  const more = list.length > 1 ? ` 외 ${list.length - 1}건` : '';
  $('#alarm-bar-text').textContent = `경보! ${who}님${where}${more} - 바로 확인해 주세요.`;
  show(bar);
  startRinging();
}

function acknowledgeAlarms() {
  for (const ev of unackedAlarms()) acked.add(ev.id);
  saveAcked();
  updateAlarm();
  toast('소리를 껐습니다. 경보는 해제할 때까지 화면에 남아 있습니다.', 'info');
}

// 끝난 이벤트의 '확인함' 기록은 지운다(쌓이지 않게)
function pruneAcked() {
  let changed = false;
  for (const id of [...acked]) {
    if (!state.active.has(id)) { acked.delete(id); changed = true; }
  }
  if (changed) saveAcked();
}

function renderSoundBox() {
  toggle('#sound-box', !alarm.unlocked);
  toggle('#sound-on', alarm.unlocked);
}

// --- 이름·상태 표시 도우미 ---------------------------------------------------------

function myId() {
  return state.info && state.info.user ? state.info.user.id : null;
}

function ownerLabel(ownerId, ownerName) {
  if (ownerId === myId()) return '나';
  return ownerName ? `${ownerName}님` : '이름 모름';
}

function activeEventForDevice(deviceId) {
  for (const ev of state.active.values()) if (ev.device_id === deviceId) return ev;
  return null;
}

/**
 * 기기 카드의 배지(글, 색)와 설명 한 줄.
 * 진행 중인 이벤트가 있으면 연결이 끊겼어도 그 상태(확인 중·경보)를 먼저 보여 준다.
 * 경보 중에 기기 연결이 끊길 수도 있는데, 그때 '연결 없음'만 보이면 경보를 놓치기 때문이다.
 */
function deviceLook(d) {
  const ev = activeEventForDevice(d.device_id);
  const st = ev ? ev.state : d.state;
  if (st === 'ALARM') return { look: 'alarm', badge: 'badge-alarm', label: '경보' };
  if (st === 'SUSPECT') return { look: 'suspect', badge: 'badge-suspect', label: '상태 확인 중' };
  if (!d.online) return { look: 'offline', badge: 'badge-offline', label: '연결 없음' };
  if (!d.camera_ok) return { look: 'offline', badge: 'badge-offline', label: '카메라 응답 없음' };
  // NORMAL, FALLING(넘어지는 동작을 잠깐 본 상태. 누워 있는 것이 확인되면 SUSPECT가 된다)
  return { look: 'normal', badge: 'badge-normal', label: '정상 감시 중' };
}

function deviceDetail(d) {
  if (!d.online) return '기기가 꺼져 있거나 인터넷이 끊겼습니다. 지금은 감시하지 못합니다.';
  if (!d.camera_ok) return '서버와 연결은 되어 있지만 카메라 영상이 오지 않습니다.';
  return d.person_detected ? '카메라 정상 · 사람이 보입니다' : '카메라 정상 · 지금은 사람이 보이지 않습니다';
}

// 이벤트 상태 설명
function eventStatusText(ev) {
  if (ev.state === 'SUSPECT') return '넘어진 것 같아 음성으로 괜찮은지 확인하고 있습니다.';
  if (ev.state === 'ALARM') {
    const why = resultLabel(ev.result);
    return why ? `경보 이유: ${why}` : '경보가 울렸습니다.';
  }
  return '';
}

// 기록 목록의 결과 글
function historyResult(ev) {
  if (ev.state === 'SUSPECT') return { text: '상태 확인 중', badge: 'badge-suspect' };
  if (ev.state === 'ALARM') return { text: `경보 중 · ${resultLabel(ev.result) || '확인 필요'}`, badge: 'badge-alarm' };
  const label = resultLabel(ev.result) || '끝남';
  if (ev.result && ev.result.startsWith('ALERT_')) return { text: `${label} → 경보 해제됨`, badge: 'badge-offline' };
  if (ev.result === 'INTERRUPTED') return { text: label, badge: 'badge-offline' };
  return { text: label, badge: 'badge-normal' };
}

// --- 그리기 -----------------------------------------------------------------------

function renderActiveEvents() {
  const box = $('#active-events');
  // 경보를 확인 중보다 위에, 같은 종류끼리는 먼저 생긴 것을 위에
  const list = [...state.active.values()].sort((a, b) =>
    (a.state === 'ALARM' ? 0 : 1) - (b.state === 'ALARM' ? 0 : 1) || a.id - b.id);
  box.replaceChildren(...list.map(eventCard));
}

function eventCard(ev) {
  const isAlarm = ev.state === 'ALARM';
  const button = el('button', {
    class: isAlarm ? 'btn btn-danger btn-big btn-block' : 'btn btn-secondary btn-block',
    type: 'button',
    text: isAlarm ? '경보 해제' : '괜찮은 것을 확인함 (해제)',
    'data-action': 'clear',
    on: { click: () => clearEvent(ev.id, button) },
  });
  return el('article', { class: 'event-card', 'data-event-id': ev.id, 'data-state': ev.state }, [
    el('div', { class: 'head' }, [
      el('span', { class: 'who', text: ownerLabel(ev.owner_id, ev.owner_name) }),
      el('span', { class: `badge ${isAlarm ? 'badge-alarm' : 'badge-suspect'}`, text: isAlarm ? '경보' : '상태 확인 중' }),
    ]),
    el('p', { class: 'meta', text: `${ev.device_name || '기기'} · ${formatTime(ev.started_at, { withSeconds: true })}` }),
    el('p', { class: 'explain', text: eventStatusText(ev) }),
    isAlarm
      ? el('p', { class: 'small', text: '먼저 전화 등으로 상황을 확인하세요. 위급하면 119에 신고해 주세요.' })
      : null,
    button,
  ]);
}

// 사람(주인)별로 묶는다: 내 기기 → 마이페이지의 '내가 보호하는 사람' 순서 → 그 밖(방금 연결된 분 등)
function ownerGroups() {
  const groups = new Map();   // owner_id -> {name, devices: []}
  const me = myId();
  const wards = (state.info && state.info.wards) || [];
  const hasMine = [...state.devices.values()].some((d) => d.owner_id === me);
  if (hasMine) groups.set(me, { name: '내 기기', isMe: true, devices: [] });
  for (const w of wards) {
    if (w.user && w.user.id != null && !groups.has(w.user.id)) {
      groups.set(w.user.id, { name: `${w.user.name || w.user.email}님`, devices: [] });
    }
  }
  const devices = [...state.devices.values()].sort((a, b) => a.device_id - b.device_id);
  for (const d of devices) {
    if (!groups.has(d.owner_id)) groups.set(d.owner_id, { name: ownerLabel(d.owner_id, d.owner_name), devices: [] });
    groups.get(d.owner_id).devices.push(d);
  }
  return groups;
}

function renderDevices() {
  const box = $('#devices');
  const groups = ownerGroups();
  const wards = (state.info && state.info.wards) || [];

  // 보호하는 분이 없으면 연결 방법을 안내한다
  const noWards = state.info && wards.length === 0;
  toggle('#empty-help', noWards);
  if (state.info) {
    $('#my-email').textContent = state.info.user.email;
    const pending = (state.info.requests || []).length;
    const note = $('#pending-note');
    if (pending > 0) {
      note.replaceChildren(
        `받은 보호자 요청이 ${pending}건 있습니다. `,
        el('a', { href: '/mypage', text: '마이페이지에서 수락하기 →' }),
      );
      show(note);
    } else {
      hide(note);
    }
  }

  if (!state.connected && state.devices.size === 0) {
    box.replaceChildren(el('p', { class: 'empty', text: '서버에서 상태를 받는 중입니다…' }));
    return;
  }
  if (groups.size === 0) {
    box.replaceChildren(el('p', { class: 'empty', text: '보여 줄 기기가 없습니다.' }));
    return;
  }

  const nodes = [];
  for (const [ownerId, group] of groups) {
    const items = group.devices.map(deviceCard);
    if (items.length === 0) {
      items.push(el('p', { class: 'empty', text:
        '등록된 기기가 없습니다. 그분이 카메라 화면을 열거나 Pi 기기를 추가하면 여기에 보입니다.' }));
    }
    nodes.push(el('div', { class: 'owner-group', 'data-owner-id': ownerId }, [
      el('h3', { text: group.name }),
      ...items,
    ]));
  }
  box.replaceChildren(...nodes);
}

function deviceCard(d) {
  const look = deviceLook(d);
  const kind = KIND_LABELS[d.kind] || '카메라';
  return el('div', {
    class: 'device-card',
    'data-device-id': d.device_id,
    'data-look': look.look,
    'data-state': d.state,
    'data-online': d.online ? '1' : '0',
  }, [
    el('div', { class: 'head' }, [
      el('span', { class: 'name', text: d.name }),
      el('span', { class: `badge ${look.badge}`, text: look.label }),
    ]),
    el('div', { class: 'sub', text: `${kind} · ${ownerLabel(d.owner_id, d.owner_name)}` }),
    el('div', { class: 'sub', text: deviceDetail(d) }),
  ]);
}

function renderAll() {
  renderActiveEvents();
  renderDevices();
  updateAlarm();
}

// --- 최근 기록 ---------------------------------------------------------------------

let historyTimer = null;
let historyBusy = false;
let historyAgain = false;

// 이벤트 메시지가 몰려 와도 기록은 한 번만 다시 받는다
function scheduleHistory(delay = 400) {
  clearTimeout(historyTimer);
  historyTimer = setTimeout(loadHistory, delay);
}

async function loadHistory() {
  if (historyBusy) { historyAgain = true; return; }
  historyBusy = true;
  try {
    const data = await api('/api/events?limit=30');
    renderHistory((data && data.events) || []);
  } catch (err) {
    if (!(err instanceof ApiError && err.status === 401)) {
      $('#history').replaceChildren(el('li', { class: 'empty', text: '기록을 불러오지 못했습니다. ' + errorText(err) }));
    }
  } finally {
    historyBusy = false;
    if (historyAgain) { historyAgain = false; scheduleHistory(0); }
  }
}

function renderHistory(events) {
  const list = $('#history');
  if (events.length === 0) {
    list.replaceChildren(el('li', { class: 'empty', text: '아직 기록이 없습니다.' }));
    return;
  }
  list.replaceChildren(...events.map((ev) => {
    const r = historyResult(ev);
    const ended = ev.ended_at ? ` · 끝난 시각 ${formatTime(ev.ended_at)}` : '';
    return el('li', { class: 'list-item history-item', 'data-event-id': ev.id, 'data-state': ev.state }, [
      el('div', { class: 'grow' }, [
        el('div', { class: 'when', text: formatTime(ev.started_at) }),
        el('div', { class: 'sub', text: `${ownerLabel(ev.owner_id, ev.owner_name)} · ${ev.device_name || '기기'}${ended}` }),
      ]),
      el('span', { class: `badge ${r.badge}`, text: r.text }),
    ]);
  }));
}

// --- 경보 해제 ---------------------------------------------------------------------

async function clearEvent(eventId, button) {
  const ev = state.active.get(eventId);
  if (!ev) return;
  const who = ownerLabel(ev.owner_id, ev.owner_name);
  const question = ev.state === 'ALARM'
    ? `${who}의 경보를 해제할까요?\n\n전화 등으로 괜찮은 것을 확인한 뒤에 해제해 주세요. 해제하면 다른 보호자의 경보도 함께 꺼집니다.`
    : `${who}의 상태를 확인하셨나요? 괜찮으면 해제해 주세요.\n\n해제하면 음성 확인을 멈추고 경보를 울리지 않습니다.`;
  if (!window.confirm(question)) return;

  button.disabled = true;
  try {
    const res = await api(`/api/events/${eventId}/clear`, { method: 'POST' });
    if (res && res.event) applyEvent(res.event);
    else { state.active.delete(eventId); renderAll(); }
    toast('해제했습니다.', 'success');
  } catch (err) {
    if (err instanceof ApiError && (err.status === 409 || err.status === 404)) {
      // 다른 보호자나 현장에서 이미 해제했다
      state.active.delete(eventId);
      pruneAcked();
      renderAll();
      toast('이미 해제된 상황입니다.', 'info');
    } else {
      button.disabled = false;
      toast('해제하지 못했습니다. ' + errorText(err), 'error');
    }
  }
  scheduleHistory(0);
}

// --- 서버 메시지 -------------------------------------------------------------------

let meTimer = null;

// 모르는 분(방금 연결된 피보호자 등)의 기기가 보이면 내 정보(wards)를 다시 받는다
function scheduleMeRefresh() {
  clearTimeout(meTimer);
  meTimer = setTimeout(refreshMe, 300);
}

async function refreshMe() {
  try {
    const info = await me({ force: true });
    if (!info) { goLogin(); return; }
    state.info = info;
    renderDevices();
    renderActiveEvents();
  } catch (err) {
    console.warn('내 정보를 다시 받지 못함', err);
  }
}

function isKnownOwner(ownerId) {
  if (ownerId === myId()) return true;
  const wards = (state.info && state.info.wards) || [];
  return wards.some((w) => w.user && w.user.id === ownerId);
}

function applyEvent(ev) {
  if (!ev || typeof ev.id !== 'number') return;
  if (ev.state === 'ENDED') {
    state.active.delete(ev.id);
    pruneAcked();
  } else {
    const before = state.active.get(ev.id);
    state.active.set(ev.id, ev);
    if (ev.state === 'ALARM' && (!before || before.state !== 'ALARM')) {
      // 새 경보: 띠·소리는 updateAlarm()이 켠다. 소리가 꺼져 있으면 켜 달라고 한 번 더 알린다.
      if (!alarm.unlocked) toast('경보가 왔습니다. 소리를 들으려면 "알림 소리 켜기"를 눌러 주세요.', 'warn');
    }
  }
  if (ev.owner_id != null && !isKnownOwner(ev.owner_id)) scheduleMeRefresh();
  renderAll();
}

function onMessage(msg) {
  if (!msg || typeof msg !== 'object') return;
  if (msg.type === 'snapshot') {
    // 처음 접속·다시 접속할 때마다 전체를 새로 받는다. 끊긴 동안 끝난 경보·지워진 기기는 여기서 사라진다.
    state.devices = new Map((msg.devices || []).map((d) => [d.device_id, d]));
    state.active = new Map((msg.active_events || []).filter((e) => e.state !== 'ENDED').map((e) => [e.id, e]));
    state.connected = true;
    document.body.classList.remove('disconnected');
    hide('#conn-banner');
    pruneAcked();
    renderAll();
    refreshMe();          // 연결이 바뀌었을 수 있으므로 보호하는 사람 목록도 새로
    scheduleHistory(0);
  } else if (msg.type === 'device' && msg.device) {
    state.devices.set(msg.device.device_id, msg.device);
    if (!isKnownOwner(msg.device.owner_id)) scheduleMeRefresh();
    renderDevices();
  } else if (msg.type === 'event' && msg.event) {
    applyEvent(msg.event);
    scheduleHistory();
  }
}

function showBanner(text, kind = '') {
  const banner = $('#conn-banner');
  banner.className = 'banner' + (kind ? ` banner-${kind}` : '');
  banner.textContent = text;
  show(banner);
}

function onClose(code, { willReconnect, retryIn }) {
  state.connected = false;
  document.body.classList.add('disconnected');
  if (code === 4401) {
    // 로그인이 풀렸다(로그아웃·세션 만료). 다시 로그인하면 이 화면으로 돌아온다.
    state.stopped = true;
    showBanner('로그인이 풀렸습니다. 로그인 화면으로 이동합니다…', 'alarm');
    setTimeout(goLogin, 800);
    return;
  }
  if (!willReconnect) {
    state.stopped = true;
    showBanner('서버가 연결을 거부했습니다. 이 서버의 주소로 직접 접속했는지 확인한 뒤 새로 고쳐 주세요.', 'alarm');
    return;
  }
  showBanner(`연결 끊김 - 다시 연결 중 (${retryIn}초 뒤). 그동안 화면의 상태는 최신이 아닐 수 있습니다.`);
}

// --- 휴대폰 알림(웹 푸시) ----------------------------------------------------------

const PUSH_BADGE = {
  on: ['badge-normal', '켜짐'],
  off: ['badge-offline', '꺼짐'],
  denied: ['badge-alarm', '차단됨'],
  unsupported: ['badge-offline', '사용할 수 없음'],
  'server-off': ['badge-offline', '서버 설정 없음'],
};

async function renderPush() {
  let status;
  try {
    status = await pushStatus();
  } catch (err) {
    status = { state: 'off', message: errorText(err) };
  }
  const [cls, label] = PUSH_BADGE[status.state] || PUSH_BADGE.off;
  const badge = $('#push-badge');
  badge.className = `badge ${cls}`;
  badge.textContent = label;
  $('#push-message').textContent = status.message;
  const button = $('#push-toggle');
  button.dataset.state = status.state;
  button.textContent = status.state === 'on' ? '알림 끄기' : '알림 켜기';
  button.className = status.state === 'on' ? 'btn btn-secondary btn-block' : 'btn btn-block';
  button.disabled = status.state === 'unsupported' || status.state === 'server-off';
}

async function onPushToggle(event) {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    if (button.dataset.state === 'on') {
      await disablePush();
      toast('이 휴대폰의 경보 알림을 껐습니다.', 'success');
    } else {
      await enablePush();
      toast('경보 알림을 켰습니다.', 'success');
    }
  } catch (err) {
    toast(errorText(err), 'error');
  }
  await renderPush();
}

// --- 시작 ---------------------------------------------------------------------------

function bindButtons() {
  $('#sound-unlock').addEventListener('click', () => {
    if (unlockSound()) {
      playChime();
      toast('알림 소리를 켰습니다.', 'success');
    } else {
      toast('이 브라우저에서는 소리를 낼 수 없습니다. 휴대폰 알림을 켜 두세요.', 'error');
    }
  });
  $('#sound-test').addEventListener('click', () => {
    unlockSound();
    playSiren();
    vibrate(300);
  });
  $('#alarm-ack').addEventListener('click', () => {
    unlockSound();           // 이 누름도 사용자 동작이므로 이후 새 경보 소리를 위해 깨워 둔다
    acknowledgeAlarms();
  });
  $('#push-toggle').addEventListener('click', onPushToggle);

  // 화면을 어디든 한 번 누르면 소리를 쓸 수 있게 깨워 둔다(버튼을 놓쳐도 경보 소리가 나도록).
  const autoUnlock = () => {
    if (!alarm.unlocked) unlockSound();
    if (alarm.unlocked) {
      document.removeEventListener('click', autoUnlock, true);
      document.removeEventListener('keydown', autoUnlock, true);
    }
  };
  document.addEventListener('click', autoUnlock, true);
  document.addEventListener('keydown', autoUnlock, true);

  // 화면이 다시 보이면 잠들었던 소리 장치를 깨운다(휴대폰 화면을 껐다 켠 경우)
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && alarm.ctx && alarm.ctx.state === 'suspended') {
      alarm.ctx.resume().catch(() => {});
    }
  });
}

async function start() {
  bindButtons();
  renderSoundBox();

  let info = null;
  try {
    info = await renderNav();
  } catch (err) {
    console.warn(err);
  }
  if (!info || !info.user) {
    // 서버가 로그인하지 않은 접속을 이미 로그인 화면으로 보내지만, 그 사이 로그아웃한 경우를 위해
    goLogin();
    return;
  }
  state.info = info;
  renderDevices();

  const socket = connectSocket('/ws/guardian', {
    onMessage,
    onClose,
    onOpen: () => showBanner('서버에 연결되었습니다. 상태를 받는 중입니다…'),
  });
  // 공유기·프록시가 조용한 연결을 끊지 않게 가끔 신호를 보낸다(서버는 무시한다)
  setInterval(() => socket.send({ type: 'ping' }), 25000);

  loadHistory();
  syncPush().finally(renderPush);
}

start();
