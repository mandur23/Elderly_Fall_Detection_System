// 끊기면 다시 연결하는 WebSocket.
//
// 와이파이가 잠깐 끊기거나 서버가 재시작해도 화면이 스스로 다시 붙어야 한다(보호자가 경보를 놓치면 안 된다).
// 다시 연결하는 간격은 1, 2, 5, 10초(이후 계속 10초)로 늘려 서버에 부담을 주지 않는다.
//
// 서버가 아래 코드로 닫으면 다시 연결해도 똑같이 거부되므로 멈추고 onClose(code)로 알린다.
//   4400 잘못된 요청, 4401 인증 실패(로그인·기기 키), 4403 권한 없음, 4409 다른 화면이 같은 기기로 새로 접속함
// (4409에서 다시 연결하면 두 화면이 서로를 계속 밀어내므로 반드시 멈춘다.)

export const STOP_CODES = [4400, 4401, 4403, 4409];
const BACKOFF_SECONDS = [1, 2, 5, 10];

// 서버 주소: https 페이지면 wss, http 페이지면 ws
export function socketUrl(path) {
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${scheme}//${location.host}${path}`;
}

/**
 * @param {string} path  '/ws/guardian', '/ws/camera?device_id=3' 등
 * @param {object} handlers
 *   onMessage(data, event): 글자 메시지는 JSON으로 풀어서(풀 수 없으면 문자열 그대로), 바이너리는 ArrayBuffer로 준다
 *   onOpen(): 연결될 때마다 (다시 연결될 때도)
 *   onClose(code, info): 끊길 때마다. info = { reason, willReconnect, retryIn(초) }
 *   binary: true면 받은 바이너리를 ArrayBuffer로 (기본도 ArrayBuffer로 둔다. Blob보다 다루기 쉽다)
 * @returns {{send(data): boolean, close(): void, readonly connected: boolean,
 *            readonly bufferedAmount: number, readonly socket: WebSocket|null}}
 */
export function connectSocket(path, { onMessage, onOpen, onClose, binary = true } = {}) {
  let ws = null;
  let attempt = 0;          // 연속 실패 횟수 (성공하면 0으로)
  let timer = null;
  let stopped = false;      // close()를 불렀거나 STOP_CODES로 닫혔음

  function open() {
    clearTimeout(timer);
    timer = null;
    if (stopped) return;
    try {
      ws = new WebSocket(socketUrl(path));
    } catch (err) {
      console.error('WebSocket을 만들지 못함', err);
      scheduleRetry(1006, '');
      return;
    }
    ws.binaryType = binary ? 'arraybuffer' : 'blob';
    const current = ws;

    current.onopen = () => {
      if (current !== ws) return;
      attempt = 0;
      if (onOpen) onOpen();
    };

    current.onmessage = (event) => {
      if (current !== ws) return;
      let data = event.data;
      if (typeof data === 'string') {
        try { data = JSON.parse(data); } catch { /* JSON이 아니면 문자열 그대로 넘긴다 */ }
      }
      if (onMessage) onMessage(data, event);
    };

    current.onclose = (event) => {
      if (current !== ws) return;   // 이미 버린 연결
      ws = null;
      if (STOP_CODES.includes(event.code)) stopped = true;
      scheduleRetry(event.code, event.reason);
    };
    // onerror 뒤에는 항상 onclose가 오므로 여기서는 따로 처리하지 않는다
  }

  function scheduleRetry(code, reason) {
    const willReconnect = !stopped;
    const retryIn = BACKOFF_SECONDS[Math.min(attempt, BACKOFF_SECONDS.length - 1)];
    if (onClose) onClose(code, { reason, willReconnect, retryIn: willReconnect ? retryIn : null });
    if (!willReconnect) return;
    attempt += 1;
    timer = setTimeout(open, retryIn * 1000);
  }

  // 네트워크가 돌아오거나 화면을 다시 켰을 때는 기다리지 않고 바로 다시 연결한다
  function retryNow() {
    if (!stopped && !ws && timer) open();
  }
  function onVisible() {
    if (document.visibilityState === 'visible') retryNow();
  }
  window.addEventListener('online', retryNow);
  document.addEventListener('visibilitychange', onVisible);

  open();

  return {
    // 연결되어 있으면 보내고 true, 아니면 false (끊긴 동안의 프레임은 그냥 버린다)
    send(data) {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(typeof data === 'string' || data instanceof Blob || data instanceof ArrayBuffer
          || ArrayBuffer.isView(data) ? data : JSON.stringify(data));
        return true;
      }
      return false;
    },
    // 완전히 닫고 다시 연결하지 않는다
    close() {
      stopped = true;
      clearTimeout(timer);
      timer = null;
      window.removeEventListener('online', retryNow);
      document.removeEventListener('visibilitychange', onVisible);
      if (ws) {
        const old = ws;
        ws = null;           // onclose가 다시 연결하지 않게 먼저 떼어 낸다
        try { old.close(1000, 'bye'); } catch { /* 이미 닫힘 */ }
      }
    },
    get connected() { return !!ws && ws.readyState === WebSocket.OPEN; },
    get bufferedAmount() { return ws ? ws.bufferedAmount : 0; },
    get socket() { return ws; },
  };
}
