"""Estimate a beat pulse from the original audio with the local NumPy runtime."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

import numpy as np

SR = 11025
FRAME = 1024
HOP = 256
FPS = SR / HOP
LAGS = np.arange(12, 44)


def _window_plan(start: float, duration: float) -> list[tuple[float, float]]:
    if duration < 8:
        raise ValueError("片段不足 8 秒，无法可靠识别 BPM；请手动填写")
    length = min(45.0, duration)
    if duration <= 60:
        return [(start, length)]
    offsets = (0.08, 0.40, 0.70)
    return [(start + min(duration - length, duration * at), length) for at in offsets]


def _beat_profile(path: Path, ffmpeg: Path, start: float, duration: float) -> np.ndarray:
    command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path),
               "-ss", str(start), "-t", str(duration), "-ac", "1", "-ar", str(SR),
               "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1"]
    decoded = subprocess.run(command, capture_output=True, timeout=90, check=False)
    if decoded.returncode or not decoded.stdout:
        raise ValueError("无法解码音频以识别 BPM")
    signal = np.frombuffer(decoded.stdout, dtype="<f4")
    if signal.size < SR * 8 or not np.all(np.isfinite(signal)):
        raise ValueError("音频太短或损坏，无法识别 BPM")
    frames = np.lib.stride_tricks.sliding_window_view(signal, FRAME)[::HOP]
    spectrum = np.abs(np.fft.rfft(frames * np.hanning(FRAME), axis=1))
    flux = np.maximum(0, np.diff(spectrum, axis=0)).sum(axis=1)
    if float(np.std(flux)) < 1e-5:
        raise ValueError("片段缺少清晰节拍，请手动填写 BPM")
    flux = (flux - np.mean(flux)) / np.std(flux)
    return np.asarray([np.dot(flux[:-lag], flux[lag:]) / (len(flux) - lag) for lag in LAGS])


def _refined_bpm(profile: np.ndarray, index: int) -> float:
    lag = float(LAGS[index])
    if 0 < index < len(profile) - 1:
        left, middle, right = (float(profile[index + shift]) for shift in (-1, 0, 1))
        denominator = left - 2 * middle + right
        if denominator < -1e-9:
            lag += float(np.clip(0.5 * (left - right) / denominator, -0.5, 0.5))
    return round(60 * FPS / lag, 1)


def estimate(path: Path, ffmpeg: Path, start: float, duration: float) -> dict:
    windows = _window_plan(start, duration)
    profiles = [_beat_profile(path, ffmpeg, at, length) for at, length in windows]
    average = np.mean(profiles, axis=0)
    candidates = [i for i in range(len(LAGS)) if
                  (i == 0 or average[i] >= average[i - 1]) and
                  (i == len(LAGS) - 1 or average[i] >= average[i + 1])]
    if not candidates:
        raise ValueError("未找到稳定节拍，请手动填写 BPM")
    # The half-time pulse is preferred when 2:1 peaks are equally credible.
    def ranked(index: int) -> float:
        bpm = 60 * FPS / LAGS[index]
        return float(average[index]) * (1.08 if 80 <= bpm <= 160 else 1.0)
    best = max(candidates, key=ranked)
    strength = float(average[best])
    support = sum(float(profile[best]) >= max(0.10, float(np.max(profile)) * 0.55)
                  for profile in profiles)
    if strength < 0.12 or support < (2 if len(profiles) > 1 else 1):
        raise ValueError("节拍不够稳定，无法可靠识别 BPM；请手动填写")
    bpm = _refined_bpm(average, best)
    alternatives = []
    for candidate in candidates:
        if candidate == best or float(average[candidate]) < strength * 0.75:
            continue
        other = _refined_bpm(average, candidate)
        ratio = max(other, bpm) / min(other, bpm)
        if 1.90 <= ratio <= 2.10:
            alternatives.append(other)
    return {"schema_version": "1.0", "method": "spectral-flux-autocorrelation-v1",
            "input_sha256": sha256(path.read_bytes()).hexdigest(), "bpm": bpm,
            "alternatives_bpm": sorted(set(alternatives)),
            "confidence": "moderate" if strength >= 0.25 else "low",
            "correlation": round(strength, 3),
            "windows": [{"start_sec": round(at, 3), "duration_sec": round(length, 3)}
                        for at, length in windows]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--start", type=float, required=True)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(estimate(args.input, args.ffmpeg, args.start, args.duration),
                                      ensure_ascii=False, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
