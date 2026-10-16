# edge — 라즈베리파이 입출력 장치

라즈베리파이에서 실행하는 프로그램입니다. 카메라 영상과 마이크 소리를 PC 서버로 보내고, 서버가 보낸 소리(안내 음성·경보음)를 스피커로 재생합니다. 화면은 Chromium 키오스크로 서버의 현장 화면(`/kiosk`)을 엽니다.

자세 추정, 음성 인식, 낙상 판정은 **모두 PC 서버(`server/`)가 합니다.** edge에는 모델이 없습니다. 통신 규약은 [`docs/PROTOCOL.md`](../docs/PROTOCOL.md)와 [`shared/protocol.py`](../shared/protocol.py)를 따릅니다.

```
카메라 ──JPEG(서버가 정한 해상도·fps·품질)──▶
마이크 ──PCM 16kHz, 0.1초 단위(서버가 켤 때만)──▶   PC 서버  ws://<서버>:8000/ws/device
스피커 ◀──PCM(play_begin → AUDIO_OUT → play_end)──
상태   ──2초마다 카메라·마이크·스피커·fps·CPU 온도──▶
화면   Chromium 키오스크 ── http://<서버>:8000/kiosk?device=<이 장치 id>&key=<장치별 키>  (서버에 여러 대가 붙어도 자기 장치만 표시)
```

현장 화면 주소의 `key`는 장치 토큰과 `device_id`로 계산한 장치별 키(`shared/protocol.py`의 `kiosk_key`)입니다. 서버도 같은 토큰으로 같은 값을 계산하므로, 이 키가 붙은 화면만 그 장치의 상태 보기와 '괜찮아요' 해제를 할 수 있습니다. `kiosk.sh`가 `--print-url waiting`으로 이 주소가 든 대기 화면 주소를 받아 열기 때문에 직접 입력할 일은 없습니다.

## 파일

| 파일 | 내용 |
| --- | --- |
| `__main__.py`, `main.py` | `python -m edge` 진입점. 설정을 읽고 장치를 연 뒤 접속 |
| `config.py` | 설정 파일 `edge/config.toml` + 명령행 인자 |
| `camera.py` | picamera2(카메라 모듈) / OpenCV(USB 웹캠·영상 파일), 회전, JPEG, 끊기면 다시 열기 |
| `mic.py` | sounddevice 녹음, 0.1초(3200바이트) 단위로 자르고 녹음 시각 붙이기 |
| `speaker.py` | 재생 순서·반복·중단 관리, sounddevice 출력 또는 가짜 출력(null) |
| `client.py` | WebSocket 클라이언트: hello/welcome, 영상·소리·상태 전송, 서버 명령 처리, 재접속 |
| `update.py` | 서버에서 새 코드를 받는 자동 업데이트 (`python -m edge.update`, 서비스 시작 전에 실행) |
| `config.example.toml` | 설정 예시 (복사해서 `config.toml`로 씀. `config.toml`은 저장소에 올리지 않음) |
| `requirements.txt` | PC에서 실행할 때 필요한 패키지 |
| `deploy/setup.sh` | 한 줄 설치(서버가 `/edge/setup.sh`로 내려 줌): 서버에서 코드 묶음을 받아 풀고 `install.sh` 실행 |
| `deploy/install.sh` | 라즈베리파이 설치 스크립트 (패키지·가상환경·설정·서비스·키오스크·자동 로그인) |
| `deploy/fall-edge.service` | systemd 사용자 서비스 (시작 전 자동 업데이트, 자동 시작·자동 재시작) |
| `deploy/kiosk.sh` | 켜자마자 대기 화면을 띄우고 Chromium 키오스크를 계속 띄워 둠 (한 번에 하나) |
| `deploy/waiting.html` | 대기 화면: 서버가 응답하면 현장 화면으로 넘어감, 설정이 없으면 '설정이 필요합니다' |
| `deploy/requirements-pi.txt` | 라즈베리파이에서 pip로 설치하는 패키지 (나머지는 apt) |
| `VERSION` | (서버에서 받아 설치한 장치에만 있음) 설치된 코드 버전. 있으면 자동 업데이트, 지우면 끔 |
| `tests/` | 자동 테스트와 가짜 서버(`fake_server.py`) |

