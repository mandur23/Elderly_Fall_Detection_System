"""edge 스피커로 보낼 소리 만들기: 안내 음성, 알림음, 경보음.

안내 음성은 녹음 파일 -> 운영체제 합성 음성(Windows System.Speech, Linux espeak-ng) -> 알림음 순서로 정한다.
모두 규약 형식(16kHz mono s16le PCM)으로 바꿔 메모리에 둔다. 합성 결과는 파일로 남기지 않는다.
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import struct
import subprocess
import sys
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import numpy as np

from shared import protocol as P

log = logging.getLogger("server.speech")

RATE = P.AUDIO_RATE
BYTES_PER_SECOND = RATE * P.AUDIO_SAMPLE_BYTES * P.AUDIO_CHANNELS


def duration(pcm: bytes) -> float:
    return len(pcm) / BYTES_PER_SECOND


@dataclass
class Sound:
    pcm: bytes
    source: str  # file, tts-windows, tts-espeak, chime, siren

    @property
    def seconds(self) -> float:
        return duration(self.pcm)


# --- WAV 읽기와 형식 변환 ---
def parse_wav(data: bytes) -> Tuple[int, int, int, bytes]:
    """(표본 주파수, 채널 수, 표본 바이트 수, PCM). espeak-ng --stdout처럼 크기 칸이 틀린 파일도 읽는다."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("WAV 파일이 아닙니다")
    pos, fmt = 12, None
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        body = pos + 8
        if cid == b"fmt ":
            audio_format, channels, rate = struct.unpack_from("<HHI", data, body)
            bits = struct.unpack_from("<H", data, body + 14)[0]
            if audio_format not in (1, 0xFFFE):
                raise ValueError(f"PCM이 아닌 WAV는 지원하지 않습니다 (형식 {audio_format})")
            fmt = (rate, channels, bits // 8)
        elif cid == b"data":
            if fmt is None:
                raise ValueError("WAV fmt 정보가 없습니다")
            end = len(data) if size in (0, 0xFFFFFFFF) or body + size > len(data) else body + size
            return (*fmt, data[body:end])
        pos = body + size + (size & 1)
    raise ValueError("WAV data 부분이 없습니다")


def to_protocol_pcm(pcm: bytes, rate: int, channels: int, sample_bytes: int) -> bytes:
    """임의 PCM -> 16kHz mono s16le."""
    if sample_bytes == 1:
        x = (np.frombuffer(pcm, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sample_bytes == 2:
        x = np.frombuffer(pcm[:len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
    elif sample_bytes == 3:
        b = np.frombuffer(pcm[:len(pcm) // 3 * 3], dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        x = (np.where(v >= 1 << 23, v - (1 << 24), v) / float(1 << 23)).astype(np.float32)
    elif sample_bytes == 4:
        x = np.frombuffer(pcm[:len(pcm) // 4 * 4], dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"지원하지 않는 표본 크기: {sample_bytes}바이트")
    if channels > 1:
        x = x[:len(x) // channels * channels].reshape(-1, channels).mean(axis=1)
    if rate != RATE and len(x) > 1:
        n = max(1, int(round(len(x) * RATE / rate)))
        x = np.interp(np.arange(n) * (rate / RATE), np.arange(len(x)), x).astype(np.float32)
    return (np.clip(x, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def wav_to_pcm(data: bytes) -> bytes:
    rate, channels, width, pcm = parse_wav(data)
    return to_protocol_pcm(pcm, rate, channels, width)


# --- 직접 만드는 소리 (외부 음원 라이선스 문제가 없도록) ---
def _tone(freq: float, seconds: float, volume: float = 0.8) -> array:
    n = int(RATE * seconds)
    fade = 0.02 * RATE  # 클릭음 방지
    out = array("h")
    for i in range(n):
        env = min(1.0, i / fade, (n - i) / fade)
        out.append(int(32767 * volume * env * math.sin(2 * math.pi * freq * i / RATE)))
    return out


def chime_pcm() -> bytes:
    gap = array("h", [0] * int(RATE * 0.05))
    return (_tone(880, 0.25) + gap + _tone(1175, 0.4)).tobytes()


def siren_pcm(seconds: float = 2.0, volume: float = 0.8) -> bytes:
    """2초 주기로 오르내리는 경보음. edge가 loop=true로 반복 재생한다."""
    t = np.arange(int(RATE * seconds)) / RATE
    freq = 900 + 400 * np.sin(2 * np.pi * 0.5 * t)
    phase = 2 * np.pi * np.cumsum(freq) / RATE
    return (np.sin(phase) * 32767 * volume).astype("<i2").tobytes()


# --- 운영체제 합성 음성 ---
# 문장과 음성 이름은 환경변수로 넘긴다(명령줄 따옴표·인코딩 문제를 피하려고).
# 출력은 16kHz 16bit mono PCM을 표준 출력으로 바로 내보낸다(파일을 만들지 않음).
_WINDOWS_TTS = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
if ($env:FALL_TTS_VOICE) {
  $s.SelectVoice($env:FALL_TTS_VOICE)
} else {
  $v = $s.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -eq 'ko-KR' } | Select-Object -First 1
  if (-not $v) { exit 3 }
  $s.SelectVoice($v.VoiceInfo.Name)
}
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$ms = New-Object System.IO.MemoryStream
$s.SetOutputToAudioStream($ms, $fmt)
$s.Rate = -1
$s.Speak($env:FALL_TTS_TEXT)
$s.Dispose()
$out = [Console]::OpenStandardOutput()
$out.Write($ms.ToArray(), 0, [int]$ms.Length)
$out.Flush()
"""


def synthesize_windows(text: str, voice: str = "", timeout: float = 60.0) -> Optional[bytes]:
    exe = shutil.which("powershell.exe") or shutil.which("powershell")
    if exe is None:
        return None
    env = {**os.environ, "FALL_TTS_TEXT": text, "FALL_TTS_VOICE": voice}
    try:
        proc = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_TTS],
                              capture_output=True, env=env, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("Windows 합성 음성 실행 실패: %s", e)
        return None
    if proc.returncode == 3:
        log.warning("Windows 한국어 합성 음성이 설치되어 있지 않습니다")
        return None
    if proc.returncode != 0 or len(proc.stdout) < BYTES_PER_SECOND // 2:
        log.warning("Windows 합성 음성 실패 (코드 %s): %s", proc.returncode,
                    proc.stderr.decode("utf-8", "replace").strip()[:300])
        return None
    return proc.stdout[:len(proc.stdout) // 2 * 2]


def synthesize_espeak(text: str, timeout: float = 60.0) -> Optional[bytes]:
    exe = shutil.which("espeak-ng")
    if exe is None:
        return None
    try:
        proc = subprocess.run([exe, "-v", "ko", "-s", "140", "--stdout", text], capture_output=True,
                              timeout=timeout)
        if proc.returncode != 0:
            log.warning("espeak-ng 실패 (코드 %s)", proc.returncode)
            return None
        return wav_to_pcm(proc.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        log.warning("espeak-ng 실행 실패: %s", e)
        return None


def prepare_prompt(text: str, wav_path: Optional[Path] = None, voice: str = "") -> Sound:
    """안내 음성을 준비한다. 녹음 파일 -> 합성 음성 -> 알림음 순서."""
    if wav_path is not None:
        if wav_path.exists():
            try:
                return Sound(wav_to_pcm(wav_path.read_bytes()), "file")
            except (OSError, ValueError) as e:
                log.error("안내 음성 파일을 읽지 못했습니다 (%s): %s", wav_path, e)
        else:
            log.warning("안내 음성 파일이 없습니다: %s", wav_path)
    pcm = synthesize_windows(text, voice) if sys.platform == "win32" else synthesize_espeak(text)
    if pcm:
        return Sound(pcm, "tts-windows" if sys.platform == "win32" else "tts-espeak")
    log.warning("합성 음성을 만들지 못해 알림음으로 안내합니다 (화면 문구로 안내)")
    return Sound(chime_pcm(), "chime")
