# PC 서버 (`server/`)

라즈베리파이(edge)가 WebSocket으로 보내는 영상·음성을 PC에서 실시간으로 처리합니다.
낙상 판정, 음성으로 상태 확인, 경보·보호자 알림, 기록, 현장 화면(`/kiosk`), 관리자 화면(`/admin`)을 모두 이 프로그램이 맡습니다.
**라즈베리파이 여러 대**가 함께 붙을 수 있고, 장치마다 판정·음성 확인·경보가 따로 돕니다.
통신 규약은 [`docs/PROTOCOL.md`](../docs/PROTOCOL.md)와 [`shared/protocol.py`](../shared/protocol.py)를 따릅니다.

```
라즈베리파이(여러 대) ──영상 JPEG 10fps──▶ 자세 추정(MediaPipe) → 특징 → 낙상 판정   (장치마다)
                    ──마이크(요청 시)───▶ 음성 인식(Vosk) → 응답 판단             (장치마다)
                    ◀──안내 음성·경보음──  이벤트 처리 → 기록(SQLite), 보호자 알림(텔레그램)
 Chromium(각 장치) ──HTTP──▶ 현장 화면 /kiosk?device=<장치 id>&key=<장치별 키>
 관리자 PC 브라우저 ─HTTP──▶ 관리자 화면 /admin (모든 장치)
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

requirements 파일 첫 줄의 `# -*- coding: utf-8 -*-`는 지우지 마세요. 한국어 Windows에서 pip가 한글 주석을
cp949로 읽다가 실패하는 것을 막습니다.

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
| `device_name` | 설치 위치 기본값. 라즈베리파이가 보낸 이름(edge `[device] name`)이 있으면 그것을 씁니다 |
| `admin_password` | 관리자 화면 비밀번호. 비워 두면 서버 PC에서 직접 연 브라우저(`127.0.0.1`)에서만 관리자 화면을 열 수 있습니다. 터널을 쓰면 꼭 설정하세요 |
| `public_url` | 보호자가 접속하는 서버 주소(예: `http://192.168.0.10:8000`). 설정하면 텔레그램 경보에 보호자 웹 링크가 붙습니다 |
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

`--config 경로`, `--host`, `--port`, `--log-level DEBUG`, `--log-file data/logs/server.log`(10MB씩 5개까지 돌려 씀,
환경변수 `FALL_LOG_FILE`)를 줄 수 있습니다. 기본은 `0.0.0.0:8000`입니다.
모델을 불러온 뒤(몇 초) 접속을 받기 시작합니다. 확인:

- http://127.0.0.1:8000/api/health → `{"ok": true, ...}`
- http://127.0.0.1:8000/admin → 관리자 화면 (모든 장치 상태, 기록, 장치 이상)
- 각 장치의 현장 화면은 관리자 화면 장치 카드의 '현장 화면 보기'로 엽니다(장치별 키가 붙은 주소)

라즈베리파이 쪽에는 서버 주소와 같은 토큰을 설정합니다. 서버 PC의 주소는 `ipconfig`의
'IPv4 주소'(예: `192.168.0.10`)입니다. edge는 `ws://192.168.0.10:8000/ws/device`로 접속하고,
Chromium은 `http://192.168.0.10:8000/kiosk?device=<자기 장치 id>&key=<장치별 키>`를 엽니다(edge가 토큰으로
키를 계산해 붙임). 현장 화면에는 로그인이 없으므로, 같은 공유기에 있는 다른 사람이 경보를 끄거나 다른 방 상태를 보지
못하게 장치마다 다른 키를 요구합니다.
공유기 설정에서 PC 주소를 고정해 두면 편합니다.

라즈베리파이를 여러 대 붙일 때는 edge 설정의 `[device] id`를 장치마다 다르게, `name`을 설치 위치(거실·안방 등)로
정합니다. 같은 `id`로 두 대가 접속하면 나중에 접속한 쪽이 이전 연결을 끊습니다.

### 관리자 화면 (`/admin`)

여러 장치를 한 화면에서 봅니다. 1초마다 새로 고칩니다.

