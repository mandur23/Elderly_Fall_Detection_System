// 휴대폰 카메라 감지 화면 (/camera, 로그인 필요)
//
// 하는 일
// 1. 이 휴대폰을 로그인한 계정의 'phone' 기기로 등록하고(처음 한 번), 기기 번호를 기억한다.
// 2. '감지 시작'을 누르면 카메라·마이크를 켜고 서버(/ws/camera)에 연결해 1초에 10장씩 JPEG를 보낸다.
//    낙상 판정은 전부 서버가 한다. 이 화면은 영상을 보내고 서버의 지시를 따르기만 한다.
// 3. 서버가 voice_check(음성 확인)를 보내면 안내 음성을 들려주고 5초 녹음해 올린다.
// 4. 낙상 의심·경보 중에는 화면 전체에 큰 '괜찮아요' 버튼을 띄운다.
//
// 휴대폰 브라우저는 HTTPS 페이지에서만 카메라·마이크를 쓸 수 있다(http://localhost는 예외).

import { api, ApiError } from './api.js';
import { connectSocket } from './ws.js';
import { $, show, hide, toast, errorText, renderNav, stateLabel, stateClass } from './ui.js';

// --- 설정 값 ------------------------------------------------------------------

const DEVICE_NAME = '휴대폰 카메라';
const PROMPT_TEXT = "괜찮으신가요? 괜찮으시면 '괜찮아요'라고 말씀하시거나 아래 버튼을 눌러 주세요.";
const ALARM_TEXT = '보호자에게 알렸습니다. 괜찮으시면 아래 버튼을 눌러 주세요.';
// 녹음된 안내 음성. 없으면(404) 브라우저 음성 합성으로 읽는다. (web/static/audio/README.md)
const PROMPT_AUDIO_URL = '/static/audio/ask_status.mp3';

const FRAME_INTERVAL_MS = 100;           // 1초에 10장
const FRAME_LONG_SIDE = 640;             // 긴 변 640px (가로로 들면 640x480)
const JPEG_QUALITY = 0.7;
// 보내지 못하고 쌓인 데이터가 이보다 많으면 이번 장은 건너뛴다(와이파이가 느릴 때 지연이 쌓이지 않게,
// 늘 '가장 최근 장면'만 보낸다). 640x480 JPEG 한 장은 보통 30~60KB.
const MAX_BUFFERED_BYTES = 256 * 1024;
const RECORD_MS = 5000;                  // 대답 녹음 길이
// 음성 합성이 끝났다는 신호(onend)가 오지 않는 브라우저가 있어 이 시간이 지나면 그냥 녹음으로 넘어간다.
// 서버는 음성 확인마다 20초를 기다리므로 안내 + 녹음 + 업로드가 그 안에 끝나야 한다.
const PROMPT_MAX_MS = 10000;

// --- 화면 요소 ----------------------------------------------------------------

const els = {
  error: $('#camera-error'),
  connBadge: $('#conn-badge'),
  stateBadge: $('#state-badge'),
  detail: $('#status-detail'),
  video: $('#preview'),
  placeholder: $('#preview-placeholder'),
  startBtn: $('#start-btn'),
  stopBtn: $('#stop-btn'),
  overlay: $('#alert-overlay'),
  alertTitle: $('#alert-title'),
  alertText: $('#alert-text'),
  alertStep: $('#alert-step'),
  okBtn: $('#ok-btn'),
  overlayStopBtn: $('#overlay-stop-btn'),
};

// --- 상태 ---------------------------------------------------------------------