## 라즈베리파이 설치

대상: 라즈베리파이 5, Raspberry Pi OS 64비트 데스크톱(Bookworm 이후), 카메라 모듈 또는 USB 웹캠, USB 마이크, 스피커, 화면. 라즈베리파이는 서버 PC와 같은 공유기에 연결하고, 처음 설치할 때는 인터넷(apt·pip)도 필요합니다.

### 한 줄 설치 (권장)

1. 서버 PC에서 관리자 화면(`http://<서버>:8000/admin`) → **라즈베리파이 추가** → **설치 명령 보기**를 펼치고, 장치 이름(예: 거실)을 넣은 뒤 **명령 복사**를 누릅니다.
2. 라즈베리파이의 터미널(일반 사용자, `sudo` 없이)에 붙여 넣고 Enter. 명령은 이런 모양입니다.

```bash
curl -fsSL http://192.168.0.10:8000/edge/setup.sh | bash -s -- --token '<장치 토큰>' --name '거실'
```

3. 끝나면 `sudo reboot`. 켜지면 바로 화면이 뜹니다(아래 '켜면 일어나는 일'). 명령 끝에 `--reboot`를 붙이면 설치가 끝나자마자 다시 시작합니다.

명령에는 장치 토큰이 들어 있으니 다른 사람에게 보내거나 공유하지 마세요. `--token`을 빼면 설치 중에 토큰을 물어봅니다(화면에 보이지 않음).

| 옵션 | 기본값 | 설명 |
| --- | --- | --- |
| `--token` | (물어봄) | 서버의 장치 토큰 |
| `--name` | 장치 id | 설치 위치 이름. 한글·공백 가능 |
| `--id` | 컴퓨터 이름(소문자) | 장치 구분자. 영문·숫자·`.`·`_`·`-`. 장치마다 달라야 함 |
| `--dir` | `~/fall-edge` | 설치 폴더 (git 저장소 폴더에는 설치하지 않음) |
| `--reboot` | | 설치가 끝나면 바로 다시 시작 |
| `--server` | 스크립트를 내려 준 서버 | 서버 주소를 직접 줄 때 (`http://<서버>:8000`) |

`setup.sh`가 하는 일: 서버에서 코드 묶음(`/edge/bundle.tar.gz`, 장치 토큰 필요)을 받아 항목을 검사(edge/·shared/ 아래 일반 파일만)한 뒤 설치 폴더에 풀고, `edge/VERSION`을 쓰고, `install.sh --server --token --id --name`을 실행합니다. **같은 명령을 다시 실행하면 업데이트**입니다: `edge/config.toml`과 `.venv`는 그대로 두고 코드만 바꾸며, 주지 않은 장치 id·이름도 그대로 둡니다.

`install.sh`가 하는 일 (몇 번 실행해도 같은 결과):

1. apt 패키지: `python3-picamera2`, `python3-opencv`, `python3-numpy`, `libportaudio2`, `chromium`, `fonts-nanum`, `curl` 등
2. `.venv` 가상환경(`--system-site-packages`)을 만들고 `aiohttp`, `sounddevice`만 pip로 설치
   - numpy·OpenCV·picamera2는 apt 것을 그대로 씁니다. pip로 numpy 2.x가 깔리면 picamera2가 깨질 수 있기 때문입니다.