- 위쪽 요약: 연결된 장치 수, 경보·낙상 의심·점검 필요 장치 수. 경보가 있으면 빨간 띠와 탭 제목 `(경보 n)`
- 장치 카드(경보 → 낙상 의심 → 연결 끊김 → 점검 → 정상 순): 상태, 진행 중인 음성 확인 단계, 카메라·마이크·스피커,
  영상 처리 속도, 최근 24시간 낙상 의심·경보 건수, 장치 문제, '현장 화면 보기', '관리자 확인 후 해제' 버튼
- 서버를 다시 켜도 전에 접속했던 장치는 '연결 끊김'으로 남아 감시됩니다. 치운 라즈베리파이는 연결이 끊긴 상태에서
  '목록에서 지우기'로 지웁니다(지우지 않으면 계속 '장치 이상'으로 알림)
- 최근 낙상 의심 기록(장치 이름, 결과, 보호자 알림, 해제 주체), 장치 이상·복구 알림 기록
- 영상·음성 원본은 보여 주지 않습니다(저장하지도 않음)

접근: `admin_password`를 설정하면 다른 컴퓨터에서도 로그인해서 볼 수 있습니다. 로그인은 30일 유지되고 서버를 다시
켜도(자동 업데이트 포함) 유지됩니다. 로그아웃하거나 `admin_password`를 바꾸면 끊깁니다. 같은 곳에서 5번 틀리면 5분 동안
막습니다. 설정하지 않으면 서버 PC에서 직접 연 브라우저에서만 열립니다. 같은 PC에서 도는 터널(cloudflared 등)을 거쳐 온
요청은 서버 PC 자신으로 보지 않습니다.

관리자 화면 아래쪽 **보호자 계정**에서 보호자 계정을 만들고(아이디·이름·비밀번호 8자 이상), 맡을 장치와
'보호자 웹에서 경보를 끌 수 있음' 권한을 정합니다. 수정·비밀번호 재설정·삭제도 여기서 합니다.

### 보호자 웹 (`/g`)

보호자(가족·요양보호사)가 휴대폰 브라우저로 `http://<서버 주소>:8000/g`에 접속해, 관리자가 만들어 준 계정으로
로그인합니다. 맡은 장치만 보입니다.

- 장치마다 큰 글씨 상태: 정상 감시 중 / 넘어진 것 같아 확인 중(음성 확인 단계) / 경보 / 장치 점검 / 연결 끊김
- 경보일 때 '119 전화' 버튼, 권한이 있으면 '현장 확인 완료 · 경보 끄기' 버튼(기록에 `GUARDIAN:<아이디>`로 남음)
- 최근 기록, 최근 24시간 넘어짐 의심·경보 횟수
- 화면을 켜 두고 '경보 소리 켜기'를 누르면 경보 동안 10초마다 소리·진동으로 알림(휴대폰 브라우저 정책상 한 번 눌러야 소리가 남)
- 영상·음성 원본은 보여 주지 않습니다

보안: 비밀번호는 PBKDF2 해시로만 저장하고, 로그인은 30일 유지되는 서명 쿠키입니다(서버를 다시 켜도 유지).
비밀번호를 바꾸거나 계정을 지우면 그 보호자의 기존 로그인이 바로 끊깁니다. 같은 곳·같은 아이디로 5번 틀리면
5분 동안 로그인을 막습니다(터널을 거친 요청은 터널이 알려 준 원래 주소로 셈). 다른 사이트가 화면을 틀(frame)에
넣지 못하게 막아 둡니다.

**집 밖(인터넷)에서 보려면** 공유기 포트를 열지 말고, HTTPS를 제공하는 터널을 쓰세요. 예: Tailscale(보호자 휴대폰에도
설치, 사설망처럼 접속), Cloudflare Tunnel(공개 https 주소). 터널 주소를 `public_url`로 설정하면 경보 알림의 링크도
그 주소가 됩니다. 터널 계정·설치는 직접 해야 하며 이 저장소에는 설정이 들어 있지 않습니다. 터널로는 보호자 웹에 필요한
경로(`/g`, `/g/*`, `/api/g/*`)만 내보내고, 터널을 쓰면 `admin_password`를 꼭 설정하세요.