let me = null;               // 로그인한 사용자 {id, email, name}
let deviceId = null;
let running = false;         // '감지 시작' 상태인가
let stream = null;           // getUserMedia 결과 (영상 + 소리)
let micTrack = null;         // 마이크 트랙. 마이크를 못 쓰면 null
let micProblem = '';         // 마이크를 못 쓰는 이유 (서버에 error로 보낸다)
let sock = null;             // connectSocket() 결과
let frameTimer = null;
let statusTimer = null;
let encoding = false;        // JPEG 변환 중이면 다음 장은 건너뛴다
let canvas = null;
let wakeLock = null;
let recreatedDevice = false; // 기기를 다시 만든 적이 있는가(한 번만 시도해 무한 반복을 막는다)
let serverState = null;      // 서버가 알려 준 감지 상태 (NORMAL / SUSPECT / ALARM)
let activeEventId = null;    // 진행 중인 이벤트 번호 ('괜찮아요' 버튼에 필요)
let voiceAbort = null;       // 진행 중인 음성 확인을 멈추는 AbortController
const handledChecks = new Set();  // 이미 처리한 (event_id:attempt). 다시 연결하면 서버가 같은 요청을 또 보낸다.
const sentAt = [];           // 최근 보낸 프레임 시각 (보내는 속도 표시용)

let promptAudio = null;      // 안내 음성 <audio>
let promptAudioMissing = false;

// --- 기기 번호 기억 ---------------------------------------------------------------
// 기기 번호는 비밀이 아니므로 localStorage에 둔다(기기 키는 휴대폰 기기에는 없다).
// 한 휴대폰을 여러 계정이 쓸 수 있어 사용자마다 따로 기억한다.

function storageKey() {
  return `fall.camera.deviceId.${me.id}`;
}

function readStoredDeviceId() {
  try {
    const value = Number(localStorage.getItem(storageKey()));
    return Number.isInteger(value) && value > 0 ? value : null;
  } catch {
    return null;   // 사생활 보호 모드 등에서 저장소를 못 쓸 수 있다
  }
}

function storeDeviceId(id) {
  try { localStorage.setItem(storageKey(), String(id)); } catch { /* 기억하지 못해도 동작은 한다 */ }
}

function forgetDeviceId() {
  try { localStorage.removeItem(storageKey()); } catch { /* 무시 */ }
}

/**
 * 이 휴대폰의 기기 번호. 기억해 둔 기기가 내 기기 목록에 그대로 있으면 그것을 쓰고,
 * 없으면(처음이거나 마이페이지에서 지웠으면) 새로 등록한다.
 */
async function ensureDevice() {
  const stored = readStoredDeviceId();
  const { devices } = await api('/api/devices');
  if (stored && devices.some((d) => d.id === stored && d.kind === 'phone')) return stored;
  const result = await api('/api/devices', { json: { name: DEVICE_NAME, kind: 'phone' } });
  storeDeviceId(result.device.id);
  return result.device.id;
}

// --- 화면 표시 도우미 -------------------------------------------------------------

function showError(message) {
  els.error.textContent = message;
  show(els.error);
}

function hideError() {
  els.error.textContent = '';
  hide(els.error);
}

function setDetail(text) {
  els.detail.textContent = text;
}

// 서버 연결 배지: idle / connecting / connected / reconnecting
function setConn(kind, retryIn) {
  const map = {
    idle: ['연결 안 됨', 'badge-offline'],
    connecting: ['연결 중…', 'badge-suspect'],
    connected: ['연결됨', 'badge-normal'],
    reconnecting: [retryIn ? `끊김 - ${retryIn}초 뒤 다시 연결` : '끊김 - 다시 연결 중', 'badge-alarm'],
  };
  const [text, cls] = map[kind] || map.idle;
  els.connBadge.textContent = text;
  els.connBadge.className = `badge ${cls}`;
}

// 감지 상태 배지. FALLING(넘어지는 중인지 지켜보는 단계)은 사용자에게 따로 보여 주지 않는다.
function setStateBadge(state) {
  if (!state) {
    els.stateBadge.textContent = '멈춤';
    els.stateBadge.className = 'badge badge-offline';
    return;
  }
  els.stateBadge.textContent = stateLabel(state);
  els.stateBadge.className = `badge ${stateClass(state)}`;
}

