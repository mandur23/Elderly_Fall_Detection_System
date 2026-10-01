"""알림음(chime.wav)과 경보음(siren.wav)을 생성한다. 외부 음원 라이선스 문제가 없도록 직접 합성한다.

    python -m tools.make_sounds

안내 음성(ask_status.wav)은 사람 목소리로 녹음해 assets/audio/에 넣는다(16kHz 이상 mono wav 권장).
"""

import wave
from pathlib import Path

import numpy as np

import config

RATE = 16000


def write_wav(path: Path, samples: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (np.clip(samples, -1, 1) * 32767 * 0.8).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(data.tobytes())


def tone(freq: float, seconds: float) -> np.ndarray:
    t = np.arange(int(RATE * seconds)) / RATE
    env = np.minimum(1, np.minimum(t, seconds - t) / 0.02)  # 클릭음 방지용 페이드
    return np.sin(2 * np.pi * freq * t) * env


def chime() -> np.ndarray:
    gap = np.zeros(int(RATE * 0.05))
    return np.concatenate([tone(880, 0.25), gap, tone(1175, 0.4)])


def siren(seconds: float = 2.0) -> np.ndarray:
    t = np.arange(int(RATE * seconds)) / RATE
    freq = 900 + 400 * np.sin(2 * np.pi * 0.5 * t)  # 2초 주기로 오르내리는 음
    phase = 2 * np.pi * np.cumsum(freq) / RATE
    return np.sin(phase)


def main() -> None:
    write_wav(config.AUDIO_DIR / "chime.wav", chime())
    write_wav(config.AUDIO_DIR / "siren.wav", siren())
    print(f"생성 완료: {config.AUDIO_DIR}")


if __name__ == "__main__":
    main()
