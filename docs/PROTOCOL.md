# 라즈베리파이–PC 서버 통신 규약

> 새 구조(라즈베리파이는 입출력, PC 서버는 처리)의 기준 문서입니다. 코드는 [`shared/protocol.py`](../shared/protocol.py)에 있고, 이 문서와 코드가 다르면 코드를 기준으로 하고 문서를 고칩니다. 바꿀 때는 `PROTOCOL_VERSION`을 올리고 양쪽을 함께 고칩니다.

## 1. 구성

```
라즈베리파이 (edge/)                              PC 서버 (server/)
 카메라 ──JPEG 10fps──────────┐                ┌─ 자세 추정 → 특징 → 낙상 판정
 마이크 ──PCM 16kHz(요청 시)──┤  WebSocket     ├─ 음성 인식(Vosk) → 응답 판단
 스피커 ◀─PCM(안내·경보음)────┤ /ws/device     ├─ 이벤트 기록, 보호자 알림
 상태(카메라·마이크·스피커)───┘  (edge가 접속) └─ 현장 화면(/kiosk), API
 화면: Chromium 키오스크 ───────── HTTP ─────────▶ http://<서버>:8000/kiosk
```

| 폴더 | 실행 위치 | 담당 |
| --- | --- | --- |
| `edge/` | 라즈베리파이 | 영상·소리 입출력, 서버 접속·재접속, 장치 상태 보고, 키오스크 실행 |
| `server/` | PC | 영상·음성 실시간 처리, 낙상 판정, 음성 확인, 경보·알림, 기록, 현장 화면 |
| `shared/` | 양쪽 | 통신 규약 (표준 라이브러리만 사용) |

기존 코드(`l2_system/`, `l3_perception/`, `l4_application/`, `common/`, `tools/`, `tests/`, `config.py`, `run_demo.py`)는 새 코드가 완성될 때까지 **참고용으로만** 남깁니다. 새 코드는 이 폴더들을 import하지 않습니다.

## 2. 연결

- edge가 서버에 접속합니다: `ws://<서버 주소>:8000/ws/device`
- 핸드셰이크 헤더: `Authorization: Bearer <DEVICE_TOKEN>`. 토큰이 틀리면 서버는 HTTP 401로 거절합니다.
- 접속 직후 edge가 `hello`를 보내고, 서버가 `welcome`으로 답합니다. edge는 `welcome`의 영상 설정을 적용한 뒤 영상을 보내기 시작합니다.
- 양쪽 모두 10초마다 WebSocket ping을 보냅니다. 응답이 없으면 끊긴 것으로 봅니다.
- 끊기면 edge가 1 → 2 → 5 → 10초(이후 10초) 간격으로 다시 접속합니다.
- 같은 `device_id`가 다시 접속하면 서버는 이전 연결을 코드 4409로 닫습니다. 버전이 다르거나 메시지가 잘못되면 4400으로 닫습니다.

## 3. 메시지

### 바이너리: `[종류 1바이트][시각 8바이트 float64 big-endian][내용]`

| 종류 | 방향 | 내용 | 시각 |
| --- | --- | --- | --- |
| `VIDEO` 0x01 | edge → 서버 | JPEG 한 장 | edge의 촬영 시각 `time.monotonic()` (초) |
| `AUDIO_IN` 0x02 | edge → 서버 | 마이크 PCM s16le mono 16kHz, 0.1초(3200바이트) 단위 | 첫 샘플의 녹음 시각 (같은 시계) |
| `AUDIO_OUT` 0x03 | 서버 → edge | 재생할 PCM (같은 형식), 크기 자유 | 쓰지 않음 (0) |

### JSON: `{"type": ..., ...}`