function showOverlay(state) {
  const alarm = state === 'ALARM';
  els.overlay.classList.toggle('is-alarm', alarm);
  els.alertTitle.textContent = alarm ? '경보' : '상태 확인 중';
  els.alertText.textContent = alarm ? ALARM_TEXT : PROMPT_TEXT;
  if (alarm) els.alertStep.textContent = '';
  els.okBtn.disabled = false;
  const wasHidden = els.overlay.classList.contains('hidden');
  show(els.overlay);
  if (wasHidden) els.okBtn.focus();
}

function hideOverlay() {
  hide(els.overlay);
  els.alertStep.textContent = '';
}

function setStep(text) {
  els.alertStep.textContent = text;
}

// --- 카메라·마이크 ----------------------------------------------------------------

function mediaErrorText(err) {
  const name = err && err.name;
  if (name === 'NotAllowedError' || name === 'SecurityError') {
    return '카메라 사용이 거부되었습니다. 주소창 왼쪽의 자물쇠(사이트 설정)를 눌러 카메라와 마이크를 ‘허용’으로 바꾼 뒤 다시 눌러 주세요.';
  }
  if (name === 'NotFoundError' || name === 'OverconstrainedError') return '이 휴대폰에서 카메라를 찾을 수 없습니다.';
  if (name === 'NotReadableError' || name === 'AbortError') {
    return '다른 앱이 카메라를 쓰고 있습니다. 그 앱을 닫고 다시 눌러 주세요.';
  }
  return `카메라를 켜지 못했습니다. (${name || '알 수 없는 오류'})`;
}

const VIDEO_CONSTRAINTS = { facingMode: 'environment', width: 640, height: 480 };

/**
 * 카메라와 마이크를 한 번에 요청한다(권한 창이 한 번만 뜨게).
 * 마이크만 거부되었으면 영상만으로 감지는 계속하고, 음성 확인 때는 '녹음 실패'를 서버에 알린다
 * (서버는 이것을 '확인 불가'로 보고 보호자에게 경보를 보낸다).
 */
async function openMedia() {
  let result;
  try {
    result = await navigator.mediaDevices.getUserMedia({ video: VIDEO_CONSTRAINTS, audio: true });
    micProblem = '';
  } catch (err) {
    console.info('카메라+마이크 요청 실패, 카메라만 다시 시도', err.name);
    // 영상만 다시 요청해 본다. 이것도 안 되면 카메라 문제다.
    result = await navigator.mediaDevices.getUserMedia({ video: VIDEO_CONSTRAINTS, audio: false });
    micProblem = `microphone unavailable (${err.name || 'error'})`;
    toast('마이크를 쓸 수 없어 음성으로 대답을 들을 수 없습니다. 넘어지면 보호자에게 바로 알립니다.', 'warn');
  }
  stream = result;
  micTrack = stream.getAudioTracks()[0] || null;
  if (!micTrack && !micProblem) micProblem = 'microphone unavailable (no track)';

  const videoTrack = stream.getVideoTracks()[0];
  if (videoTrack) {
    // 다른 앱이 카메라를 가져가거나 권한이 취소되면 트랙이 끝난다
    videoTrack.addEventListener('ended', () => {
      if (running) stopDetection({ message: '카메라가 꺼졌습니다. 다시 ‘감지 시작’을 눌러 주세요.' });
    });
  }
  els.video.srcObject = stream;
  hide(els.placeholder);
  try { await els.video.play(); } catch { /* autoplay 속성으로 곧 재생된다 */ }
}

function stopMedia() {
  if (stream) for (const track of stream.getTracks()) track.stop();
  stream = null;
  micTrack = null;
  els.video.srcObject = null;
  show(els.placeholder);
}

// --- 화면 꺼짐 방지 (Wake Lock) -----------------------------------------------------
// 화면이 꺼지면 브라우저가 카메라를 멈춘다. 다른 앱을 갔다 오면 잠금이 풀리므로 다시 요청한다.

async function requestWakeLock() {
  if (!('wakeLock' in navigator) || wakeLock || !running) return;
  try {
    wakeLock = await navigator.wakeLock.request('screen');
    wakeLock.addEventListener('release', () => { wakeLock = null; });
  } catch (err) {
    console.info('화면 꺼짐 방지를 켜지 못함', err.name);
  }
}

