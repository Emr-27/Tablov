"""Run inside the existing audio interpreter; extract vocal or accompaniment pitch evidence.

The application remains dependency-free. NumPy comes from the existing DDSP
interpreter, while FFmpeg decodes MP3/WAV in a child process.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

import numpy as np

SR = 16000
HOP = 256
FRAME = 2048
INSTRUMENTAL_FRAME = 4096
INSTRUMENTAL_MIDIS = np.arange(60, 89)
MIN_LAG = 7
MAX_LAG = 160


def _pitch(frame: np.ndarray) -> tuple[float | None, float, float | None]:
    centered = frame - np.mean(frame)
    if np.mean(centered * centered) < 2.5e-6:
        return None, 0.0, None
    n = len(centered)
    spectrum = np.fft.rfft(centered, n=2*n)
    correlation = np.fft.irfft(spectrum * np.conj(spectrum), n=2*n)[:MAX_LAG+1]
    cumulative = np.concatenate(([0.0], np.cumsum(centered * centered)))
    lag = np.arange(1, MAX_LAG+1)
    difference = cumulative[n-lag] + (cumulative[n]-cumulative[lag]) - 2*correlation[lag]
    difference = np.maximum(difference, 1e-10)
    cmnd = difference * lag / np.cumsum(difference)
    region = cmnd[MIN_LAG-1:]
    candidates = np.where((region[1:-1] <= region[:-2]) & (region[1:-1] <= region[2:]) &
                          (region[1:-1] < 0.22))[0] + MIN_LAG + 1
    best = int(candidates[0]) if candidates.size else int(np.argmin(region) + MIN_LAG)
    strength = float(max(0.0, min(1.0, 1.0 - cmnd[best-1])))
    if 1 < best < MAX_LAG:
        left, middle, right = cmnd[best-2:best+1]
        denominator = left - 2*middle + right
        adjustment = 0.5 * (left-right) / denominator if abs(denominator) > 1e-12 else 0.0
        best += float(np.clip(adjustment, -0.5, 0.5))
    candidate = SR / best
    return (candidate if strength >= 0.60 else None), strength, (candidate if strength >= 0.35 else None)


def _instrumental_scores(spectrum: np.ndarray, frame_size: int) -> np.ndarray:
    """Harmonic salience for a possible lead line above the bass register."""
    peak = float(np.max(spectrum))
    if peak < 1e-6:
        return np.zeros(INSTRUMENTAL_MIDIS.size)
    normalized = spectrum / peak
    fundamentals = 440.0 * 2 ** ((INSTRUMENTAL_MIDIS - 69) / 12)
    scores = np.zeros(INSTRUMENTAL_MIDIS.size)
    for harmonic in range(1, 5):
        bins = np.rint(fundamentals * harmonic * frame_size / SR).astype(int)
        values = np.maximum.reduce((normalized[bins - 1], normalized[bins], normalized[bins + 1]))
        scores += values / harmonic
    return scores


def _instrumental_pitch(scores: np.ndarray) -> tuple[float | None, float, float | None]:
    """Accept a lead note only when its harmonic support beats the alternatives."""
    ranking = np.argsort(scores)
    best, runner_up = float(scores[ranking[-1]]), float(scores[ranking[-2]])
    ratio = best / max(1e-9, runner_up)
    candidate = float(440.0 * 2 ** ((INSTRUMENTAL_MIDIS[ranking[-1]] - 69) / 12))
    support = min(1.0, best / 0.9) * min(1.0, max(0.0, (ratio - 1.0) / 0.25))
    accepted = candidate if best >= 0.45 and ratio >= 1.20 else None
    return accepted, (min(1.0, 0.60 + (ratio - 1.20)) if accepted else 0.0), (candidate if best >= 0.35 and ratio >= 1.07 and support >= 0.24 else None)


def _rmvpe_track(signal: np.ndarray, code: Path, weight: Path) -> np.ndarray:
    """Use the user's installed vocal model on the decoded, 16 kHz segment."""
    sys.path.insert(0, str(code.resolve().parents[2]))
    from encoder.rmvpe.inference import RMVPE

    model = RMVPE(str(weight), hop_length=160)
    f0 = np.asarray(model.infer_from_audio(signal.astype(np.float32), sample_rate=SR), dtype=np.float64)
    if f0.ndim != 1 or f0.size < 2 or not np.all(np.isfinite(f0)):
        raise ValueError("RMVPE 返回的音高轨无效")
    return f0


