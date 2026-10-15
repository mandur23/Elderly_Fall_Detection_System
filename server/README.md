# PC 서버 (`server/`)

라즈베리파이(edge)가 WebSocket으로 보내는 영상·음성을 PC에서 실시간으로 처리합니다.
낙상 판정, 음성으로 상태 확인, 경보·보호자 알림, 기록, 현장 화면(`/kiosk`)을 모두 이 프로그램이 맡습니다.
통신 규약은 [`docs/PROTOCOL.md`](../docs/PROTOCOL.md)와 [`shared/protocol.py`](../shared/protocol.py)를 따릅니다.

```
라즈베리파이 ──영상 JPEG 10fps──▶ 자세 추정(MediaPipe) → 특징 → 낙상 판정
           ──마이크(요청 시)───▶ 음성 인식(Vosk) → 응답 판단
           ◀──안내 음성·경보음──  이벤트 처리 → 기록(SQLite), 보호자 알림(텔레그램)
 Chromium ──HTTP────────────▶ 현장 화면 /kiosk, API
```

## 1. 설치

Python 3.11 이상이 필요합니다. 저장소 최상위 폴더에서 실행합니다.

```powershell
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r server/requirements.txt
```

`mediapipe`가 `opencv-contrib-python`을 함께 설치합니다. `opencv-python`이나 `opencv-python-headless`를
따로 설치하면 `cv2`가 충돌할 수 있으니 설치하지 마세요.

### 모델 내려받기

두 모델 모두 `assets/models/`에 둡니다(이 폴더는 저장소에 올리지 않습니다). 다른 곳에 두었다면 설정에서 경로를 바꿉니다.

| 모델 | 용도 | 내려받기 |
| --- | --- | --- |
| `pose_landmarker_lite.task` (약 5MB) | 자세 추정 | https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task |
| `vosk-model-small-ko-0.22` (약 82MB 압축) | 한국어 음성 인식 | https://alphacephei.com/vosk/models/vosk-model-small-ko-0.22.zip |

```powershell
New-Item -ItemType Directory -Force assets/models
Invoke-WebRequest https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task -OutFile assets/models/pose_landmarker_lite.task
Invoke-WebRequest https://alphacephei.com/vosk/models/vosk-model-small-ko-0.22.zip -OutFile assets/models/vosk-ko.zip
Expand-Archive assets/models/vosk-ko.zip -DestinationPath assets/models
Remove-Item assets/models/vosk-ko.zip
```

압축을 풀면 `assets/models/vosk-model-small-ko-0.22/` 폴더가 생겨야 합니다.

## 2. 설정

```powershell
copy server\config.example.toml server\config.toml
```

`server/config.toml`은 토큰이 들어 있어 저장소에 올리지 않습니다(`server/.gitignore`). 꼭 바꿀 항목:

| 항목 | 설명 |
| --- | --- |
| `device_token` | 라즈베리파이와 같은 값. 비어 있으면 서버가 시작하지 않습니다. 만들기: `python -c "import secrets; print(secrets.token_urlsafe(24))"` |
| `device_name` | 설치 위치(알림·화면에 표시). 기본 `거실` |
| `telegram_bot_token`, `telegram_chat_id` | 보호자 알림. 비워 두면 '알림 미설정'으로 표시하고 보내지 않습니다 |
| `pose_model_path`, `vosk_model_path` | 모델 경로. 상대 경로는 저장소 최상위 기준 |
| `prompt_wav` | 녹음한 안내 음성(wav). 없으면 운영체제 합성 음성, 그것도 없으면 알림음 |

나머지 항목과 기본값은 [`server/config.py`](config.py)의 `Settings`에 설명이 있습니다. 낙상 판정 기준(하강 속도 1.5,
누움 2초 등)은 공개 영상 평가로 정한 값이므로([`docs/EVALUATION.md`](../docs/EVALUATION.md)) 근거 없이 바꾸지 마세요.