function releaseWakeLock() {
  if (wakeLock) {
    wakeLock.release().catch(() => {});
    wakeLock = null;
  }
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && running) requestWakeLock();
});

// --- 프레임 보내기 ------------------------------------------------------------------

function sendFrame() {
  if (!running || encoding || !sock || !sock.connected) return;
  if (sock.bufferedAmount > MAX_BUFFERED_BYTES) return;   // 앞 장이 아직 나가는 중
  const video = els.video;
  const w = video.videoWidth;
  const h = video.videoHeight;
  if (video.readyState < 2 || !w || !h) return;           // 아직 영상이 없음

  // 휴대폰을 세로로 들면 영상이 480x640으로 온다. 비율을 지켜 긴 변을 640으로 맞춘다(찌그러지면 자세 판정이 틀린다).
  const scale = Math.min(1, FRAME_LONG_SIDE / Math.max(w, h));
  const cw = Math.round(w * scale);
  const ch = Math.round(h * scale);
  if (!canvas) canvas = document.createElement('canvas');
  if (canvas.width !== cw || canvas.height !== ch) {
    canvas.width = cw;
    canvas.height = ch;
  }
  canvas.getContext('2d').drawImage(video, 0, 0, cw, ch);

  encoding = true;
  canvas.toBlob((blob) => {
    encoding = false;
    if (!blob || !running || !sock) return;
    if (sock.send(blob)) {        // 바이너리 메시지 1개 = JPEG 1장
      sentAt.push(performance.now());
      if (sentAt.length > 40) sentAt.shift();
    }
  }, 'image/jpeg', JPEG_QUALITY);
}

// 1초마다 보내는 속도를 보여 준다(서버가 영상을 받고 있는지 사용자가 확인할 수 있게)
function updateStatusText() {
  if (!running) return;
  if (!sock || !sock.connected) return;
  const now = performance.now();
  const recent = sentAt.filter((t) => now - t <= 2000).length / 2;
  if (serverState === 'SUSPECT' || serverState === 'ALARM') return;
  setDetail(recent > 0
    ? `감지 중입니다. 화면을 1초에 ${Math.round(recent)}장 보내고 있습니다.`
    : '감지 중입니다. 카메라 영상을 기다리는 중…');
}

// --- 서버 연결 ----------------------------------------------------------------------

function closeText(code) {
  if (code === 4409) return '다른 화면에서 같은 기기로 감지 중입니다. 이 화면의 감지는 멈췄습니다.';
  if (code === 4401) return '로그인이 풀렸거나 이 기기가 지워졌습니다. 다시 로그인한 뒤 감지를 시작해 주세요.';
  if (code === 4403) return '이 기기를 쓸 권한이 없습니다. 이 휴대폰에 로그인한 계정을 확인해 주세요.';
  if (code === 4400) return '잘못된 연결 요청입니다. 화면을 새로 고친 뒤 다시 시도해 주세요.';
  return `서버 연결이 끊겼습니다. (코드 ${code})`;
}

function openSocket() {
  setConn('connecting');
  const current = connectSocket(`/ws/camera?device_id=${deviceId}`, {
    onOpen() {
      setConn('connected');
      updateStatusText();
    },
    onMessage(data) {
      if (current === sock) handleMessage(data);
    },
    onClose(code, info) {
      if (current !== sock || !running) return;
      handleClose(code, info);
    },
  });
  sock = current;
}