3. `edge/config.toml`(권한 600): 없으면 예시에서 만들고, 옵션으로 받은 서버 주소·토큰·장치 id·이름만 저장합니다(`python -m edge --save-config`. 다른 설정과 주석은 그대로).
4. 사용자 서비스 `fall-edge` 등록, 로그인하지 않아도 시작되게 linger 설정, 로그 크기 200MB 제한. 서버 주소와 토큰이 있으면 **서비스를 바로 시작(다시 시작)**합니다.
5. 키오스크 자동 실행: `~/.config/autostart/fall-kiosk.desktop` → `edge/deploy/kiosk.sh` (`~/.config/labwc/autostart`는 만들지 않습니다. 사용자 파일이 있으면 시스템 기본 자동 실행을 대신해 작업 표시줄 등이 사라집니다.)
6. 화면 꺼짐 방지(`raspi-config nonint do_blanking 1`)와 데스크톱 자동 로그인(`raspi-config nonint do_boot_behaviour B4`)
7. `--reboot`면 다시 시작, 아니면 다시 시작하면 화면이 뜬다고 안내

### 저장소로 설치 (개발용, 다른 방법)

```bash
git clone <저장소 주소> ~/fall-detection
cd ~/fall-detection
bash edge/deploy/install.sh --server 192.168.0.10:8000 --token '<장치 토큰>' --name '거실'
```

옵션 없이 `bash edge/deploy/install.sh`만 실행하면 예전처럼 예시 설정 파일만 만들고 안내합니다(`nano edge/config.toml`로 `[server] address`, `token`을 채운 뒤 `systemctl --user restart fall-edge`). git으로 받은 폴더에는 `edge/VERSION`이 없으므로 **자동 업데이트를 하지 않습니다**(`git pull`로 직접 업데이트).

설치 뒤 확인:

```bash
.venv/bin/python -m edge --list-devices    # 마이크·스피커 장치 확인
curl $(.venv/bin/python -m edge --print-url health)   # 서버에 닿는지 확인
journalctl --user -u fall-edge -f          # 로그
```

### 켜면 일어나는 일

1. 데스크톱 자동 로그인 → `kiosk.sh`가 **바로** Chromium 키오스크로 라즈베리파이 안의 대기 화면(`edge/deploy/waiting.html`)을 엽니다. 서버가 아직 꺼져 있어도 "서버에 연결하는 중입니다"와 장치 이름·서버 주소·기다린 시간이 보입니다(1분이 넘으면 확인할 것도 보임).
2. 대기 화면이 2초마다 서버의 `/api/health`를 확인하고, 서버가 응답하면 현장 화면(`/kiosk?device=...&key=...`)으로 넘어갑니다.
3. 설정(서버 주소·토큰)을 읽을 수 없으면 대기 화면에 **"설정이 필요합니다"**와 이유가 보입니다. `kiosk.sh`가 30초마다, 그리고 `config.toml`이 바뀌면 바로 다시 읽어 주소가 달라졌으면 브라우저를 새 주소로 다시 띄웁니다(토큰·장치 id를 바꿨을 때도 같음).
4. 브라우저가 꺼지면 3초 뒤 다시 띄웁니다. 자동 실행이 두 번 되어도 `flock`(`$XDG_RUNTIME_DIR/fall-kiosk.lock`)으로 브라우저는 하나만 뜹니다. 자동 실행일 때 `kiosk.sh`의 기록은 `~/.cache/fall-edge/kiosk.log`에 남습니다.
5. 같은 때에 `fall-edge` 서비스가 시작합니다(로그인과 상관없이). 시작 전에 자동 업데이트를 한 번 확인합니다(아래).

서비스는 프로그램이 끝나면 3초 뒤 다시 띄웁니다(`Restart=always`). 단, 설정 오류(종료 코드 2)는 다시 띄워도 같으므로 멈춥니다(`RestartPreventExitStatus=2`). `config.toml`을 고친 뒤 `systemctl --user restart fall-edge`로 다시 시작하세요.

### 자동 업데이트

서버의 코드가 바뀌면(서버 업데이트) 라즈베리파이도 서버와 같은 버전으로 맞춥니다. **`edge/VERSION`이 있을 때만**(한 줄 설치로 받은 장치) 동작합니다.

