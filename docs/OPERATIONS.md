# 설치 및 운영 안내

> Raspberry Pi 5 (Raspberry Pi OS Bookworm 64-bit, 데스크톱)를 기준으로 작성했습니다. 실제 장치에서 절차를 검증한 뒤 결과에 맞게 고칩니다.

## 설치

```bash
git clone https://github.com/mandur23/Elderly_Fall_Detection_System.git ~/fall-detect-system
cd ~/fall-detect-system
bash l2_system/setup_pi.sh
```

`setup_pi.sh`는 다음을 처리합니다.

| 단계 | 내용 |
| --- | --- |
| 시스템 패키지 | picamera2, PortAudio, espeak-ng, Chromium 등 |
| 파이썬 환경 | `.venv` (picamera2 사용을 위해 시스템 패키지 공유) |
| 모델 | MediaPipe 자세 모델(`assets/models/`), Vosk 한국어 모델(`data/`) |
| 소리 | 알림음·경보음 생성 (`python -m tools.make_sounds`) |
| 설정 | `config.local.toml` 생성, 내부 API 키 자동 발급 |
| 서비스 | `fall-app`, `fall-detect` 사용자 서비스 등록, 로그인 없이 자동 시작(linger) |
| 화면 | 데스크톱 시작 시 키오스크 화면 자동 실행, 화면 꺼짐 방지 |

설치 후 `config.local.toml`에 텔레그램 토큰, 수신자, 설치 위치 이름을 채웁니다. 이 파일은 저장소에 올리지 않습니다.

## 설치 현장 점검

```bash
.venv/bin/python -m tools.check_device --send-test
```

카메라 해상도·속도·밝기, 자세 모델 추론 시간, 스피커, 마이크 입력 크기, 음성 인식, 텔레그램 전송, 저장 공간, 시간 동기화를 확인합니다. `실패` 항목이 없어야 설치를 마칩니다.

카메라 위치는 다음을 권장합니다(시험 후 갱신).

- 방 전체와 바닥이 보이도록 높은 곳에서 비스듬히 내려다보게 설치
- 역광(창문 정면)을 피하고, 밤에도 사람 윤곽이 보일 정도의 조명 유지
- 카메라를 돌려 설치했다면 `CAMERA_ROTATION` 설정

## 서비스 관리

```bash
systemctl --user status fall-app fall-detect
systemctl --user restart fall-app fall-detect
journalctl --user -u fall-app -u fall-detect -f
```

- `fall-detect`(L3)는 멈추면 30초 안에 워치독이 재시작합니다.
- 두 서비스 모두 비정상 종료 시 3초 후 자동 재시작합니다.
- 로그는 최대 200MB까지 보관합니다.

## 장애 감지와 알림

| 상황 | 현장 화면 | 보호자 알림 |
| --- | --- | --- |
| 감지 프로그램 무응답, 카메라·모델·마이크 오류 | 회색 '장치 점검 필요'와 원인 | `DEVICE_ALERT_AFTER_SECONDS`(기본 180초) 이상 지속 시 '장치 이상', 복구 시 '정상 복구' |
| 알림 전송 실패 | 경보 화면에 '전송 실패, 자동 재전송' | 30초·2분·10분 간격으로 재시도, 지연 전송 표시 |
| 정상 동작 확인 | - | `DAILY_REPORT_TIME` 설정 시 매일 상태 보고 |

외부에서 상태를 확인하려면 `GET http://127.0.0.1:5000/api/health`를 씁니다(문제가 있으면 HTTP 503).

## 데이터

- 이벤트 기록: `data/fall_events.db` (SQLite). 영상·음성 원본은 저장하지 않습니다.
- 장치 이상·복구·일일 보고 기록은 같은 DB의 `system_events` 테이블에 남습니다.
- `EVENT_RETENTION_DAYS`를 설정하면 시작할 때 오래된 기록을 지웁니다. 보관 기간은 개인정보 정책을 정한 뒤 설정합니다.

## 업데이트

```bash
cd ~/fall-detect-system
git pull
.venv/bin/pip install -r requirements.txt -r requirements-device.txt
systemctl --user restart fall-app fall-detect
.venv/bin/python -m tools.check_device --no-sound
```