환경변수가 있으면 설정 파일보다 우선합니다: `DEVICE_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
`FALL_SERVER_HOST`, `FALL_SERVER_PORT`, `FALL_POSE_MODEL`, `FALL_VOSK_MODEL`, `FALL_DB_PATH`,
`FALL_SERVER_CONFIG`(설정 파일 경로).

### 안내 음성

낙상이 의심되면 서버가 안내 음성을 만들어 라즈베리파이 스피커로 보냅니다. 순서대로 시도합니다.

1. `prompt_wav`로 지정한 녹음 파일 (어떤 wav든 16kHz mono로 바꿔 보냄)
2. 운영체제 합성 음성: Windows는 System.Speech(한국어 음성, 예: Microsoft Heami), Linux는 `espeak-ng`
3. 알림음 (안내 문장은 현장 화면에만 표시)

서버 시작 직후 미리 만들어 둡니다. 어느 것을 쓰는지는 `/api/state`의 `prompt_source`로 확인합니다.

## 3. 실행

```powershell
python -m server
```

`--config 경로`, `--host`, `--port`, `--log-level DEBUG`를 줄 수 있습니다. 기본은 `0.0.0.0:8000`입니다.
모델을 불러온 뒤(몇 초) 접속을 받기 시작합니다. 확인:

- http://127.0.0.1:8000/api/health → `{"ok": true, ...}`
- http://127.0.0.1:8000/kiosk → 현장 화면 (라즈베리파이가 연결되기 전에는 '연결 끊김')

라즈베리파이 쪽에는 서버 주소와 같은 토큰을 설정합니다. 서버 PC의 주소는 `ipconfig`의
'IPv4 주소'(예: `192.168.0.10`)입니다. edge는 `ws://192.168.0.10:8000/ws/device`로 접속하고,
Chromium은 `http://192.168.0.10:8000/kiosk`를 엽니다. 공유기 설정에서 PC 주소를 고정해 두면 편합니다.

### 라즈베리파이 없이 시험 (가짜 edge)

```powershell
# 낙상 영상 → 안내 음성 → '괜찮아요' 대답 → 해제
python -m server.fake_edge --token <토큰> --video fall.avi --hold 3 --reply safe.wav
# 대답 없음 → 두 번 묻고 경보 → 현장 화면에서 '괜찮아요' 버튼으로 해제
python -m server.fake_edge --token <토큰> --video fall.avi --hold 3
```

`--hold 3`은 영상이 끝난 뒤 마지막 장면을 3초 더 보냅니다(넘어진 사람이 계속 누워 있다고 가정).
`--reply`는 16kHz가 아니어도 됩니다. 그 밖에 `--speaker-fail`, `--mic-fail`, `--no-playback-done`으로 장치 고장을 흉내 냅니다.
가짜 edge는 소리를 스피커로 내지 않고(길이만큼 기다렸다가 재생 완료를 보냄) 아무것도 저장하지 않습니다.

## 4. Windows 방화벽 (8000 포트 허용)

라즈베리파이가 PC에 접속하려면 8000 포트를 열어야 합니다. 처음 실행할 때 Windows 보안 경고가 뜨면
**개인 네트워크**만 허용하세요. 또는 관리자 PowerShell에서:

```powershell
New-NetFirewallRule -DisplayName "Fall detection server 8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private
```

Wi-Fi가 '공용 네트워크'로 잡혀 있으면 위 규칙이 적용되지 않습니다. 설정 → 네트워크 및 인터넷 → 사용 중인 Wi-Fi →
'개인 네트워크'로 바꿉니다. 규칙 지우기: `Remove-NetFirewallRule -DisplayName "Fall detection server 8000"`.
같은 공유기(LAN) 안에서만 쓰고, 공유기에서 이 포트를 외부로 열지(포트 포워딩) 마세요.

## 5. 동작

### 낙상 판정
장면마다 MediaPipe로 어깨·엉덩이를 찾아 몸통 각도, 종횡비(팔 제외 영역), 하강 속도(0.3초 구간, 몸통 길이/초)를 계산합니다.
하강 속도 1.5 이상 → 하강 감지 → 누운 자세 또는 사람이 안 보이는 상태가 2초 이어지면 **낙상 의심**.
일어선 자세가 0.5초 이어지면 정상 복귀, 5초가 지나도 정해지지 않으면 시간 초과로 정상 복귀합니다.
시간은 edge가 붙인 촬영 시각을 쓰고, 처리가 밀리면 최신 장면만 처리합니다. 다시 접속하면 판정 기록을 새로 시작합니다.