async function handleClose(code, info) {
  if (code === 4409) {
    stopDetection({ message: closeText(code) });
    return;
  }
  // 기억해 둔 기기가 지워졌거나(4401 unknown device) 다른 계정의 기기면(4403) 새로 등록해 한 번만 다시 붙는다.
  // 로그인이 풀린 경우라면 ensureDevice()의 API 호출이 401이 되어 로그인 화면으로 간다.
  if ((code === 4401 || code === 4403) && !recreatedDevice) {
    recreatedDevice = true;
    sock = null;
    forgetDeviceId();
    try {
      deviceId = await ensureDevice();
      if (running) openSocket();
    } catch (err) {
      stopDetection({ message: errorText(err) });
    }
    return;
  }
  if (!info.willReconnect) {
    stopDetection({ message: closeText(code) });
    return;
  }
  setConn('reconnecting', info.retryIn);
  setDetail('서버 연결이 끊겼습니다. 다시 연결하는 중입니다. 와이파이를 확인해 주세요.');
}

function handleMessage(msg) {
  if (!msg || typeof msg !== 'object') return;
  if (msg.type === 'hello') {
    setDetail(`‘${msg.name || DEVICE_NAME}’(기기 ${msg.device_id})로 연결되었습니다.`);
  } else if (msg.type === 'state') {
    setServerState(msg.state, msg.event_id);
  } else if (msg.type === 'voice_check') {
    handleVoiceCheck(msg);
  }
}

function setServerState(rawState, eventId) {
  // FALLING은 '넘어지는 중인지 지켜보는 단계'라 사용자에게는 정상과 같게 보여 준다
  const state = rawState === 'FALLING' ? 'NORMAL' : rawState;
  serverState = state;
  setStateBadge(state);
  if (state === 'SUSPECT' || state === 'ALARM') {
    if (eventId) activeEventId = Number(eventId);
    // 다시 연결했을 때의 첫 state 메시지에는 이벤트 번호가 없으므로 따로 물어본다
    else if (!activeEventId) lookupActiveEvent();
    showOverlay(state);
    if (state === 'ALARM') {
      abortVoice();   // 경보로 넘어갔으면 더 녹음할 필요가 없다
      setDetail('보호자에게 경보를 보냈습니다.');
    } else {
      setDetail('넘어진 것 같아 상태를 확인하고 있습니다.');
    }
  } else {
    // 정상으로 돌아옴(음성으로 괜찮다고 했거나, 버튼·보호자가 해제함)
    activeEventId = null;
    abortVoice();
    hideOverlay();
    updateStatusText();
  }
}

async function lookupActiveEvent() {
  try {
    const result = await api(`/api/devices/${deviceId}/state`, { noRedirect: true });
    if (result.active_event) activeEventId = result.active_event.id;
  } catch (err) {
    console.info('진행 중인 이벤트를 확인하지 못함', errorText(err));
  }
  return activeEventId;
}

// --- 음성 확인 ----------------------------------------------------------------------

/**
 * 버튼을 누른 순간(사용자 동작) 소리 재생 권한을 열어 둔다.
 * 휴대폰 브라우저는 사용자가 화면을 누르기 전에는 소리를 내지 못하게 막는데,
 * 낙상 의심은 아무도 화면을 누르지 않을 때 오므로 미리 열어 두어야 한다.
 */
function unlockAudio() {
  if (!promptAudio) {
    promptAudio = new Audio();
    promptAudio.preload = 'auto';
    // 파일이 없으면(404) 다음부터는 바로 음성 합성을 쓴다
    promptAudio.addEventListener('error', () => { promptAudioMissing = true; });
    promptAudio.src = PROMPT_AUDIO_URL;
    promptAudio.load();
  }
  if ('speechSynthesis' in window) {
    try {
      speechSynthesis.getVoices();   // 목소리 목록을 미리 불러온다(안드로이드는 처음에 비어 있음)
      const silent = new SpeechSynthesisUtterance(' ');
      silent.volume = 0;
      speechSynthesis.speak(silent);
    } catch { /* 음성 합성이 없는 브라우저 */ }
  }
}

function abortVoice() {
  if (voiceAbort) {
    voiceAbort.abort();
    voiceAbort = null;
  }
  if ('speechSynthesis' in window) speechSynthesis.cancel();
  if (promptAudio && !promptAudio.paused) promptAudio.pause();
}