- **서비스가 시작할 때마다** `ExecStartPre=-.../python -m edge.update`가 서버의 `/edge/version`을 봅니다. 이 장치의 `edge/VERSION`과 다르면 묶음을 받아, 모든 항목을 검사(일반 파일·폴더만, `edge/`·`shared/` 아래 상대 경로만, `..`·절대 경로·링크 거부)한 뒤 임시 폴더에 풀고 파일을 하나씩 바꿉니다. `edge/config.toml`과 `.venv`는 건드리지 않고, `edge/VERSION`은 맨 마지막에 씁니다(중간에 끊기면 다음 시작 때 다시 받음).
- `deploy/requirements-pi.txt`가 바뀌었으면 파일을 바꾸기 전에 `.venv`에 pip로 설치하고, 실패하면 이번 업데이트를 하지 않습니다.
- 요청마다 10초, 묶음 받기 60초, pip 180초 제한. 서버가 꺼져 있거나 실패해도 서비스는 그대로 시작합니다(앞의 `-`).
- **실행 중에**: 서버에 접속했을 때 `welcome`의 `edge_version`이 이 장치와 다르면, 영상을 보내기 전에 종료 코드 3으로 끝나 systemd가 다시 띄우며 업데이트합니다(`SuccessExitStatus=3`). 재생이나 음성 확인 중에는 하지 않고, 같은 버전으로 30분 안에 시도한 적이 있으면 다시 끝내지 않습니다(업데이트가 계속 실패해도 반복해서 다시 시작하지 않게). 시도 기록: `~/.cache/fall-edge/update-attempt.json`.
- 서비스 설정 파일(`fall-edge.service`)이나 `kiosk.sh`가 바뀐 업데이트는 각각 한 줄 설치 명령을 다시 실행했을 때, 다음 부팅 때 반영됩니다.

**자동 업데이트 끄기**: `rm ~/fall-edge/edge/VERSION` (다시 켜려면 한 줄 설치 명령을 다시 실행). 직접 한 번 확인: `.venv/bin/python -m edge.update`.

## 설정

`edge/config.toml` (예시: [`config.example.toml`](config.example.toml)). 명령행 인자가 설정 파일보다 우선합니다.

| 설정 파일 | 명령행 | 기본값 | 설명 |
| --- | --- | --- | --- |
| `[server] address` | `--server` | (필수) | `192.168.0.10`, `192.168.0.10:8000`, `ws://.../ws/device` 모두 됨. 포트 생략 시 8000 |
| `[server] token` | `--token` | (필수) | 서버와 같은 장치 토큰. 명령행보다 설정 파일에 두기를 권장(`ps`에 보이지 않게) |
| `[device] id` | `--device-id` | 컴퓨터 이름 | 장치 구분자. 장치마다 달라야 함 |
| `[device] name` | `--name` | 빈 값 | 설치 위치(예: 거실) |
| `[camera] backend` | `--camera` | `auto` | `auto`(카메라 모듈이 있으면 picamera2, 없으면 OpenCV) / `picamera2` / `opencv` / `none` |
| `[camera] source` | `--source` | 기본 카메라 | USB 웹캠 번호 또는 영상 파일 경로 |
| `[camera] rotation` | `--rotation` | `0` | 0·90·180·270 (시계 방향) |
| `[camera] loop_file` | `--no-loop` | `true` | 영상 파일이 끝나면 처음부터 다시 |
| `[microphone] enabled` | `--no-mic` | `true` | 마이크 사용 |
| `[microphone] device` | `--mic-device` | 기본 장치 | 번호 또는 이름 일부 |
| `[speaker] backend` | `--speaker` | `sounddevice` | `null`이면 소리를 내지 않고 재생한 것처럼 처리(시험용) |
| `[speaker] device` | `--speaker-device` | 기본 장치 | 번호 또는 이름 일부 |
| `[speaker] volume` | `--volume` | `1.0` | 0.0~1.0 |
| `[log] level` | `--log-level` | `INFO` | |
| | `--list-devices` | | 오디오 장치 목록을 보여 주고 끝냄 |
| | `--print-url kiosk\|health\|ws\|http\|waiting` | | 설정된 서버 URL을 출력하고 끝냄(키오스크 스크립트가 씀). `kiosk`는 `device`와 장치별 `key`가 붙은 주소이며 토큰이 있어야 함. `waiting`은 대기 화면 주소(설정이 틀리면 `error`가 붙은 주소와 종료 코드 2) |
| | `--save-config` | | `--server`·`--token`·`--device-id`·`--name` 값만 설정 파일에 저장하고 끝냄(설치 스크립트가 씀. 한글·따옴표도 바르게 저장, 다른 설정·주석 유지, 권한 600) |