def analyze(input_path: Path, ffmpeg: Path, start_sec: float, duration_sec: float,
            mode: str = "vocal", pitch_backend: str = "fft-yin",
            rmvpe_code: Path | None = None, rmvpe_weight: Path | None = None) -> dict:
    if pitch_backend == "rmvpe" and (mode != "vocal" or rmvpe_code is None or rmvpe_weight is None):
        raise ValueError("RMVPE 仅用于已分离的人声音轨，且需要本机模型路径")
    command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin",
               "-i", str(input_path), "-ss", str(start_sec), "-t", str(duration_sec)]
    if mode == "accompaniment":
        # Reduce bass and cymbal energy before estimating one possible lead line.
        command += ["-af", "highpass=f=260,lowpass=f=1800"]
    command += [
               "-ac", "1", "-ar", str(SR), "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1"]
    decoded = subprocess.run(command, capture_output=True, timeout=90, check=False)
    if decoded.returncode or not decoded.stdout:
        raise ValueError("音频无法解码：" + decoded.stderr.decode("utf-8", errors="replace")[-500:])
    signal = np.frombuffer(decoded.stdout, dtype="<f4").astype(np.float64)
    if signal.size < SR // 2:
        raise ValueError("片段少于 0.5 秒")
    if not np.all(np.isfinite(signal)):
        raise ValueError("解码结果含非有限值")
    rmvpe_f0 = _rmvpe_track(signal, rmvpe_code, rmvpe_weight) if pitch_backend == "rmvpe" else None
    frame_size = INSTRUMENTAL_FRAME if mode == "accompaniment" else FRAME
    padded = np.pad(signal, (frame_size//2, frame_size//2))
    count = int(np.ceil(signal.size/HOP))
    records = []
    onsets = []
    last_rms = 0.0
    last_spectrum = None
    onset_scores = []
    instrumental_scores = []
    for i in range(count):
        frame = padded[i*HOP:i*HOP+frame_size]
        if len(frame) < frame_size:
            frame = np.pad(frame, (0, frame_size-len(frame)))
        rms = float(np.sqrt(np.mean(frame*frame)))
        spectrum = np.abs(np.fft.rfft(frame * np.hanning(frame_size)))
        if rmvpe_f0 is not None:
            index = min(len(rmvpe_f0) - 1, round(i * HOP / 160))
            local = rmvpe_f0[max(0, index - 2):min(len(rmvpe_f0), index + 3)]
            voiced_local = local[local > 0]
            hz = float(np.median(voiced_local)) if voiced_local.size >= min(3, local.size) else None
            strength = 1.0 if hz is not None else 0.0  # RMVPE returns voiced/unvoiced, not a calibrated probability.
            candidate_hz = hz
        elif mode == "accompaniment":
            instrumental_scores.append(_instrumental_scores(spectrum, frame_size))
            hz, strength, candidate_hz = None, 0.0, None
        else:
            hz, strength, candidate_hz = _pitch(frame)
        flux = float(np.sum(np.maximum(0, spectrum-last_spectrum))) if last_spectrum is not None else 0.0
        onset_scores.append(max(0.0, rms-last_rms) + flux/5000)
        last_rms, last_spectrum = rms, spectrum
        records.append({"sec": round(start_sec + i*HOP/SR, 6),
                        "hz": round(hz, 3) if hz is not None else None,
                        "candidate_hz": round(candidate_hz, 3) if candidate_hz is not None else None,
                        "voiced": hz is not None, "periodicity": round(strength, 4),
                        "rms": round(rms, 6)})
    if instrumental_scores:
        # An 80 ms window reduces frame-to-frame chord-tone switching without
        # erasing the shortest notes that survive the score's 160 ms minimum.
        matrix = np.pad(np.asarray(instrumental_scores), ((2, 2), (0, 0)), mode="edge")
        totals = np.vstack((np.zeros((1, matrix.shape[1])), np.cumsum(matrix, axis=0)))
        averaged = (totals[5:] - totals[:-5]) / 5
        for record, scores in zip(records, averaged):
            hz, strength, candidate_hz = _instrumental_pitch(scores)
            record.update(hz=round(hz, 3) if hz is not None else None,
                          candidate_hz=round(candidate_hz, 3) if candidate_hz is not None else None,
                          voiced=hz is not None, periodicity=round(strength, 4))
    scores = np.asarray(onset_scores)
    threshold = max(float(np.percentile(scores, 80))*2.0, float(np.max(scores))*0.12)
    for i in range(2, count-2):
        if scores[i] >= threshold and scores[i] > scores[i-1] and scores[i] >= scores[i+1]:
            if not onsets or i-onsets[-1] >= 5:
                onsets.append(i)
    return {
        "schema_version": "1.0", "backend": "rmvpe-local" if rmvpe_f0 is not None else
            "fft-yin-highpass-candidate" if mode == "accompaniment" else "fft-yin",
        "backend_version": "1.2" if mode == "accompaniment" else "1.1",
        "pitch_confidence_kind": "binary_voicing" if rmvpe_f0 is not None else
            "harmonic_margin" if mode == "accompaniment" else "yin_periodicity",
        "input_sha256": sha256(input_path.read_bytes()).hexdigest(),
        "sample_rate": SR, "hop_samples": HOP, "frame_length": frame_size,
        "frame_center": "center", "source_start_sec": start_sec,
        "decoded_duration_sec": round(signal.size/SR, 6),
        "channel_mix": "ffmpeg mono downmix", "onset_frames": onsets,
        "frames": records,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--start", type=float, required=True)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("vocal", "accompaniment"), default="vocal")
    parser.add_argument("--pitch-backend", choices=("fft-yin", "rmvpe"), default="fft-yin")
    parser.add_argument("--rmvpe-code", type=Path)
    parser.add_argument("--rmvpe-weight", type=Path)
    args = parser.parse_args()
    args.output.write_text(json.dumps(analyze(args.input, args.ffmpeg, args.start, args.duration,
                                              args.mode, args.pitch_backend, args.rmvpe_code, args.rmvpe_weight),
                                      allow_nan=False, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
