"""설치 현장 장치 점검. docs/HARDWARE.md '환경 구축 순서'를 명령 하나로 확인한다.

    python -m tools.check_device              # 전체 점검 (스피커에서 알림음이 나옴)
    python -m tools.check_device --no-sound   # 소리 내지 않음
    python -m tools.check_device --send-test  # 텔레그램 시험 메시지 전송까지

모든 항목이 통과하면 종료 코드 0, 하나라도 실패하면 1.
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
import time
from typing import Callable, List, Optional, Tuple

import requests

import config

OK, WARN, FAIL = "통과", "주의", "실패"
Result = Tuple[str, str, str]  # (항목, 결과, 설명)


class Checker:
    def __init__(self, args):
        self.args = args
        self.results: List[Result] = []
        self.frame = None

    def run(self, name: str, fn: Callable[[], Tuple[str, str]]) -> None:
        print(f"- {name} 점검 중...", flush=True)
        try:
            status, detail = fn()
        except Exception as e:
            status, detail = FAIL, f"{type(e).__name__}: {e}"
        self.results.append((name, status, detail))

    # --- 항목 ---
    def python(self):
        v = sys.version_info
        status = OK if v >= (3, 11) else WARN
        return status, f"Python {platform.python_version()} / {platform.machine()} / {platform.system()}"

    def disk(self):
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        free_gb = shutil.disk_usage(config.DATA_DIR).free / 1e9
        return (OK if free_gb >= 2 else WARN if free_gb >= 0.5 else FAIL), f"여유 공간 {free_gb:.1f} GB"

    def time_sync(self):
        if platform.system() != "Linux":
            return WARN, "Linux가 아니어서 확인하지 않음"
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        if out == "yes":
            return OK, "NTP 시간 동기화됨"
        return WARN, "시간 동기화 안 됨 (알림의 발생 시각이 틀릴 수 있음)"

    def camera(self):
        from l3_perception.camera import create_camera, rotate
        cam = create_camera(config.CAMERA_BACKEND, config.CAMERA_INDEX, config.FRAME_WIDTH,
                            config.FRAME_HEIGHT, config.TARGET_FPS)
        cam.open()
        try:
            frames, start = [], time.monotonic()
            while len(frames) < 20 and time.monotonic() - start < 10:
                f = cam.read()
                if f is not None:
                    frames.append(f)
            elapsed = time.monotonic() - start
        finally:
            cam.close()
        if not frames:
            return FAIL, f"{cam.name}: 프레임을 읽지 못함"
        self.frame = rotate(frames[-1], config.CAMERA_ROTATION)
        h, w = self.frame.shape[:2]
        brightness = float(self.frame.mean())
        detail = f"{cam.name} {w}x{h}, {len(frames) / elapsed:.1f} fps, 밝기 {brightness:.0f}/255"
        if brightness < 30:
            return WARN, detail + " (너무 어두움 - 조명 확인)"
        return OK, detail

    def pose_model(self):
        import numpy as np
        from l3_perception.fall_detector import load_detector
        det = load_detector(config.POSE_MODEL)
        frame = self.frame if self.frame is not None else np.zeros(
            (config.FRAME_HEIGHT, config.FRAME_WIDTH, 3), dtype=np.uint8)
        times, kp = [], None
        for i in range(10):
            t0 = time.monotonic()
            kp = det.detect(frame, time.time() + i * 0.1)
            times.append(time.monotonic() - t0)
        det.close()
        avg_ms = 1000 * sum(times[2:]) / len(times[2:])  # 첫 실행(초기화) 제외
        max_fps = 1000 / avg_ms if avg_ms > 0 else float("inf")
        detail = f"{det.name}, 추론 {avg_ms:.0f} ms (최대 약 {max_fps:.1f} fps), 사람 {'검출' if kp else '미검출'}"
        if max_fps < config.TARGET_FPS * 0.5:
            return WARN, detail + f" - 목표 {config.TARGET_FPS} fps에 크게 못 미침"
        return OK, detail

    def speaker(self):
        if self.args.no_sound:
            return WARN, "건너뜀 (--no-sound)"
        from l4_application.audio import play_blocking
        chime = config.AUDIO_DIR / "chime.wav"
        if not chime.exists():
            return FAIL, f"알림음 파일 없음: {chime} (python -m tools.make_sounds)"
        play_blocking(chime)
        return OK, "알림음 재생함 - 소리가 들렸는지 직접 확인하세요"

    def prompt_audio(self):
        prompt = config.AUDIO_DIR / "ask_status.wav"
        if prompt.exists():
            return OK, str(prompt)
        if shutil.which("espeak-ng"):
            return WARN, "녹음된 안내 음성 없음 - espeak-ng 합성 음성으로 대체"
        return WARN, "녹음된 안내 음성과 espeak-ng 모두 없음 - 알림음만 재생"

    def microphone(self):
        import sounddevice as sd
        seconds = 3
        print(f"  {seconds}초 동안 '괜찮아요'라고 말해 주세요...", flush=True)
        audio = sd.rec(int(seconds * config.VOICE_SAMPLE_RATE), samplerate=config.VOICE_SAMPLE_RATE,
                       channels=1, dtype="int16", device=config.MIC_DEVICE)
        sd.wait()
        self.recording = audio
        peak = int(abs(audio).max())
        rms = float((audio.astype("float64") ** 2).mean() ** 0.5)
        detail = f"최대 {peak}, 평균 {rms:.0f} (int16)"
        if peak == 0:
            return FAIL, detail + " - 입력 없음 (연결·음소거 확인)"
        if peak < 1000:
            return WARN, detail + " - 소리가 매우 작음 (마이크 위치·증폭 확인)"
        return OK, detail

    def speech_recognition(self):
        from vosk import KaldiRecognizer, Model, SetLogLevel
        import json
        SetLogLevel(-1)
        if not config.VOSK_MODEL_PATH.exists():
            return FAIL, f"Vosk 모델 없음: {config.VOSK_MODEL_PATH}"
        model = Model(str(config.VOSK_MODEL_PATH))
        if getattr(self, "recording", None) is None:
            return OK, "모델 로드됨 (녹음이 없어 인식은 건너뜀)"
        rec = KaldiRecognizer(model, config.VOICE_SAMPLE_RATE)
        rec.AcceptWaveform(self.recording.tobytes())
        text = json.loads(rec.FinalResult()).get("text", "")
        from l4_application.voice_checker import classify
        kind = classify(text, config.KEYWORDS_NEGATED_SAFE, config.KEYWORDS_HELP, config.KEYWORDS_SAFE)
        return (OK if kind == "SAFE" else WARN), f"인식 '{text}' -> {kind}"

    def telegram(self):
        if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
            return WARN, "토큰/수신자 미설정 - 보호자 알림이 나가지 않음"
        base = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
        me = requests.get(f"{base}/getMe", timeout=5)
        if me.status_code != 200:
            return FAIL, f"봇 토큰 확인 실패 (HTTP {me.status_code})"
        name = me.json()["result"].get("username")
        if not self.args.send_test:
            return OK, f"봇 @{name} 확인 (시험 메시지는 --send-test)"
        res = requests.post(f"{base}/sendMessage", timeout=5, json={
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": f"[시험] {config.DEVICE_NAME} 낙상 감지 장치 알림 시험입니다.",
        })
        if res.status_code != 200:
            return FAIL, f"시험 메시지 전송 실패 (HTTP {res.status_code}) - chat_id 확인"
        return OK, f"봇 @{name}, 시험 메시지 전송함"

    def services(self):
        try:
            res = requests.get(f"{config.API_BASE_URL}/api/health", timeout=2)
        except requests.RequestException:
            return WARN, "L4 서버가 실행 중이 아님 (서비스 시작 전이면 정상)"
        body = res.json()
        if res.status_code == 200:
            return OK, f"실행 중, 상태 {body['state']}"
        return WARN, "실행 중이나 문제 있음: " + ", ".join(body["problems"])


def main() -> int:
    parser = argparse.ArgumentParser(description="낙상 감지 장치 점검")
    parser.add_argument("--no-sound", action="store_true", help="스피커·마이크 점검 생략")
    parser.add_argument("--send-test", action="store_true", help="텔레그램 시험 메시지 전송")
    args = parser.parse_args()

    c = Checker(args)
    c.run("Python", c.python)
    c.run("저장 공간", c.disk)
    c.run("시간 동기화", c.time_sync)
    c.run("카메라", c.camera)
    c.run("자세 추정 모델", c.pose_model)
    c.run("스피커", c.speaker)
    c.run("안내 음성", c.prompt_audio)
    if not args.no_sound:
        c.run("마이크", c.microphone)
    c.run("음성 인식", c.speech_recognition)
    c.run("보호자 알림", c.telegram)
    c.run("서비스 상태", c.services)

    print("\n=== 장치 점검 결과 ===")
    width = max(len(n) for n, _, _ in c.results)
    for name, status, detail in c.results:
        print(f"[{status}] {name.ljust(width)}  {detail}")
    failed = [n for n, s, _ in c.results if s == FAIL]
    print(f"\n실패 {len(failed)}건" + (f": {', '.join(failed)}" if failed else " - 설치 조건 충족"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