### 음성 확인
낙상 의심 → 안내 음성 전송(`play_begin`/`AUDIO_OUT`/`play_end`) → `playback_done`을 받은 뒤 마이크 켜기 →
5초 동안 실시간 인식 → 마이크 끄기 → 판단. 판단은 자유 인식과 응답 문구로 제한한 인식을 함께 쓰며, 문구 제한 인식은
모든 단어 신뢰도가 0.9 이상일 때만 인정합니다. 도움 요청·부정 표현("안 괜찮아요")을 먼저 검사합니다.
대답이 없거나 불분명하면 한 번 더 묻습니다(최대 2번).

| 결과 | 처리 |
| --- | --- |
| 괜찮다 | 해제 (`CLEARED_VOICE`) |
| 도움 요청 | 경보 (`ALERT_HELP`) |
| 2번 모두 무응답·불분명 | 경보 (`ALERT_NO_REPLY`) |
| 마이크·스피커 오류, 재생 완료 없음, 완전한 무음, 인식 모델 없음 | 경보 (`ALERT_VOICE_FAIL`) |
| 음성 확인 중 연결 끊김 | 경보 (`ALERT_DISCONNECTED`) |
| 현장 화면 '괜찮아요' 버튼 | 확인 중이면 해제(`CLEARED_BUTTON`), 경보 중이면 경보 해제 |

경보가 나면 edge에 경보음을 `loop=true`로 보내고 보호자에게 알립니다. 화면 버튼으로 해제하면 `play_stop`을 보내고
보호자에게 '경보 해제'를 알립니다. 경보 중 다시 접속한 장치에도 경보음을 다시 보냅니다.

### 보호자 알림
텔레그램으로 텍스트만 보냅니다. 실패하면 30초·2분·10분(이후 10분마다) 뒤 다시 보내고, 늦게 보낸 알림에는
'지연 전송'과 원래 발생 시각을 적습니다. 다시 보내기 전에 현장에서 해제했으면 보내지 않습니다.

### 장치 감시
edge 연결 끊김, `status` 끊김(6초), 카메라·마이크·스피커 문제, 영상 끊김(5초), 모델을 불러오지 못함을 현장 화면에
'장치 점검'으로 보여 주고, 180초 이상 이어지면 보호자에게 '장치 이상', 복구되면 '정상 복구'를 알립니다.

### 기록 (SQLite, 기본 `data/server_events.db`)
발생 시각, 판정 결과, 음성 확인 결과(분류), 알림 결과만 남깁니다. 영상·음성 원본은 저장하지 않고 인식 문장 원문도 기본으로는
저장하지 않습니다(`store_voice_text`). 서버를 다시 켜면 처리 중이던 이벤트를 '중단'(`INTERRUPTED`)으로 정리하고,
해제되지 않은 경보를 다시 띄우며, 보내는 도중 끊긴 알림을 다시 보냅니다.

## 6. API

| 경로 | 설명 |
| --- | --- |
| `GET /ws/device` | edge 접속 (`Authorization: Bearer <토큰>`, 틀리면 401) |
| `GET /kiosk` | 현장 화면 |
| `GET /api/health` | 서버가 떠 있으면 200 `{"ok": true, "protocol": 1, "state": ..., "devices_connected": n}` |
| `GET /api/state` | `state`(NORMAL·SUSPECT·ALARM), `view`(normal·suspect·alarm·check·disconnected), 진행 중 이벤트, 장치 문제, 장치별 상태 |
| `POST /api/clear` | 화면 '괜찮아요' 버튼. 다른 웹 페이지가 몰래 보내지 못하게 `X-Kiosk-Clear: 1` 헤더가 있어야 합니다 |
| `GET /api/events?limit=20` | 최근 이벤트. `&kind=system`이면 장치 이상·복구 기록 |

## 7. 테스트

저장소 최상위에서:

```powershell
python -m unittest discover -s server/tests -t .
```