`token`이 예시 값(`__DEVICE_TOKEN__`) 그대로면 토큰이 없는 것으로 봅니다.

영상 해상도·fps·JPEG 품질은 edge에서 정하지 않고 **서버가 `welcome`/`config`로 정합니다**(기본 640×480, 10fps, 품질 80).

## 동작 요약

- **접속**: `ws://<서버>:8000/ws/device`에 `Authorization: Bearer <토큰>`으로 접속 → `hello` → 서버의 `welcome` 영상 설정 적용 → 영상·상태 전송 시작. 10초마다 ping.
- **재접속**: 끊기면 1 → 2 → 5 → 10초(이후 10초) 간격으로 다시 접속. 토큰 거절(401), 규약 오류(4400), 같은 `device_id` 접속(4409)은 다시 해도 같은 결과라 10초 간격.
- **영상**: 카메라는 계속 읽고, 서버 fps에 맞춰 골라 JPEG로 보냅니다. 이전 장면을 아직 보내는 중이면 새 장면은 버립니다(쌓지 않음, 인코딩도 하지 않음). 시각은 촬영 직후 `time.monotonic()`.
  - 약한 Wi-Fi에서 오래된 장면이 송신 버퍼에 쌓이지 않게, 커널에는 아직 보내지 못한 데이터를 32KB까지만 두고(리눅스 `TCP_NOTSENT_LOWAT`) asyncio 쓰기 버퍼도 16KB로 줄입니다. 장면이 버퍼를 다 떠날 때까지는 '보내는 중'으로 보고 그동안 찍힌 장면을 버리므로, 서버는 늘 최근 장면을 받고 ping/pong도 밀리지 않습니다. WebSocket 압축은 쓰지 않습니다(JPEG는 거의 줄지 않음).
  - 해상도는 **회전 전 기준**입니다. 카메라가 더 크게 주면 비율을 유지해 줄이고, 작으면 키우지 않습니다. 90·270도 회전이면 세로 영상이 됩니다.
  - 서버가 해상도나 fps를 바꾸면 카메라를 그 값으로 다시 엽니다(품질만 바뀌면 다시 열지 않음). 영상 파일은 fps만 바뀌면 다시 열지 않습니다.
  - 영상 파일은 파일의 fps대로(실시간처럼) 내보내고, 시각은 장면이 나올 예정 시각입니다.
  - 카메라가 3초 동안 장면을 주지 않거나 열리지 않으면 1·2·5·10초 간격으로 다시 엽니다. 예상하지 못한 오류도 기록한 뒤 다시 엽니다.
