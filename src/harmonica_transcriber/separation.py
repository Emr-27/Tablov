"""Use the existing local MSST vocal model on one selected song excerpt."""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess

from .audio import CONFIG
from .errors import ContractError

RUNNER = Path(__file__).with_name("msst_runner.py")


def separate_vocals(path: Path, job: Path, start: float, duration: float) -> tuple[Path, dict]:
    path = path.resolve()
    job = job.resolve()
    try:
        resources = json.loads(CONFIG.read_text(encoding="utf-8"))["resources"]
        python = Path(resources["msst_python"])
        cli = Path(resources["msst_cli"])
        weight = Path(resources["msst_vocal_weight"])
        config = Path(resources["msst_vocal_config"])
        relative_config = Path(resources["msst_relative_config"])
        ffmpeg = Path(resources["ffmpeg"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ContractError("人声分离", "本机 MSST 配置不可用") from exc
    if not all(item.is_file() for item in (python, cli, weight, config, relative_config, ffmpeg)):
        raise ContractError("人声分离", "本机 MSST 模型或程序文件缺失，请检查本机配置")
    root = cli.parent.parent
    staging = job / "msst"
    input_dir = staging / "input"
    output_dir = staging / "output"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(exist_ok=True)
    (staging / "data_backup").mkdir(exist_ok=True)
    shutil.copyfile(relative_config, staging / "data_backup" / "webui_config.json")
    clip = input_dir / "segment.wav"
    decode = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
              "-i", str(path), "-ss", str(start), "-t", str(duration), "-ac", "2",
              "-ar", "44100", "-c:a", "pcm_f32le", str(clip)]
    try:
        done = subprocess.run(decode, capture_output=True, timeout=120, check=False,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise ContractError("人声分离", "选段解码超时") from exc
    if done.returncode or not clip.is_file() or clip.stat().st_size < 10000:
        detail = done.stderr.decode("utf-8", errors="replace")[-300:].strip()
        raise ContractError("人声分离", detail or "选段无法解码")
    env = os.environ.copy()
    env["PATH"] = str(ffmpeg.parent) + os.pathsep + env.get("PATH", "")
    # Bundled librosa 0.9.2 otherwise spends minutes compiling eager Numba
    # functions during import; source separation itself runs on Torch/CUDA.
    env["NUMBA_DISABLE_JIT"] = "1"
    command = [str(python), "-B", str(RUNNER), str(staging), str(cli), "--device", "auto",
               "--model_type", "mel_band_roformer", "--model_path", str(weight),
               "--config_path", str(config), "--input_folder", str(input_dir),
               "--output_folder", str(output_dir), "--output_format", "wav",
               "--wav_bit_depth", "FLOAT"]
    try:
        done = subprocess.run(command, cwd=root, env=env, capture_output=True, timeout=600,
                              check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise ContractError("人声分离", "MSST 运行超过 10 分钟；请选择更短片段") from exc
    vocal = output_dir / "segment_vocals.wav"
    instrumental = output_dir / "segment_other.wav"
    if done.returncode or not vocal.is_file() or vocal.stat().st_size < 10000:
        detail = done.stderr.decode("utf-8", errors="replace")[-500:].strip()
        raise ContractError("人声分离", detail or "MSST 未产出主唱音轨；请检查任务日志")
    if not instrumental.is_file() or instrumental.stat().st_size < 10000:
        raise ContractError("伴奏分离", "MSST 未产出伴奏音轨；请检查任务日志")
    manifest = {"backend": "msst-mel-band-roformer", "model_weight": weight.name,
                "model_config": config.name, "source_sha256": sha256(path.read_bytes()).hexdigest(),
                "clip_sha256": sha256(clip.read_bytes()).hexdigest(),
                "vocal_sha256": sha256(vocal.read_bytes()).hexdigest(),
                "accompaniment_sha256": sha256(instrumental.read_bytes()).hexdigest(),
                "accompaniment_backend_stem_name": "other",
                "segment_start_sec": start, "segment_requested_duration_sec": duration}
    return vocal, manifest


def join_stems(parts: list[Path], output: Path) -> Path:
    """Concatenate separated chunks without loading the full song into Python."""
    if not parts:
        raise ContractError("人声分离", "没有可拼接的人声片段")
    resources = json.loads(CONFIG.read_text(encoding="utf-8"))["resources"]
    ffmpeg = Path(resources["ffmpeg"])
    playlist = output.with_suffix(".ffconcat")
    playlist.write_text("ffconcat version 1.0\n" + "".join(
        "file '" + str(part.resolve()).replace("\\", "/").replace("'", "'\\''") + "'\n"
        for part in parts), encoding="utf-8")
    command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-f", "concat", "-safe", "0", "-i", str(playlist),
               "-ac", "2", "-ar", "44100", "-c:a", "pcm_f32le", str(output)]
    try:
        done = subprocess.run(command, capture_output=True, timeout=300, check=False,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise ContractError("人声分离", "整首人声音轨拼接超时") from exc
    if done.returncode or not output.is_file():
        detail = done.stderr.decode("utf-8", errors="replace")[-500:].strip()
        raise ContractError("人声分离", detail or "整首人声音轨拼接失败")
    return output


join_vocals = join_stems