// signal이 취소되면 바로 끝나는 기다림
function waitFor(ms, signal) {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener('abort', () => { clearTimeout(timer); resolve(); }, { once: true });
  });
}

// 녹음된 안내 음성을 끝까지 재생한다. 파일이 없거나 재생을 막히면 오류를 던진다.
async function playPromptFile(signal) {
  if (!promptAudio || promptAudioMissing) throw new Error('no prompt file');
  promptAudio.currentTime = 0;
  await promptAudio.play();
  await new Promise((resolve) => {
    const done = () => {
      clearTimeout(timer);
      promptAudio.removeEventListener('ended', done);
      promptAudio.removeEventListener('error', done);
      resolve();
    };
    const timer = setTimeout(done, PROMPT_MAX_MS * 1.5);
    promptAudio.addEventListener('ended', done);
    promptAudio.addEventListener('error', done);
    signal.addEventListener('abort', () => { promptAudio.pause(); done(); }, { once: true });
  });
}

// 브라우저 음성 합성(한국어)으로 안내 문장을 읽는다. 끝날 때까지(또는 최대 PROMPT_MAX_MS) 기다린다.
function speakPrompt(signal) {
  if (!('speechSynthesis' in window)) return Promise.resolve();
  return new Promise((resolve) => {
    let started = false;
    let finished = false;
    const finish = () => {
      if (finished) return;
      finished = true;
      clearTimeout(maxTimer);
      clearTimeout(startCheck);
      resolve();
    };
    const utter = new SpeechSynthesisUtterance(PROMPT_TEXT);
    utter.lang = 'ko-KR';
    utter.rate = 0.9;      // 어르신이 알아듣기 쉽게 조금 천천히
    const korean = speechSynthesis.getVoices().find((v) => v.lang && v.lang.toLowerCase().startsWith('ko'));
    if (korean) utter.voice = korean;
    utter.onstart = () => { started = true; };
    utter.onend = finish;
    utter.onerror = finish;
    const maxTimer = setTimeout(finish, PROMPT_MAX_MS);
    // 1.5초가 지나도 말하기 시작하지 않았으면(목소리가 없는 기기 등) 기다리지 않는다
    const startCheck = setTimeout(() => {
      if (!started && !speechSynthesis.speaking) finish();
    }, 1500);
    signal.addEventListener('abort', () => { speechSynthesis.cancel(); finish(); }, { once: true });
    speechSynthesis.cancel();   // 앞에 남은 말이 있으면 지운다
    speechSynthesis.speak(utter);
  });
}

async function playPrompt(signal) {
  try {
    await playPromptFile(signal);
  } catch {
    if (!signal.aborted) await speakPrompt(signal);
  }
}

// 브라우저가 지원하는 녹음 형식 (안드로이드 Chrome은 webm/opus, 아이폰 Safari는 mp4)
function pickMimeType() {
  if (typeof MediaRecorder === 'undefined' || !MediaRecorder.isTypeSupported) return '';
  const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus'];
  return candidates.find((type) => MediaRecorder.isTypeSupported(type)) || '';
}

function fileNameFor(mimeType) {
  if (mimeType.includes('mp4')) return 'reply.m4a';
  if (mimeType.includes('ogg')) return 'reply.ogg';
  return 'reply.webm';
}

/**
 * 마이크로 RECORD_MS 동안 녹음한다. onTick(남은 초)로 화면에 남은 시간을 보여 준다.
 * 취소되면 null, 성공하면 {blob, filename}. 녹음할 수 없으면 오류를 던진다.
 */