| type | 방향 | 필수 항목 | 그 밖의 항목·의미 |
| --- | --- | --- | --- |
| `hello` | edge → 서버 | `protocol`, `device_id`, `video`, `audio` | `name`(설치 위치), `software`. `video`: `{width, height, fps, format:"jpeg"}`, `audio`: `{rate, channels, format:"s16le"}` |
| `status` | edge → 서버 | `camera_ok`, `mic_ok`, `speaker_ok` | 2초마다. `camera_error`·`mic_error`·`speaker_error`(문자열), `fps`(실제 전송 속도), `cpu_temp_c`, `uptime_s` |
| `playback_done` | edge → 서버 | `id` | `play_begin`으로 시작한 재생이 끝났을 때. 중단됐으면 `stopped: true` |
| `welcome` | 서버 → edge | `protocol`, `video` | `video`: `{width, height, fps, jpeg_quality}`. `name`(서버가 아는 설치 위치) |
| `config` | 서버 → edge | `video` | 접속 중 영상 설정 변경 |
| `mic` | 서버 → edge | `enabled` | `true`면 `AUDIO_IN` 전송 시작, `false`면 중지. 접속 직후 기본은 꺼짐 |
| `play_begin` | 서버 → edge | `id` | 이어서 오는 `AUDIO_OUT`을 이 id로 재생. `loop: true`면 `play_stop`까지 반복 |
| `play_end` | 서버 → edge | `id` | 이 id의 소리를 다 보냄. edge는 다 재생한 뒤 `playback_done` |
| `play_stop` | 서버 → edge | - | 재생 즉시 중단, 받은 소리를 버림. 재생 중이던 id마다 `playback_done`(`stopped: true`) |
| `error` | 양쪽 | `message` | 사람이 읽는 설명 |

## 4. 동작 규칙

### 영상
- edge는 설정된 fps로 촬영해 최신 장면만 보냅니다. 이전 장면을 아직 보내는 중이면 새 장면을 버리고, 쌓아 두지 않습니다.
- 서버도 처리가 밀리면 가장 최근 장면만 처리합니다.
- 서버는 낙상 판정(하강 속도 등)에 **edge가 붙인 촬영 시각**을 씁니다. 네트워크 지연이 판정을 흔들지 않게 하기 위함입니다. 다시 접속하면 시계가 달라질 수 있으므로 판정 기록을 새로 시작합니다.

### 소리
- 마이크는 서버가 `mic`로 켤 때만 보냅니다(평소에는 방 안 소리를 내보내지 않음).
- 안내 음성은 서버가 만들어 보냅니다: `play_begin` → `AUDIO_OUT` 여러 개 → `play_end`. edge는 다 재생한 뒤 `playback_done`을 보냅니다.
- 서버는 `playback_done`을 받은 뒤 마이크를 켭니다. 안내 음성이 녹음되지 않게 하기 위함입니다.
- 경보음은 `loop: true`로 한 번 보내고, edge가 `play_stop`까지 반복 재생합니다(계속 전송하지 않음).

### 연결이 끊기면
- edge: 재생과 마이크 전송을 멈추고, 다시 접속을 시도합니다. 화면은 서버 페이지라 '연결 끊김'이 표시됩니다.
- 서버: 감지가 멈춘 것으로 보고 장치 문제로 표시합니다. 끊김이 오래 이어지면 보호자에게 '장치 이상'을 알립니다. 진행 중이던 음성 확인은 안전 쪽(경보)으로 처리합니다.

### 장치 상태
- edge는 2초마다 `status`를 보냅니다. 서버는 카메라·마이크·스피커 문제와 `status`가 끊긴 상황을 장치 문제로 보고 현장 화면에 보여 줍니다.

### 개인정보
- 양쪽 모두 영상·음성 원본을 저장하지 않습니다. 서버는 메모리에서 처리만 합니다.
- 같은 공유기(LAN) 안에서만 씁니다. 토큰은 설정 파일에 두고 저장소에 올리지 않습니다.
- 영상이 네트워크로 나가므로, 외부망을 거치게 되면 TLS(`wss://`)를 먼저 적용해야 합니다(아직 미구현).

## 5. 개발 규칙

- Python 3.11 이상. edge와 server는 각자 `requirements.txt`를 둡니다(권장: 양쪽 모두 `aiohttp`).
- 테스트는 각 폴더의 `tests/`에 두고 저장소 최상위에서 실행합니다:
  `python -m unittest discover -s shared/tests -t .`, `-s edge/tests`, `-s server/tests`
- 라즈베리파이 없이도 시험할 수 있어야 합니다. edge는 PC의 웹캠·마이크나 영상 파일로 실행할 수 있게 만들고, server는 가짜 edge로 시험합니다.
