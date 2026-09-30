"""Audio intake: an isolated FFT-YIN worker and conservative note candidates."""
from fractions import Fraction
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import subprocess

from .errors import ContractError
from .models import validate_score
from .rationals import integer, number, str_q
from .theory import key_from_text

ROOT = Path(__file__).resolve().parents[2]
WORKER = Path(__file__).with_name("audio_worker.py")
CONFIG = ROOT / "configs" / "local_backends.local.json"
GRID = Fraction(1, 4)
CHUNK_SECONDS = 55.0
MAX_FULL_SECONDS = 3600.0


def resources() -> tuple[Path, Path]:
    try:
        config = json.loads(CONFIG.read_text(encoding="utf-8"))["resources"]
        python = Path(config["ddsp_python"])
        ffmpeg = Path(config["ffmpeg"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ContractError("音频环境", "本机音频工具配置不可用，请检查 configs/local_backends.local.json") from exc
    if not python.is_file() or not ffmpeg.is_file():
        raise ContractError("音频环境", "本机 DDSP Python 或 FFmpeg 不存在，请检查配置路径")
    return python, ffmpeg


def audio_duration(path: Path) -> float:
    """Probe the real media length before planning bounded worker jobs."""
    _, ffmpeg = resources()
    ffprobe = ffmpeg.with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    if not ffprobe.is_file():
        raise ContractError("音频环境", "FFprobe 不存在，无法确定整首歌曲时长")
    command = [str(ffprobe), "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    try:
        done = subprocess.run(command, capture_output=True, timeout=30, check=False,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        duration = float(done.stdout.decode("ascii").strip())
    except (subprocess.TimeoutExpired, ValueError, UnicodeError) as exc:
        raise ContractError("音频片段", "无法读取整首歌曲时长") from exc
    if done.returncode or not math.isfinite(duration) or duration < 0.5:
        raise ContractError("音频片段", "无法读取有效的整首歌曲时长")
    if duration > MAX_FULL_SECONDS:
        raise ContractError("音频片段", "整首处理目前支持最长 1 小时的音频")
    return duration


def chunk_plan(duration: float) -> list[tuple[float, float]]:
    """Cover the timeline exactly once; avoid an unprocessable sub-0.5s tail."""
    parts = []
    start = 0.0
    while start < duration - 1e-6:
        end = min(duration, start + CHUNK_SECONDS)
        if duration - end < 0.5:
            end = duration
        parts.append((start, end - start))
        start = end
    return parts


def extract_evidence(path: Path, job: Path, start_sec: float, duration_sec: float,
                     *, mode: str = "vocal") -> dict:
    path = path.resolve()
    job = job.resolve()
    job.mkdir(parents=True, exist_ok=True)
    python, ffmpeg = resources()
    evidence_path = job / ("accompaniment_evidence.json" if mode == "accompaniment" else "pitch_evidence.json")
    command = [str(python), "-B", str(WORKER), "--input", str(path), "--ffmpeg", str(ffmpeg),
               "--start", str(start_sec), "--duration", str(duration_sec), "--output", str(evidence_path),
               "--mode", mode]
    try:
        done = subprocess.run(command, cwd=job, capture_output=True, timeout=240,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
    except subprocess.TimeoutExpired as exc:
        raise ContractError("音频识别", "分析超时；请选择更短的片段") from exc
    if done.returncode or not evidence_path.is_file():
        detail = done.stderr.decode("utf-8", errors="replace")[-500:].strip()
        raise ContractError("音频识别", detail or "本机 FFT-YIN 后端未成功返回")
    try:
        data = json.loads(evidence_path.read_text(encoding="utf-8"))
        if data["input_sha256"] != sha256(path.read_bytes()).hexdigest():
            raise ValueError("输入哈希不匹配")
        if data["sample_rate"] != 16000 or data["hop_samples"] != 256 or not data["frames"]:
            raise ValueError("音高证据的时间结构无效")
        if abs(data["frames"][0]["sec"] - start_sec) > 1e-6:
            raise ValueError("首帧未对齐所选音频时间")
        return data
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ContractError("音频识别", "本机后端返回的音高证据无效：" + str(exc)) from exc


def _quantized_q(seconds: float, bpm: float) -> Fraction:
    # A fixed quarter-beat grid avoids inventing triplets or tempo changes.
    step = round(seconds * bpm / 60 * 4)
    return Fraction(max(0, step), 4)


def _fill_pitch_gaps(frames: list[dict], labels: list[int | str], *,
                     min_note_frames: int, onsets: set[int]) -> list[bool]:
    """Promote sustained alternate candidates and very short note transitions."""
    inferred = [False] * len(labels)
    candidates: list[int | None] = []
    for frame in frames:
        hz = frame.get("candidate_hz")
        if hz is None or not 80 <= float(hz) <= 2000:
            candidates.append(None)
        else:
            candidates.append(int(round(69 + 12 * math.log2(float(hz) / 440))))
    half_window = 4 if min_note_frames >= 10 else 3
    required = 5 if min_note_frames >= 10 else 4
    for i, label in enumerate(labels):
        if label != "unknown":
            continue
        window = candidates[max(0, i-half_window):min(len(labels), i+half_window+1)]
        counts: dict[int, int] = {}
        for pitch in window:
            if pitch is not None:
                counts[pitch] = counts.get(pitch, 0) + 1
        if counts:
            pitch, count = max(counts.items(), key=lambda item: item[1])
            if count >= required and (candidates[i] in (None, pitch)):
                labels[i] = pitch
                inferred[i] = True
    # Bridge only sub-0.2s gaps between audible notes. Long ambiguous passages
    # remain unknown, and low-energy rests never turn into notes.
    i = 0
    while i < len(labels):
        if labels[i] != "unknown":
            i += 1
            continue
        end = i + 1
        while end < len(labels) and labels[end] == "unknown":
            end += 1
        if i and end < len(labels) and end - i <= 12 and isinstance(labels[i-1], int) and isinstance(labels[end], int):
            onset = next((at for at in sorted(onsets) if i <= at < end), None)
            split = onset if onset is not None else (i + end) // 2
            for j in range(i, end):
                labels[j] = labels[i-1] if j < split else labels[end]
                inferred[j] = True
        i = end
    return inferred


def notes_to_score(evidence: dict, *, source_name: str, bpm: float, key_text: str,
                   do_midi: int | None = None, key_source: str = "user",
                   allow_no_notes: bool = False, min_note_frames: int = 5) -> tuple[dict, dict]:
    frames = evidence["frames"]
    start_sec = float(evidence["source_start_sec"])
    duration = float(evidence["decoded_duration_sec"])
    frame_time = lambda i: min(duration, max(0.0, float(frames[i]["sec"]) - start_sec)) if i < len(frames) else duration
    if duration <= 0 or not frames:
        raise ContractError("音频识别", "未获得有效音频帧")
    peak = max(float(f["rms"]) for f in frames)
    floor = max(0.0015, peak * 0.09)
    labels = []
    for f in frames:
        hz = f.get("hz")
        if float(f["rms"]) < floor:
            labels.append("rest")
        elif hz is not None and f.get("voiced") and f.get("periodicity", 0) >= 0.60:
            midi = int(round(69 + 12 * math.log2(float(hz) / 440)))
            labels.append(midi if 0 <= midi <= 127 else "unknown")
        else:
            labels.append("unknown")
    onset = set(int(i) for i in evidence.get("onset_frames", []))
    inferred = _fill_pitch_gaps(frames, labels, min_note_frames=min_note_frames, onsets=onset)
    # A one-frame pitch blip is evidence to inspect, not a definite note.
    for i in range(2, len(labels) - 2):
        around = labels[i-2:i] + labels[i+1:i+3]
        if isinstance(labels[i], int) and len(set(around)) == 1 and around[0] != labels[i]:
            labels[i] = around[0]
    segments = []
    left = 0
    for i in range(1, len(labels)):
        split_onset = i in onset and isinstance(labels[i], int) and labels[i] == labels[i-1] and i - left >= 6
        if labels[i] != labels[left] or split_onset:
            segments.append((left, i, labels[left]))
            left = i
    segments.append((left, len(labels), labels[left]))
    # Short pitch islands remain unknown; don't manufacture very short notes.
    segments = [(a, b, "unknown" if isinstance(label, int) and b-a < min_note_frames else label)
                for a, b, label in segments]
    boundaries = [Fraction(0)]
    for _, right, _ in segments[:-1]:
        boundaries.append(_quantized_q(frame_time(right), bpm))
    end_q = max(GRID, _quantized_q(duration, bpm))
    boundaries.append(end_q)
    events = []
    discarded = 0
    phrase = 1
    for (left, right, label), qa, qb in zip(segments, boundaries, boundaries[1:]):
        if qb <= qa:
            discarded += 1
            continue
        kind = "note" if isinstance(label, int) else label
        event = {"id": f"a{len(events)+1}", "kind": kind, "start_q": str_q(qa),
                 "duration_q": str_q(qb - qa), "observed": {
                     "start_sec": round(start_sec + frame_time(left), 6),
                     "end_sec": round(start_sec + frame_time(right), 6)}}
        if kind == "note":
            event.update(pitch_midi=label, phrase_id=f"p{phrase}")
            if any(inferred[left:right]):
                event["review_required"] = True
                event["inference_method"] = "candidate_or_short_gap"
        else:
            event["reason"] = "low_energy" if kind == "rest" else "no_confident_f0"
            if kind == "rest" and qb - qa >= Fraction(1, 2):
                phrase += 1
        events.append(event)
    if not events:
        events = [{"id": "a1", "kind": "unknown", "start_q": "0", "duration_q": str_q(end_q),
                   "reason": "no_confident_f0", "observed": {
                       "start_sec": start_sec, "end_sec": start_sec + duration}}]
    # Rounding can skip intermediate segments; fill any gaps explicitly.
    filled = []
    cursor = Fraction(0)
    for event in events:
        at = Fraction(event["start_q"])
        if at > cursor:
            filled.append({"id": f"u{len(filled)+1}", "kind": "unknown", "start_q": str_q(cursor),
                           "duration_q": str_q(at-cursor), "reason": "quantization_gap"})
        filled.append(event)
        cursor = at + Fraction(event["duration_q"])
    if cursor < end_q:
        filled.append({"id": f"u{len(filled)+1}", "kind": "unknown", "start_q": str_q(cursor),
                       "duration_q": str_q(end_q-cursor), "reason": "quantization_gap"})
    if not allow_no_notes and not any(e["kind"] == "note" for e in filled):
        raise ContractError("音频识别", "未找到足够稳定的单音旋律；请选清晰单旋律或已分离的人声")
    key = key_from_text(key_text, do_midi=do_midi, source=key_source)
    score = {
        "schema_version": "2.0", "revision_id": "audio-" + evidence["input_sha256"][:12] + "-r2",
        "source": {"kind": "audio", "name": source_name, "sha256": evidence["input_sha256"],
                   "analysis_start_sec": start_sec, "analysis_duration_sec": duration},
        "score_start_q": "0", "score_end_q": str_q(end_q),
        "time_map": {"kind": "tempo", "origin_q": "0", "origin_sec": start_sec,
                     "tempo_events": [{"at_q": "0", "bpm_q": bpm}]},
        "meter_map": [{"at_q": "0", "numerator": 4, "denominator": 4, "source": "assumed"}],
        "first_full_bar_q": "0", "meter_alignment_status": "unconfirmed",
        "key_map": [key], "events": filled, "changes": []
    }
    return validate_score(score), {"discarded_subgrid_segments": discarded, "frame_count": len(frames),
                                   "note_count": sum(e["kind"] == "note" for e in filled),
                                   "unknown_count": sum(e["kind"] == "unknown" for e in filled),
                                   "inferred_note_count": sum(e["kind"] == "note" and e.get("review_required", False) for e in filled)}


def transcribe(path: Path, job: Path, *, source_name: str, start_sec: object,
               duration_sec: object, bpm: object, key_text: str,
               do_midi: object = None, mode: str = "direct",
               full_song: bool = False, progress=None) -> tuple[dict, dict]:
    start = number(start_sec, "片段起点")
    duration = number(duration_sec, "片段长度", positive=True)
    tempo = number(bpm, "四分音符 BPM", positive=True)
    if type(full_song) is not bool:
        raise ContractError("整首处理", "选项无效")
    if full_song:
        start = 0.0
        duration = audio_duration(path)
    elif not 0 <= start <= 3600 or not 0.5 <= duration <= 60:
        raise ContractError("音频片段", "起点应在 0–3600 秒，长度应在 0.5–60 秒")
    if not 30 <= tempo <= 300:
        raise ContractError("四分音符 BPM", "应在 30–300 之间")
    if mode not in ("direct", "vocal"):
        raise ContractError("识别来源", "请选择清晰单旋律或提取主唱")
    do = None if do_midi in (None, "") else integer(do_midi, "无点 1", 0, 127)
    if not isinstance(key_text, str) or not key_text:
        key_text = "C major"
        key_source = "assumed"
    else:
        key_source = "user"
    separation = None
    accompaniment_evidence = None
    plan = chunk_plan(duration) if full_song else [(start, duration)]
    total_chunks = len(plan)
    total_steps = total_chunks + 2

    def report(phase: str, completed: int, chunks_completed: int):
        if progress:
            progress({"phase": phase, "completed": completed, "total": total_steps,
                      "chunks_completed": chunks_completed, "chunks_total": total_chunks})

    report(f"正在处理第 1/{total_chunks} 段", 0, 0)
    if full_song:
        from .separation import separate_vocals, join_stems
        chunks = []
        merged_frames = []
        merged_onsets = []
        vocals = []
        instrumental_parts = []
        instrumental_frames = []
        instrumental_onsets = []
        last_instrumental = None
        input_hash = sha256(path.read_bytes()).hexdigest()
        for index, (part_start, part_duration) in enumerate(plan):
            part_job = job / "chunks" / f"{index+1:03d}"
            if mode == "vocal":
                vocal, part_separation = separate_vocals(path, part_job, part_start, part_duration)
                vocals.append(vocal)
                instrumental = vocal.with_name("segment_other.wav")
                if instrumental.is_file():
                    instrumental_parts.append(instrumental)
                    last_instrumental = extract_evidence(instrumental, part_job, 0, part_duration,
                                                         mode="accompaniment")
                    instrumental_offset = len(instrumental_frames)
                    for frame in last_instrumental["frames"]:
                        frame["sec"] = round(part_start + frame["sec"] - last_instrumental["source_start_sec"], 6)
                    instrumental_frames.extend(last_instrumental["frames"])
                    instrumental_onsets.extend(instrumental_offset + int(i) for i in last_instrumental["onset_frames"])
                part = extract_evidence(vocal, part_job, 0, part_duration)
                part["analysis_audio_sha256"] = part["input_sha256"]
                part["input_sha256"] = input_hash
                part["channel_mix"] = "MSST vocals -> FFmpeg mono downmix"
            else:
                part_separation = None
                part = extract_evidence(path, part_job, part_start, part_duration)
            offset = len(merged_frames)
            for frame in part["frames"]:
                frame["sec"] = round(part_start + frame["sec"] - part["source_start_sec"], 6)
            merged_frames.extend(part["frames"])
            merged_onsets.extend(offset + int(i) for i in part["onset_frames"])
            chunks.append({"index": index + 1, "start_sec": part_start,
                           "decoded_duration_sec": part["decoded_duration_sec"],
                           "frame_count": len(part["frames"]), "separation": part_separation})
            next_phase = (f"正在处理第 {index+2}/{total_chunks} 段" if index + 1 < total_chunks
                          else "正在拼接人声和伴奏音轨" if mode == "vocal" else "正在合并音高证据")
            report(next_phase, index + 1, index + 1)
        if mode == "vocal":
            joined = join_stems(vocals, job / "vocal_segment.wav")
            separation = {"backend": "msst-mel-band-roformer", "vocal_sha256": sha256(joined.read_bytes()).hexdigest(),
                          "chunks": [chunk["separation"] for chunk in chunks]}
            if len(instrumental_parts) == len(plan) and last_instrumental:
                accompaniment = join_stems(instrumental_parts, job / "accompaniment_segment.wav")
                separation["accompaniment_sha256"] = sha256(accompaniment.read_bytes()).hexdigest()
                accompaniment_evidence = {key: value for key, value in last_instrumental.items()
                                          if key not in ("frames", "onset_frames")}
                accompaniment_evidence.update(input_sha256=input_hash, source_start_sec=0.0,
                    decoded_duration_sec=round(min(duration, chunks[-1]["start_sec"] + chunks[-1]["decoded_duration_sec"]), 6),
                    frames=instrumental_frames, onset_frames=instrumental_onsets)
        evidence = {key: value for key, value in part.items() if key not in ("frames", "onset_frames")}
        evidence.update(input_sha256=input_hash, source_start_sec=0.0,
                        decoded_duration_sec=round(min(duration, chunks[-1]["start_sec"] + chunks[-1]["decoded_duration_sec"]), 6),
                        frames=merged_frames, onset_frames=merged_onsets,
                        chunks=[{key: value for key, value in chunk.items() if key != "separation"} for chunk in chunks])
        (job / "pitch_evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    elif mode == "vocal":
        from .separation import separate_vocals
        vocal, separation = separate_vocals(path, job, start, duration)
        instrumental = vocal.with_name("segment_other.wav")
        if instrumental.is_file():
            accompaniment_evidence = extract_evidence(instrumental, job, 0, duration,
                                                       mode="accompaniment")
            accompaniment_evidence["analysis_audio_sha256"] = accompaniment_evidence["input_sha256"]
            accompaniment_evidence["input_sha256"] = sha256(path.read_bytes()).hexdigest()
            accompaniment_evidence["source_start_sec"] = start
            for frame in accompaniment_evidence["frames"]:
                frame["sec"] = round(frame["sec"] + start, 6)
            accompaniment_evidence["channel_mix"] = "MSST other (accompaniment) -> bandpass -> mono"
            (job / "accompaniment_segment.wav").write_bytes(instrumental.read_bytes())
            separation["accompaniment_sha256"] = sha256(instrumental.read_bytes()).hexdigest()
        evidence = extract_evidence(vocal, job, 0, duration)
        evidence["analysis_audio_sha256"] = evidence["input_sha256"]
        evidence["input_sha256"] = sha256(path.read_bytes()).hexdigest()
        evidence["source_start_sec"] = start
        for frame in evidence["frames"]:
            frame["sec"] = round(frame["sec"] + start, 6)
        evidence["channel_mix"] = "MSST vocals -> FFmpeg mono downmix"
        (job / "pitch_evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        report("正在合并音高证据", 1, 1)
    else:
        evidence = extract_evidence(path, job, start, duration)
        report("正在合并音高证据", 1, 1)
    score, summary = notes_to_score(evidence, source_name=source_name, bpm=tempo,
                                    key_text=key_text, do_midi=do, key_source=key_source)
    accompaniment_summary = None
    if accompaniment_evidence:
        accompaniment_score, accompaniment_summary = notes_to_score(
            accompaniment_evidence, source_name=source_name + " · 伴奏旋律候选", bpm=tempo,
            key_text=key_text, do_midi=do, key_source=key_source, allow_no_notes=True,
            min_note_frames=10)
        (job / "accompaniment_score.json").write_text(
            json.dumps(accompaniment_score, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        (job / "accompaniment_evidence.json").write_text(
            json.dumps(accompaniment_evidence, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    report("正在导出乐谱与音频", total_chunks + 1, total_chunks)
    manifest = {"source_name": source_name, "input_sha256": evidence["input_sha256"],
                "backend": evidence["backend"], "backend_version": evidence["backend_version"],
                "segment_start_sec": start, "decoded_duration_sec": evidence["decoded_duration_sec"],
                "requested_duration_sec": duration, "tempo_bpm_q": tempo,
                "tempo_source": "user_or_default_unverified",
                "key_source": key_source, "meter_source": "assumed", "meter_alignment_status": "unconfirmed",
                "time_grid_q": "1/4", "analysis_mode": mode, "full_song": full_song,
                "pitch_completion_version": "candidate-and-short-gap-r2",
                "chunks": evidence.get("chunks", []),
                "separation": separation, "accompaniment": accompaniment_summary, **summary}
    return score, manifest