- **장치 감시**: 서버 접속 여부와 상관없이 2초마다 확인해, 다시 시작해야만 고칠 수 있는 문제면 프로그램을 끝내 systemd가 다시 띄우게 합니다(종료 코드 1).
  - 카메라 스레드가 60초 넘게 멈추거나(드라이버 정지) 스레드가 죽었을 때.
  - USB 마이크·스피커를 60초 넘게 쓰지 못했고 그사이 사운드 카드 목록(`/proc/asound/cards`)이 바뀌었을 때. PortAudio는 시작할 때 한 번만 장치를 찾으므로 뺐다 꽂은 장치는 다시 시작해야 쓸 수 있습니다. 카드 목록이 그대로면(장치가 아예 없거나 다른 프로그램이 쓰는 중) 다시 시작해도 같으므로 끝내지 않고 계속 다시 엽니다. 서버가 켠 음성 확인(마이크 전송)이나 재생이 진행 중이면 끝날 때까지 기다립니다.
- **마이크**: 장치는 계속 열어 두지만(고장을 바로 알기 위해), 서버가 `mic enabled=true`를 보낸 동안에만 16kHz mono s16le를 0.1초(3200바이트) 단위로 보냅니다. 시각은 각 조각 첫 샘플의 녹음 시각.
- **스피커**: `play_begin` → `AUDIO_OUT` → `play_end`를 받은 순서대로 재생하고, 장치 출력 지연까지 기다린 뒤 `playback_done`을 보냅니다(안내 음성이 녹음되지 않게). `play_end` 없이 다음 `play_begin`이 오면 앞 소리는 받은 데까지만 재생하고 끝냅니다.
  - `loop: true`면 `play_stop`까지 반복합니다. 반복 중에 새 `play_begin`이 오면 반복을 끊고(`playback_done`에 `stopped: true`) 새 소리를 재생합니다.
  - `play_stop`이면 즉시 멈추고 받은 소리를 버리며, 끝나지 않은 id마다 `playback_done`(`stopped: true`)을 보냅니다.
- **연결이 끊기면** 재생과 마이크 전송을 멈춥니다. 다시 접속해도 마이크는 서버가 다시 켤 때까지 꺼져 있습니다.
- **상태**: 2초마다 `camera_ok`·`mic_ok`·`speaker_ok`, 문제가 있으면 `*_error`(한국어 설명), 실제 전송 `fps`, `uptime_s`(프로그램 실행 시간), 라즈베리파이면 `cpu_temp_c`.
- **개인정보**: 영상·소리를 파일로 저장하지 않습니다. 메모리에서 보내기만 합니다.

## PC에서 시험하기 (Windows/macOS/Linux)