function recordReply(signal, onTick) {
  if (typeof MediaRecorder === 'undefined') throw new Error('MediaRecorder not supported');
  if (!micTrack || micTrack.readyState !== 'live') throw new Error(micProblem || 'microphone ended');
  const mimeType = pickMimeType();
  // 마이크 트랙만 녹음한다(영상까지 넣으면 파일이 커지고 서버가 소리를 꺼내기 어렵다)
  const recorder = new MediaRecorder(new MediaStream([micTrack]), mimeType ? { mimeType } : undefined);
  const chunks = [];
  return new Promise((resolve, reject) => {
    let left = Math.round(RECORD_MS / 1000);
    onTick(left);
    const ticker = setInterval(() => { left -= 1; if (left > 0) onTick(left); }, 1000);
    const stopTimer = setTimeout(() => { if (recorder.state !== 'inactive') recorder.stop(); }, RECORD_MS);
    const cleanup = () => { clearInterval(ticker); clearTimeout(stopTimer); };

    recorder.ondataavailable = (event) => { if (event.data && event.data.size) chunks.push(event.data); };
    recorder.onerror = (event) => {
      cleanup();
      reject(event.error || new Error('recorder error'));
    };
    recorder.onstop = () => {
      cleanup();
      if (signal.aborted) { resolve(null); return; }
      const type = recorder.mimeType || mimeType || 'audio/webm';
      const blob = new Blob(chunks, { type });
      if (!blob.size) { reject(new Error('empty recording')); return; }
      resolve({ blob, filename: fileNameFor(type) });
    };
    signal.addEventListener('abort', () => {
      if (recorder.state !== 'inactive') recorder.stop();
    }, { once: true });
    recorder.start();
  });
}

async function uploadVoice(eventId, attempt, { blob, filename, error }) {
  const form = new FormData();
  if (blob) form.append('audio', blob, filename);
  else form.append('error', error);
  form.append('attempt', String(attempt));
  // 감지 도중 로그인 화면으로 넘어가면 카메라가 멈추므로, 401이어도 화면은 그대로 둔다
  return api(`/api/events/${eventId}/voice`, { form, noRedirect: true });
}

async function handleVoiceCheck(msg) {
  const eventId = Number(msg.event_id);
  const attempt = Number(msg.attempt);
  if (!Number.isInteger(eventId) || !Number.isInteger(attempt)) return;
  const key = `${eventId}:${attempt}`;
  if (handledChecks.has(key)) return;   // 다시 연결할 때 서버가 같은 요청을 또 보낸다
  handledChecks.add(key);

  // 앞 회차가 아직 진행 중이면 새 회차가 우선이다(서버는 이미 앞 회차를 기다리지 않는다)
  abortVoice();
  const ctrl = new AbortController();
  voiceAbort = ctrl;
  const { signal } = ctrl;

  activeEventId = eventId;
  if (serverState !== 'SUSPECT') {
    serverState = 'SUSPECT';
    setStateBadge('SUSPECT');
  }
  showOverlay('SUSPECT');

  try {
    setStep(attempt > 1 ? '다시 여쭙니다…' : '안내를 들려 드리는 중…');
    await playPrompt(signal);
    if (signal.aborted) return;

    let payload;
    try {
      const recorded = await recordReply(signal, (left) => setStep(`지금 말씀해 주세요 (${left}초)`));
      if (!recorded) return;   // 녹음 중에 취소됨(버튼으로 해제 등)
      payload = recorded;
    } catch (err) {
      if (signal.aborted) return;
      console.info('녹음 실패', err);
      // 녹음을 못 하면 '확인 불가'로 서버에 알린다(서버가 보호자에게 경보를 보낸다)
      payload = { error: micProblem || `record failed (${err.name || err.message || 'error'})` };
    }
    if (signal.aborted) return;

    setStep('대답을 보내는 중…');
    await uploadVoice(eventId, attempt, payload);
    if (!signal.aborted) setStep('대답을 확인하고 있습니다…');
  } catch (err) {
    if (signal.aborted) return;
    // 이미 끝난 이벤트(404)는 조용히 넘어간다. 그 밖의 오류는 서버가 기다리다 다시 묻는다.
    if (err instanceof ApiError && err.status === 404) return;
    setStep('');
    toast('대답을 보내지 못했습니다. ' + errorText(err), 'error');
  } finally {
    if (voiceAbort === ctrl) voiceAbort = null;
  }
}

// --- '괜찮아요' 버튼 ------------------------------------------------------------------