### 라즈베리파이 없이 시험 (가짜 edge)

```powershell
# 낙상 영상 → 안내 음성 → '괜찮아요' 대답 → 해제
python -m server.fake_edge --token <토큰> --video fall.avi --hold 3 --reply safe.wav
# 대답 없음 → 두 번 묻고 경보 → 현장 화면에서 '괜찮아요' 버튼으로 해제
python -m server.fake_edge --token <토큰> --video fall.avi --hold 3
```

`--hold 3`은 영상이 끝난 뒤 마지막 장면을 3초 더 보냅니다(넘어진 사람이 계속 누워 있다고 가정).
여러 대를 시험하려면 `--device-id room-a --name 거실`처럼 장치마다 다른 id와 이름으로 여러 개를 실행합니다.
`--reply`는 16kHz가 아니어도 됩니다. 그 밖에 `--speaker-fail`, `--mic-fail`, `--no-playback-done`으로 장치 고장을 흉내 냅니다.
가짜 edge는 소리를 스피커로 내지 않고(길이만큼 기다렸다가 재생 완료를 보냄) 아무것도 저장하지 않습니다.

### 자동 실행 (Windows)

`server\deploy\windows\`의 파일로 서버를 계속 켜 둡니다.

| 파일 | 하는 일 |
| --- | --- |
| `start_server.bat` | 서버 실행. 꺼지면(오류·자동 업데이트) 다시 켬(켜진 지 30초 안에 꺼지면 5초→60초까지 늘려 기다림). 이미 서버가 응답하면 새로 켜지 않음. 설정 오류(종료 코드 2)면 멈추고 마지막 기록을 보여 줌. 기록 `data\logs\server.log`(10MB씩 5개), 갑자기 꺼질 때의 오류는 `data\logs\server-console.log` |
| `install_autostart.bat` | `start_server.bat`를 Windows 로그인 때 자동 실행되게 작업 스케줄러에 등록(현재 사용자, 보통 우선순위, 시간 제한 없음) |
| `install_autostart.bat -WithUpdate` | + 10분마다 자동 업데이트(아래 '자동 배포') |
| `install_autostart.bat -Firewall` | + 방화벽에서 8000 포트 허용(개인 네트워크만). 관리자 권한으로 실행 |
| `install_autostart.bat -Remove` | 등록 지우기 |

안내 음성(Windows 합성 음성)이 사용자 세션에서 동작하므로 '로그인할 때' 실행합니다. 정전 뒤에도 켜지게 하려면
BIOS의 '전원 복구 시 켜기'와 Windows 자동 로그인을 설정하세요. 서버 PC가 절전 모드로 들어가지 않게 전원 설정도 바꿉니다.

### 자동 배포

`server\deploy\windows\update_server.ps1`이 GitHub의 같은 브랜치에 새 커밋이 있는지 보고 반영합니다.

1. 새 커밋이 없으면 아무것도 하지 않습니다. 서버 PC에서 고친 파일이 있거나, 원격 기록이 이어지지 않거나(강제 push),
   브랜치가 아닌 커밋에 있으면(detached HEAD) 건드리지 않습니다.
2. 새 커밋을 임시 작업 폴더(`data\update_test`, git worktree)에 꺼내 `pip install`, 설정 읽기 확인, shared·server 시험을
   돌립니다. 실행 중인 코드 폴더는 시험이 통과할 때까지 바뀌지 않습니다.
3. 통과하면 fast-forward로 반영합니다. 서버가 낙상 확인·경보 중이면(`/api/health`의 `state`가 NORMAL이 아님) 끝날 때까지
   15초마다 확인하며 기다리고, 오래 걸리면 다음 실행 때 다시 시도합니다(`data\update_restart_pending.txt`). 정상이면 이
   설치의 파이썬으로 실행 중인 서버만 끄고, `start_server.bat`가 새 코드로 다시 켭니다.
4. 설치나 시험이 실패하면 패키지를 원래 requirements로 되돌리고 서버는 그대로 둡니다. 실패한 커밋은
   `data\update_failed_commit.txt`에 남겨 다시 시험하지 않습니다(더 새 커밋이 올라오면 그것을 시험). 기록은 `data\logs\update.log`.

`install_autostart.bat -WithUpdate`로 10분마다 돌게 등록하거나 직접 실행합니다(`-DryRun`은 확인만). 한 번 실행은 30분이
넘으면 작업 스케줄러가 멈추고, git이 비밀번호를 물으며 멈추지 않게 대화형 입력을 끕니다(GitHub 인증은 미리 설정).
이 변경을 처음 받을 때 이미 열려 있던 `start_server.bat` 창은 한 번 닫았다 다시 여세요(예전 파일을 읽던 창).
GitHub에 push하면 `.github/workflows/tests.yml`이 같은 시험을 GitHub에서도 돌리므로, 시험이 실패한 커밋은 GitHub에서
먼저 보이고 서버 PC에서도 반영되지 않습니다. 서버가 다시 켜지는 동안(약 10초) 라즈베리파이는 자동으로 다시 접속합니다.

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
누운 자세가 0.3초 미만으로 잠깐 끊긴 것(자세 추정 흔들림)은 누움 시간을 이어서 잽니다.
시간은 edge가 붙인 촬영 시각을 쓰고, 처리가 밀리면 최신 장면만 처리합니다. 잠깐 끊겼다 다시 접속하면 판정을 이어 가고,
라즈베리파이가 재부팅해 시각이 거꾸로 가거나 10초 넘게 끊겼으면 새로 시작합니다. 낙상 의심 뒤에는 이벤트가 끝날 때까지
그 장치의 자세 추정을 쉬어 음성 인식에 CPU를 넘깁니다. 사람이 2초 넘게 안 보이는 빈 방은 0.2초마다(초당 5장)만
사람을 찾고, 보이면 바로 매 장면을 봅니다. 사람이 없는 장면은 자세 추정이 오히려 느리기 때문입니다(약 24ms 대 17ms).
빈 방 장치 한 대의 서버 CPU 사용이 코어 하나의 약 19%에서 8%로 줄었습니다.

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
| 음성 확인 중 서버가 꺼졌다 다시 켜짐 | 경보 (`ALERT_INTERRUPTED`), 다시 켜진 뒤 보호자에게 지연 전송 |
| 현장 화면 '괜찮아요' 버튼 | 확인 중이면 해제(`CLEARED_BUTTON`), 경보 중이면 경보 해제 |

경보가 나면 edge에 경보음을 `loop=true`로 보내고 보호자에게 알립니다. 화면 버튼으로 해제하면 `play_stop`을 보내고
보호자에게 '경보 해제'를 알립니다. 경보 중 다시 접속한 장치에도 경보음을 다시 보냅니다.

### 보호자 알림
텔레그램으로 텍스트만 보냅니다. 실패하면 30초·2분·10분(이후 10분마다) 뒤 다시 보내고, 늦게 보낸 알림에는
'지연 전송'과 원래 발생 시각을 적습니다. 다시 보내기 전에 현장에서 해제했으면 보내지 않습니다.

### 장치 감시
edge 연결 끊김, `status` 끊김(6초), 카메라·마이크·스피커 문제, 영상 끊김(5초), 모델을 불러오지 못함과, 영상은
들어오는데 분석이 멈춤(5초)·깨진 영상만 옴·처리 속도가 너무 낮음(초당 5장 미만, 카메라 쪽인지 서버 PC 부하인지 구분),
낙상 의심을 기록하지 못함(1초마다 다시 시도)을 현장 화면에 '장치 점검'으로 보여 주고, 180초 이상 이어지면 보호자에게
'장치 이상', 복구되면 '정상 복구'를 알립니다.
장치마다 따로 재고 알림에 그 장치의 설치 위치를 적습니다(모델 문제처럼 서버 공통 문제는 따로).

### 여러 장치
장치마다 진행 중인 이벤트를 따로 둡니다. 한 방에서 음성 확인을 하는 동안 다른 방에서 넘어져도 바로 따로 확인하고,
경보음은 넘어진 장치에서만 울리며, 현장 화면 버튼은 자기 장치의 경보만 해제합니다. 관리자 화면에서는 모든 장치를 해제할
수 있고 기록에 해제 주체(`BUTTON`·`ADMIN`·`GUARDIAN:<아이디>`)가 남습니다. 음성 인식은 장치 4대까지 동시에 처리하고,
인식이 밀리면 쌓인 소리를 한꺼번에 넘기며 듣는 시간이 끝났을 때 이미 도착한 소리도 인식합니다.

### 기록 (SQLite, 기본 `data/server_events.db`)
발생 시각, 판정 결과, 음성 확인 결과(분류), 알림 결과만 남깁니다. 영상·음성 원본은 저장하지 않고 인식 문장 원문도 기본으로는
저장하지 않습니다(`store_voice_text`). 서버를 다시 켜면 음성 확인 중이던 이벤트를 경보(`ALERT_INTERRUPTED`)로 올려
알리고, 해제되지 않은 경보를 다시 띄우며, 보내는 도중 끊긴 알림을 다시 보냅니다. `event_retention_days`를 정하면 시작할
때와 하루에 한 번 오래된 기록을 지우되, 해제되지 않은 경보와 보낼 알림이 남은 기록은 남깁니다. 접속했던 장치 목록과
관리자 로그인(토큰 해시)도 이 파일에 둡니다.

## 6. API

| 경로 | 설명 |
| --- | --- |
| `GET /ws/device` | edge 접속 (`Authorization: Bearer <토큰>`, 틀리면 401) |
| `GET /kiosk?device=<id>&key=<키>` | 그 장치의 현장 화면. 키는 `shared/protocol.kiosk_key(토큰, 장치 id)` (edge `--print-url kiosk`, 관리자 화면 링크) |
| `GET /api/health` | 서버가 떠 있으면 200 `{"ok": true, "protocol": 1, "state": ..., "devices_connected": n}` |
| `GET /api/state?device=<id>` | 그 장치의 `state`(NORMAL·SUSPECT·ALARM), `view`(normal·suspect·alarm·check·disconnected), 진행 중 이벤트, 문제. `X-Kiosk-Key` 헤더(그 장치의 키)나 관리자 필요. `device` 없이(장치 목록 `select`)는 관리자만 |
| `POST /api/clear?device=<id>` | 화면 '괜찮아요' 버튼(그 장치만). `X-Kiosk-Clear: 1`(다른 웹 페이지의 위조 요청 방지)과 `X-Kiosk-Key` 필요 |
| `GET /api/events?limit=20&device=<id>` | 그 장치의 최근 이벤트(`X-Kiosk-Key`). 관리자는 `device` 없이 전체, `&kind=system`이면 장치 이상·복구 기록 |
| `GET /admin`, `/admin/login`, `POST /admin/logout` | 관리자 화면과 로그인 |
| `GET /api/admin/overview` | 관리자 화면 데이터: 서버 상태, 요약, 장치별 상태·이벤트·문제·24시간 건수, 최근 기록 (관리자만) |
| `POST /api/admin/clear?device=<id>` | 관리자 해제. `X-Admin-Action: 1` 헤더 필요 (관리자만) |
| `GET /api/admin/guardians`, `POST /api/admin/guardians`, `POST·DELETE /api/admin/guardians/<id>` | 보호자 계정 목록·만들기·수정·삭제 (관리자만, 바꿀 때 `X-Admin-Action: 1`) |
| `DELETE /api/admin/devices/<id>` | 연결이 끊긴 장치를 목록에서 지우기 (관리자만, `X-Admin-Action: 1`). 연결 중이거나 경보·확인 중이면 409 |
| `GET /api/admin/edge-setup` | 라즈베리파이 설치 명령(서버 주소·장치 토큰 포함) (관리자만) |
| `GET /edge/setup.sh` | 라즈베리파이 설치 스크립트 (`edge/deploy/setup.sh`, 서버 주소를 채워서) |
| `GET /edge/version` | 라즈베리파이 코드 묶음 버전 (edge/ + shared/ 내용의 해시) |
| `GET /edge/bundle.tar.gz` | 라즈베리파이 코드 묶음 (`Authorization: Bearer <토큰>` 필요). 시험·캐시·설정 파일은 넣지 않음 |
| `GET /g`, `/g/login`, `POST /g/logout` | 보호자 웹과 로그인 |
| `GET /api/g/overview` | 보호자 웹 데이터: 맡은 장치 상태·이벤트·문제, 최근 기록 (보호자 로그인) |
| `POST /api/g/clear?device=<id>` | 보호자 해제. `X-Guardian-Action: 1` 헤더, 맡은 장치·해제 권한이 있을 때만 |

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
| '영상이 들어오지 않습니다' | edge 카메라 상태, 네트워크 속도. 관리자 화면의 `frames_received`·`frames_dropped` |
| '영상 분석이 멈췄습니다'·'영상 분석이 밀립니다' | 서버 로그의 '장면 처리 중 오류', 서버 PC CPU 사용량(다른 프로그램), 장치 수 |
| '카메라 영상이 느리게 들어옵니다' | 방이 어두워 카메라가 느려졌는지, 와이파이 신호 |
| '영상이 깨져서 들어옵니다' | edge 카메라·네트워크. 관리자 화면의 `decode_errors` |
| 현장 화면에 '화면 주소 확인 필요' | 현장 화면 주소의 키가 틀림. edge 토큰이 서버와 같은지, edge를 새 버전으로 바꿨는지(`--print-url kiosk`) |
| '장치 상태 보고가 끊겼습니다' | edge 프로그램이 멈췄거나 네트워크가 끊김 |
| 화면이 '연결 끊김'(서버에 연결할 수 없습니다) | 서버가 꺼졌거나 PC 주소가 바뀜 |
| 화면 오른쪽 위 '알림 미설정' | 텔레그램 토큰·chat_id를 설정하지 않음 (경보는 화면·경보음으로만) |
| 텔레그램 전송 실패 | 서버 로그 '알림 전송 실패', 인터넷 연결, 봇에게 먼저 말을 걸었는지(개인 chat), 그룹이면 봇 초대 |

## 9. 한계

- 판정 기준은 공개 영상(UR Fall)과 합성 음성으로 정했습니다. 실제 설치 환경·노인 목소리로 다시 확인해야 합니다([`docs/EVALUATION.md`](../docs/EVALUATION.md)).
- 영상이 네트워크(LAN)로 나갑니다. 외부망을 거치게 되면 TLS(`wss://`)를 먼저 적용해야 합니다(아직 미구현).
- 현장 화면 키는 장치 토큰에서 만들고 화면 주소에 들어갑니다. 토큰을 바꾸면 모든 현장 화면 주소가 바뀌므로 edge도 같은
  토큰으로 다시 설정해야 합니다(키만 따로 폐기할 수는 없음).
- 관리자 로그인은 비밀번호 하나만 씁니다(사용자별 계정·기록 없음). HTTPS가 아니므로 관리자·보호자 비밀번호가
  LAN에 그대로 지나갑니다. 집 밖에서 쓸 때는 반드시 HTTPS 터널을 거치세요.
- 보호자 웹은 화면을 켜 둔 동안만 소리·진동으로 알립니다(웹 푸시 알림 없음). 화면을 닫으면 텔레그램 알림에 의존합니다.
- 자동 배포는 이 PC의 Git·인터넷에 의존하고, GitHub Actions 결과를 직접 확인하지는 않습니다(같은 시험을 PC에서 다시 돌림).
- 장치 여러 대는 가짜 edge 3대로 시험했습니다. 실제 라즈베리파이 여러 대와 서버 PC 성능(대수가 늘 때 영상 처리 속도)은 확인하지 않았습니다.