라즈베리파이 없이 PC의 웹캠·마이크나 영상 파일로 실행할 수 있습니다. 저장소 최상위 폴더에서:

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r edge/requirements.txt      # macOS/Linux: .venv/bin/python
```

**가짜 서버로 시험** (실제 서버 없이): 터미널 두 개에서

```bash
.venv/Scripts/python -m edge.tests.fake_server --port 8000 --token test-token --demo
```

```bash
.venv/Scripts/python -m edge --server 127.0.0.1 --token test-token --source 0 --speaker null
```

가짜 서버는 2초마다 받은 영상 수·fps·지연과 마지막 상태를 출력합니다. `--demo`를 주면 접속마다 아주 작은 소리 재생 → `playback_done` 확인 → 마이크 2초 켜기(받은 조각 수·간격·소리 크기 출력) → 반복 재생과 `play_stop`을 차례로 해 봅니다.

- 웹캠 대신 영상 파일: `--source C:/path/fall.avi` (파일 속도대로 반복 재생, 한 번만 하려면 `--no-loop`)
- 소리를 실제로 들으려면 `--speaker null`을 빼고, 처음에는 `--volume 0.1`처럼 작게 시작하세요.
- 카메라나 마이크 없이: `--camera none`, `--no-mic`

**실제 PC 서버에 연결**: 서버 주소와 토큰만 바꿉니다. `--server 127.0.0.1 --token <서버의 DEVICE_TOKEN>`

**자동 테스트** (저장소 최상위에서, 약 20초):

```bash
.venv/Scripts/python -m unittest discover -s edge/tests -t .
```

가짜 서버에 실제로 접속해 hello/welcome, 영상 해상도·fps·촬영 시각, 상태 전송, 마이크 켜고 끄기, 재생·반복·중단과 `playback_done`, 끊김 뒤 재접속(재생·마이크 중지), 잘못된 토큰, 규약 버전 불일치, 송신 버퍼 한도, 장치 감시(카메라 정지, USB 마이크·스피커 다시 꽂기)를 확인합니다. 소리는 가짜 출력(null)으로만 재생하고, 마이크·스피커 장치 시험은 가짜 sounddevice로 합니다.

자동 업데이트(`test_update.py`)는 작은 HTTP 서버(빈 포트)가 버전·묶음을 내주고 임시 설치 폴더로 확인합니다: VERSION이 없으면 건너뜀, 같은 버전이면 받지 않음, 새 버전이면 파일 교체(config.toml·.venv 그대로, VERSION 맨 마지막), 위험한 항목(`../`, 절대 경로, edge/·shared/ 밖, 링크)이면 아무것도 쓰지 않음, requirements가 바뀌면 파일보다 먼저 pip(가짜), 토큰 거절·서버 꺼짐·깨진 묶음, 그리고 `welcome`의 `edge_version`이 다를 때 종료 코드 3(최근 시도·개발 폴더·재생 중이면 그대로). `test_config.py`는 설정 저장(`--save-config`)과 대기 화면 주소(`--print-url waiting`)도 확인합니다.

## 문제 해결

| 증상 | 확인할 것 |
| --- | --- |
| `서버가 장치 토큰을 거절했습니다(401)` | `config.toml`의 `token`이 서버의 장치 토큰과 같은지 |
| `서버 연결 문제: Cannot connect to host ...` | 서버가 실행 중인지, 주소·포트(8000)가 맞는지, 같은 공유기에 있는지, PC 방화벽이 8000 포트를 막지 않는지. `curl $(.venv/bin/python -m edge --print-url health)`로 확인 |
| `같은 device_id(...)로 다른 장치가 접속해 연결이 끊겼습니다` | 장치 두 대가 같은 `[device] id`를 쓰고 있음. 하나를 바꾸세요 |
| `규약 버전이 다릅니다` | edge와 서버의 저장소 버전을 맞추세요(`shared/protocol.py`의 `PROTOCOL_VERSION`) |
| `라즈베리파이 카메라를 열 수 없습니다` | 케이블 방향, `rpicam-hello --list-cameras`로 인식 확인. 다른 프로그램(옛 `fall-detect` 서비스 등)이 카메라를 쓰고 있지 않은지 |
| picamera2에서 `numpy.dtype size changed` 등 | 가상환경에 pip numpy가 깔림. `.venv/bin/pip uninstall numpy opencv-python opencv-python-headless` 후 apt 것을 쓰세요 |
| USB 웹캠을 쓰고 싶은데 카메라 모듈이 잡힘 | `[camera] backend = "opencv"`, `source = 0` |
| `카메라에서 영상을 받지 못합니다` | USB 연결·전원 확인. 1·2·5·10초 간격으로 저절로 다시 엽니다 |
| `마이크를 열 수 없습니다` / `스피커를 열 수 없습니다` | `--list-devices`로 장치 확인 후 `device` 지정. 16kHz를 직접 지원하지 않는 장치는 `default`나 `pipewire` 장치를 고르세요(PipeWire가 변환). USB 장치를 뺐다 꽂았으면 60초 안에 저절로 다시 시작해 새 장치를 찾습니다(`... 사운드 카드가 바뀌었습니다` 로그) |
| 소리가 나지 않음 | `volume`, `alsamixer`/작업 표시줄 음량, 출력 장치(HDMI·3.5mm·USB) 선택. 로그의 `스피커 열림: ...` 장치 이름 확인 |
| 한 줄 설치: `서버에 연결할 수 없습니다` | 서버 PC가 켜져 있는지, 같은 공유기인지, PC 방화벽이 8000 포트를 막지 않는지. 라즈베리파이에서 `curl http://<서버>:8000/edge/version` |
| 한 줄 설치: `서버가 장치 토큰을 거절했습니다 (HTTP 401)` | 관리자 화면 '라즈베리파이 추가'에서 명령을 다시 복사(서버의 장치 토큰이 바뀌었을 수 있음) |
| 한 줄 설치: `root(sudo)가 아닌 일반 사용자로 실행하세요` | `curl ... \| sudo bash`가 아니라 `curl ... \| bash -s -- ...`로 실행 |
| 켜도 로그인 화면에서 멈춤 / 화면이 안 뜸 | 데스크톱 자동 로그인: `sudo raspi-config` → System Options → Boot / Auto Login → Desktop Autologin. 자동 실행 항목 `~/.config/autostart/fall-kiosk.desktop`이 있는지 |
| 대기 화면 '서버에 연결하는 중입니다'에서 넘어가지 않음 | 화면의 서버 주소가 맞는지, 서버가 켜져 있는지, 같은 공유기인지. `curl $(.venv/bin/python -m edge --print-url health)` |
| 대기 화면 '설정이 필요합니다' | 화면의 이유대로 `edge/config.toml`의 `[server] address`·`token`을 채우거나 한 줄 설치 명령을 다시 실행. 고치면 30초 안에(파일이 바뀌면 바로) 화면이 바뀜 |
| 키오스크 화면이 아예 뜨지 않음 | `~/.cache/fall-edge/kiosk.log` 확인. 터미널에서 `edge/deploy/kiosk.sh`를 직접 실행해 메시지 확인(이미 떠 있으면 '이미 실행 중'으로 끝남) |
| 현장 화면이 상태를 못 가져옴(권한 오류) | `config.toml`의 `token`·`[device] id`가 서버에 등록된 값과 같은지. 설정 파일을 바꾸면 키오스크가 새 주소로 다시 뜸(주소의 `key`가 바뀜) |
| 자동 업데이트가 안 됨 | `edge/VERSION`이 있는지(git 설치는 업데이트 안 함), `journalctl --user -u fall-edge`의 `edge.update` 로그, `.venv/bin/python -m edge.update`로 직접 실행 |
| 업데이트 뒤 프로그램이 이상함 | 한 줄 설치 명령을 다시 실행(코드·서비스 파일을 다시 깔고 설정은 유지). 자동 업데이트를 끄려면 `edge/VERSION` 삭제 |
| 화면이 꺼짐 | `sudo raspi-config` → Display Options → Screen Blanking → 끄기 |

