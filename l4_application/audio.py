"""스피커 출력: 안내 음성 재생과 현장 경보음.

Linux(Raspberry Pi)는 aplay, Windows(개발용)는 winsound를 쓴다.
"""

import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

log = logging.getLogger("l4.audio")


def play_blocking(path: Path, timeout: float = 30.0) -> None:
    if os.name == "nt":
        import winsound
        winsound.PlaySound(str(path), winsound.SND_FILENAME)
    else:
        subprocess.run(["aplay", "-q", str(path)], check=True, timeout=timeout)


def speak_blocking(text: str, timeout: float = 30.0) -> bool:
    """녹음된 안내 음성이 없을 때의 대체 수단. espeak-ng 한국어 합성(음질은 낮음)."""
    exe = shutil.which("espeak-ng")
    if exe is None:
        return False
    try:
        subprocess.run([exe, "-v", "ko", "-s", "140", text], check=True, timeout=timeout)
        return True
    except (subprocess.SubprocessError, OSError) as e:
        log.error("espeak-ng 실행 실패: %s", e)
        return False


class PromptPlayer:
    """안내 음성 재생. 녹음 파일 -> espeak-ng 합성 -> 알림음 순서로 대체한다.

    재생이 끝날 때까지 기다린다. 재생 중에 녹음을 시작하면 안내 음성이 함께 녹음되기 때문이다.
    """

    def __init__(self, prompt_wav: Path, chime_wav: Path, prompt_text: str):
        self.prompt_wav, self.chime_wav, self.prompt_text = prompt_wav, chime_wav, prompt_text
        if not prompt_wav.exists():
            log.warning("녹음된 안내 음성이 없습니다: %s (합성 음성 또는 알림음으로 대체)", prompt_wav)

    def play(self) -> bool:
        try:
            if self.prompt_wav.exists():
                play_blocking(self.prompt_wav)
                return True
            if speak_blocking(self.prompt_text):
                return True
            if self.chime_wav.exists():
                play_blocking(self.chime_wav)
                return True
        except Exception as e:
            log.error("안내 음성 재생 실패: %s", e)
        return False


class Siren:
    """경보 상태 동안 경보음을 반복 재생한다."""

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


class WavSiren(Siren):
    def __init__(self, wav: Path):
        self.wav = wav
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if not self.wav.exists():
                log.warning("경보음 파일이 없습니다: %s", self.wav)
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="siren", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        if os.name == "nt":
            import winsound
            winsound.PlaySound(str(self.wav), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)
            self._stop.wait()
            winsound.PlaySound(None, winsound.SND_PURGE)
            return
        while not self._stop.is_set():
            try:
                with self._lock:
                    self._proc = subprocess.Popen(["aplay", "-q", str(self.wav)])
                self._proc.wait()
            except OSError as e:
                log.error("경보음 재생 실패: %s", e)
                self._stop.wait(5)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.terminate()
            self._proc = None