자세 추정·음성 인식은 대역으로 바꿔 끼워 모델 없이 돕니다. 특징·판정·음성 판단·이벤트 흐름 단위 테스트와, 가짜 edge가
실제 WebSocket으로 접속해 영상 → 낙상 의심 → 안내 재생 → 마이크 소리 → 해제/경보 → 화면 버튼 해제까지 확인하는 테스트가 있습니다.
실제 Vosk 모델과 합성 음성(safe1~4, help1~3, negated, unclear, other1~6, quiet)이 있으면 평가와 같은 결과가 나오는지도 확인합니다:

```powershell
$env:SERVER_TEST_VOSK_MODEL = "assets/models/vosk-model-small-ko-0.22"
$env:SERVER_TEST_TTS_DIR = "<합성 음성 폴더>"
python -m unittest server.tests.test_voice -v
```

## 8. 문제 해결

| 증상 | 확인할 것 |
| --- | --- |
| 서버가 바로 끝나며 'DEVICE_TOKEN이 설정되지 않았습니다' | `server/config.toml`의 `device_token` 또는 환경변수 `DEVICE_TOKEN` |
| 'address already in use' / 포트 사용 중 | 이미 켜진 서버가 있는지, `--port`로 다른 포트 사용(edge 설정도 함께 변경) |
| edge 로그에 HTTP 401 | 양쪽 토큰이 같은지 (앞뒤 공백 주의) |
| edge가 4400으로 끊김 | 규약 버전·메시지 형식이 다름. 서버 로그의 '규약 위반' 줄과 edge가 받은 `error` 메시지 확인 |
| 4409로 끊겼다 붙었다 반복 | 같은 `device_id`를 쓰는 edge가 두 대 켜져 있음 |
| 라즈베리파이에서 접속 안 됨 | 방화벽(4절), PC 주소, 같은 공유기인지. 라즈베리파이에서 `curl http://<PC 주소>:8000/api/health` |
| 화면에 '영상 분석 모델을 불러오지 못했습니다' | `pose_model_path` 경로, `pip install mediapipe` |
| 화면에 '음성 인식 모델을 불러오지 못했습니다' | `vosk_model_path`가 압축을 푼 폴더(안에 `am`, `conf` 등)를 가리키는지. 이 상태에서는 낙상 의심이 바로 경보로 갑니다 |
| 안내가 알림음으로만 나옴 | 서버 로그의 합성 음성 경고. Windows: 설정 → 시간 및 언어 → 음성에서 한국어 음성 설치, 또는 `prompt_wav` 지정. Linux: `sudo apt install espeak-ng` |
| '영상이 들어오지 않습니다' | edge 카메라 상태, 네트워크 속도. `/api/state`의 `frames_received`·`frames_dropped` |
| '장치 상태 보고가 끊겼습니다' | edge 프로그램이 멈췄거나 네트워크가 끊김 |
| 화면이 '연결 끊김'(서버에 연결할 수 없습니다) | 서버가 꺼졌거나 PC 주소가 바뀜 |
| 화면 오른쪽 위 '알림 미설정' | 텔레그램 토큰·chat_id를 설정하지 않음 (경보는 화면·경보음으로만) |
| 텔레그램 전송 실패 | 서버 로그 '알림 전송 실패', 인터넷 연결, 봇에게 먼저 말을 걸었는지(개인 chat), 그룹이면 봇 초대 |

## 9. 한계

- 판정 기준은 공개 영상(UR Fall)과 합성 음성으로 정했습니다. 실제 설치 환경·노인 목소리로 다시 확인해야 합니다([`docs/EVALUATION.md`](../docs/EVALUATION.md)).
- 영상이 네트워크(LAN)로 나갑니다. 외부망을 거치게 되면 TLS(`wss://`)를 먼저 적용해야 합니다(아직 미구현).
- `/api/clear`는 같은 네트워크의 누구나 부를 수 있습니다(헤더 검사는 다른 웹 페이지의 위조 요청만 막음). LAN 밖에 노출하지 마세요.
- 장치 여러 대를 동시에 연결할 수는 있지만, 이벤트는 한 번에 하나만 처리하고 현장 화면은 하나입니다(설치 위치 한 곳 기준).