로그: `journalctl --user -u fall-edge -f` (자세히 보려면 `[log] level = "DEBUG"`).

## 아직 확인하지 못한 것

- 라즈베리파이 실기기에서의 설치 스크립트, picamera2 카메라 모듈, systemd 사용자 서비스, 키오스크 자동 실행, 화면 꺼짐 방지는 **아직 실기기에서 확인하지 못했습니다**(Windows PC에서 문법만 확인). 실기기 설치 후 위 순서대로 점검이 필요합니다.
- 한 줄 설치(`setup.sh`)는 Windows의 Git Bash에서 가짜 서버로 받기·검사·풀기·다시 실행(업데이트)·토큰 거절까지 확인했고, `install.sh`(apt·sudo·systemctl·raspi-config)는 실기기에서 확인이 필요합니다. 특히 `raspi-config nonint do_boot_behaviour B4`(자동 로그인), `ExecStartPre` 업데이트와 `TimeoutStartSec`, 파일(`file://`) 대기 화면에서 서버 확인(`fetch` no-cors)과 현장 화면으로 넘어가기, `flock` 중복 실행 방지.
- 외부망을 거치면 TLS(`wss://`)가 필요합니다. edge는 `wss://` 주소를 받을 수 있지만 서버 쪽 TLS 구성은 아직 없습니다.