async function onOk() {
  els.okBtn.disabled = true;
  try {
    const eventId = activeEventId || await lookupActiveEvent();
    if (eventId) {
      await api(`/api/events/${eventId}/clear`, { method: 'POST', noRedirect: true });
    }
    abortVoice();
    hideOverlay();
    toast('괜찮다고 전했습니다.', 'success');
  } catch (err) {
    // 이미 끝난(409) 또는 없는(404) 이벤트면 화면만 닫는다
    if (err instanceof ApiError && (err.status === 409 || err.status === 404)) {
      abortVoice();
      hideOverlay();
      return;
    }
    els.okBtn.disabled = false;
    toast('전하지 못했습니다. ' + errorText(err), 'error');
  }
}

// --- 시작·중지 ----------------------------------------------------------------------

async function startDetection() {
  if (running) return;
  els.startBtn.disabled = true;
  hideError();
  unlockAudio();   // await 전에(버튼을 누른 그 순간에) 해야 한다

  try {
    await openMedia();
  } catch (err) {
    showError(mediaErrorText(err));
    els.startBtn.disabled = false;
    return;
  }

  try {
    setDetail('기기 정보를 확인하는 중…');
    deviceId = await ensureDevice();
  } catch (err) {
    stopMedia();
    showError('기기를 등록하지 못했습니다. ' + errorText(err));
    els.startBtn.disabled = false;
    setDetail('');
    return;
  }

  running = true;
  recreatedDevice = false;
  serverState = 'NORMAL';
  setStateBadge(null);
  hide(els.startBtn);
  els.startBtn.disabled = false;
  show(els.stopBtn);
  requestWakeLock();
  openSocket();
  frameTimer = setInterval(sendFrame, FRAME_INTERVAL_MS);
  statusTimer = setInterval(updateStatusText, 1000);
}

/**
 * 감지를 멈춘다. message가 있으면 왜 멈췄는지 보여 준다.
 */
function stopDetection({ message } = {}) {
  running = false;
  clearInterval(frameTimer);
  clearInterval(statusTimer);
  frameTimer = statusTimer = null;
  if (sock) {
    const old = sock;
    sock = null;
    old.close();
  }
  abortVoice();
  stopMedia();
  releaseWakeLock();
  hideOverlay();
  serverState = null;
  activeEventId = null;
  encoding = false;
  sentAt.length = 0;
  setConn('idle');
  setStateBadge(null);
  show(els.startBtn);
  hide(els.stopBtn);
  setDetail('감지를 멈췄습니다. ‘감지 시작’을 누르면 다시 시작합니다.');
  if (message) showError(message);
}

function onStopClick() {
  if ((serverState === 'SUSPECT' || serverState === 'ALARM')
      && !confirm('지금 감지를 멈추면 대답을 확인할 수 없어 보호자에게 경보가 갈 수 있습니다. 멈출까요?')) {
    return;
  }
  stopDetection();
}

// --- 처음 열 때 ------------------------------------------------------------------------

async function init() {
  const info = await renderNav();
  if (!info || !info.user) {
    // 서버가 로그인 전에는 이 화면을 주지 않지만, 세션이 막 끝난 경우를 대비한다
    location.href = '/login?next=' + encodeURIComponent(location.pathname);
    return;
  }
  me = info.user;

  els.startBtn.addEventListener('click', startDetection);
  els.stopBtn.addEventListener('click', onStopClick);
  els.overlayStopBtn.addEventListener('click', onStopClick);
  els.okBtn.addEventListener('click', onOk);
  // 화면을 떠날 때 연결을 바로 닫아 서버가 '연결 끊김'을 빨리 알게 한다
  window.addEventListener('pagehide', () => { if (running) stopDetection(); });

  if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    els.startBtn.disabled = true;
    showError('카메라는 보안 연결(https://로 시작하는 주소)에서만 쓸 수 있습니다. 주소를 https://로 열어 주세요.');
    setDetail('');
  }
}

init();
