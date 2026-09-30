"""只监听本机的浏览器界面；生成操作复用 CLI 核心。"""
import argparse
import base64
import binascii
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import secrets
import shutil
import threading
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit
from urllib.request import build_opener, ProxyHandler
import webbrowser
import zipfile

from . import __version__
from .cli import run
from .errors import ContractError
from .models import validate_score
from .rationals import integer
from .render import render_events

ROOT = Path(__file__).resolve().parents[2]
WEB = Path(__file__).parent / "web"
MAX_REQUEST = 136 * 1024 * 1024
MAX_FILE = 96 * 1024 * 1024
OUTPUT_NAMES = {"original_score.json", "arrangement.json", "melody_original.md",
                "melody_harmonica.md", "harmonica_tab.md", "melody_original.mid",
                "melody_harmonica.mid", "melody_original.mid.json",
                "melody_harmonica.mid.json", "report.json", "bapuluofu.zip",
                "audio_manifest.json", "pitch_evidence.json", "vocal_segment.wav", "vocal_preview.mp3"}
OUTPUT_NAMES.update({"accompaniment_score.json", "accompaniment_melody.md",
                     "accompaniment_melody.mid", "accompaniment_evidence.json",
                     "accompaniment_preview.mp3"})


def _decode_upload(item: dict, extensions: set[str]) -> tuple[str, bytes]:
    if not isinstance(item, dict) or not isinstance(item.get("name"), str):
        raise ContractError("文件", "请选择文件")
    name = item["name"].replace("\\", "/").rsplit("/", 1)[-1]
    suffix = Path(name).suffix.lower()
    if suffix not in extensions:
        raise ContractError("文件", "当前支持 MP3、WAV、MP4、M4A、MIDI 和 Score JSON")
    try:
        data = base64.b64decode(item.get("data", ""), validate=True)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ContractError("文件", "上传内容无效，请重新选择") from exc
    if not data or len(data) > MAX_FILE:
        raise ContractError("文件", "文件应为 1 字节至 96 MB")
    return suffix, data


def generate(payload: dict, jobs_root: Path, progress=None) -> dict:
    if not isinstance(payload, dict):
        raise ContractError("请求", "请求格式无效")
    settings = payload.get("settings", {})
    if not isinstance(settings, dict):
        raise ContractError("设置", "设置格式无效")
    progress_state = {"status": "running", "phase": "正在准备输入", "completed": 0,
                      "total": None, "chunks_completed": 0, "chunks_total": None}

    def emit(update):
        progress_state.update(update)
        if progress:
            progress(dict(progress_state))

    emit({})
    transpose = integer(settings.get("transpose", 0), "移调", -48, 48)
    if payload.get("example_audio") is True:
        sample = ROOT / "examples" / "c_major_four_notes.wav"
        payload = dict(payload)
        payload["input"] = {"name": sample.name, "data": base64.b64encode(sample.read_bytes()).decode("ascii")}
    origin = settings.get("midi_origin") or None
    if origin not in (None, "score-start", "audio"):
        raise ContractError("播放起点", "请选择有效的播放起点")
    job_id = secrets.token_hex(12)
    folder = jobs_root / job_id
    folder.mkdir(parents=True)
    audio_manifest = None
    if payload.get("example") is True:
        source = ROOT / "examples" / "g_major_four_notes.json"
        path = folder / "input.json"
        path.write_bytes(source.read_bytes())
        name = "G 大调 · 四音示例"
    elif "score" in payload:
        score = validate_score(payload["score"])
        parent_id = payload.get("parent_job_id")
        if parent_id and score["source"]["kind"] == "audio":
            if (not isinstance(parent_id, str) or len(parent_id) != 24 or
                    any(c not in "0123456789abcdef" for c in parent_id)):
                raise ContractError("音频证据", "原任务编号无效")
            parent_output = jobs_root / parent_id / "output"
            parent_score_path = parent_output / "original_score.json"
            parent_manifest = parent_output / "audio_manifest.json"
            parent_evidence = parent_output / "pitch_evidence.json"
            if not all(p.is_file() for p in (parent_score_path, parent_manifest, parent_evidence)):
                raise ContractError("音频证据", "找不到原任务的分析文件")
            previous = json.loads(parent_score_path.read_text(encoding="utf-8"))
            audio_manifest = json.loads(parent_manifest.read_text(encoding="utf-8"))
            if (previous["revision_id"] != score["revision_id"] or
                    previous["source"].get("sha256") != score["source"].get("sha256") or
                    audio_manifest["input_sha256"] != score["source"].get("sha256")):
                raise ContractError("音频证据", "原任务与当前乐谱不匹配")
            (folder / "pitch_evidence.json").write_bytes(parent_evidence.read_bytes())
            if audio_manifest.get("analysis_mode") == "vocal":
                parent_vocal = parent_output / "vocal_segment.wav"
                if (not parent_vocal.is_file() or
                        sha256(parent_vocal.read_bytes()).hexdigest() != audio_manifest["separation"]["vocal_sha256"]):
                    raise ContractError("音频证据", "原任务的主唱音轨缺失或已改变")
                shutil.copyfile(parent_vocal, folder / "vocal_segment.wav")
                parent_preview = parent_output / "vocal_preview.mp3"
                if parent_preview.is_file():
                    shutil.copyfile(parent_preview, folder / "vocal_preview.mp3")
                for extra in ("accompaniment_score.json", "accompaniment_evidence.json",
                              "accompaniment_preview.mp3"):
                    if (parent_output / extra).is_file():
                        shutil.copyfile(parent_output / extra, folder / extra)
        edits = payload.get("edits", [])
        if not isinstance(edits, list):
            raise ContractError("音高校正", "修改列表无效")
        score = deepcopy(score)
        by_id = {e["id"]: e for e in score["events"]}
        for edit in edits:
            if not isinstance(edit, dict) or edit.get("id") not in by_id:
                raise ContractError("音高校正", "找不到要修改的音符")
            event = by_id[edit["id"]]
            if event["kind"] != "note":
                raise ContractError("音高校正", "休止和待核对区段不能直接改成音符")
            pitch = integer(edit.get("pitch_midi"), "音高", 0, 127)
            if pitch != event["pitch_midi"]:
                score["changes"].append({"action": "pitch_edit", "event_id": event["id"],
                                         "before": event["pitch_midi"], "after": pitch,
                                         "producer": "user", "reason": "界面音高校正",
                                         "rule_version": "ui-v1"})
                event["pitch_midi"] = pitch
                event["spelling_hint"] = None
                event.pop("review_required", None)
                event.pop("inference_method", None)
        score["revision_id"] += "-edit-" + job_id[:6]
        path = folder / "input.json"
        path.write_text(json.dumps(score, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        name = str(score["source"].get("name", "校正后的乐谱"))
    else:
        item = payload.get("input")
        suffix, data = _decode_upload(item, {".json", ".mid", ".midi", ".mp3", ".wav", ".mp4", ".m4a"})
        path = folder / ("input" + suffix)
        path.write_bytes(data)
        name = item["name"].replace("\\", "/").rsplit("/", 1)[-1]
        if payload.get("sidecar"):
            if suffix not in (".mid", ".midi"):
                raise ContractError("MIDI 恢复文件", "恢复文件只能与 MIDI 一起使用")
            _, sidecar = _decode_upload(payload["sidecar"], {".json"})
            Path(str(path) + ".json").write_bytes(sidecar)
        if suffix in (".mp3", ".wav", ".mp4", ".m4a"):
            from .audio import transcribe
            score, audio_manifest = transcribe(path, folder, source_name=name,
                                               start_sec=settings.get("audio_start", 0),
                                               duration_sec=settings.get("audio_duration", 30),
                                               bpm=settings.get("audio_bpm", 120),
                                               key_text=settings.get("audio_key", ""),
                                               do_midi=settings.get("audio_do_midi"),
                                               mode=settings.get("audio_mode", "direct"),
                                               full_song=settings.get("audio_full", False), progress=emit)
            path = folder / "input.json"
            path.write_text(json.dumps(score, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    is_midi = path.suffix.lower() in (".mid", ".midi")

    def optional_int(field, label, low, high):
        value = settings.get(field)
        if value is None or value == "" or not is_midi:
            return None
        return integer(value, label, low, high)

    profile_path = None
    if payload.get("profile"):
        _, content = _decode_upload(payload["profile"], {".json"})
        profile_path = folder / "profile.json"
        profile_path.write_bytes(content)
    args = SimpleNamespace(input=path, output=folder / "output", profile=profile_path,
                           instrument=None, transpose=str(transpose), octave_policy="reject",
                           midi_origin=origin, track=optional_int("track", "轨道", 0, 65535),
                           channel=optional_int("channel", "通道", 0, 15),
                           key=(settings.get("key") or None) if is_midi else None,
                           do_midi=optional_int("do_midi", "无点 1", 0, 127),
                           strict=False, overwrite=False, debug=False)
    if progress_state["total"] is None:
        emit({"phase": "正在导出乐谱", "total": 2})
    output, _ = run(args)
    score = json.loads((output / "original_score.json").read_text(encoding="utf-8"))
    arrangement = json.loads((output / "arrangement.json").read_text(encoding="utf-8"))
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    if audio_manifest is not None:
        report["implemented_capability"] = ("msst-vocals-plus-fft-yin-review" if audio_manifest["analysis_mode"] == "vocal"
                                            else "audio-fft-yin-monophonic-review")
        report["audio"] = audio_manifest
        report["source"] = {"kind": "audio", "name": audio_manifest["source_name"],
                            "sha256": audio_manifest["input_sha256"],
                            "derived_score_path": str(path.resolve())}
        report["warnings"].append("主唱音高由 FFT-YIN、伴奏候选由谐波显著度与短缺口补全生成；BPM、拍号及小节对齐未自动确认，请试听校正")
        inferred = sum(e["kind"] == "note" and e.get("review_required", False) for e in score["events"])
        if inferred:
            report["warnings"].append(f"已自动补全 {inferred} 个待校对音（谱面标 *）；请对照原音频检查，仍无法判断的区段保留 ?")
        if audio_manifest.get("full_song"):
            report["warnings"].append(f"已自动分段分析并合并 {len(audio_manifest['chunks'])} 段；分段接缝和整曲节奏仍需试听核对")
        if audio_manifest["analysis_mode"] == "vocal":
            report["warnings"].append("已用 MSST 提取主唱；分离残留、和声与伴奏仍可能干扰音高，请与原音频逐音核对")
            if audio_manifest.get("accompaniment") is not None:
                report["warnings"].append("伴奏谱仅为单线条旋律候选；和弦、多个乐器及残留人声可能造成误识别，问号处需人工核对")
                accompaniment_inferred = audio_manifest["accompaniment"].get("inferred_note_count", 0)
                if accompaniment_inferred:
                    report["warnings"].append(f"伴奏旋律另有 {accompaniment_inferred} 个 * 标记的待校对音")
        elif name.lower().endswith((".mp4", ".m4a")):
            report["warnings"].append("当前直接分析 MP4/M4A 混音；建议选择“完整歌曲 · 先提取主唱”")
        if audio_manifest["key_source"] == "assumed":
            report["warnings"].append("调性未填写，暂按 C 大调显示简谱")
        (output / "audio_manifest.json").write_text(json.dumps(audio_manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        (output / "pitch_evidence.json").write_bytes((folder / "pitch_evidence.json").read_bytes())
        audio_artifacts = ["audio_manifest.json", "pitch_evidence.json"]
        if audio_manifest["analysis_mode"] == "vocal":
            vocal_source = folder / "vocal_segment.wav"
            if not vocal_source.is_file():
                vocal_source = folder / "msst" / "output" / "segment_vocals.wav"
            shutil.copyfile(vocal_source, output / "vocal_segment.wav")
            audio_artifacts.append("vocal_segment.wav")
            from .audio import resources
            _, ffmpeg = resources()
            preview = folder / "vocal_preview.mp3"
            if not preview.is_file():
                import subprocess
                command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                           "-i", str(output / "vocal_segment.wav"), "-codec:a", "libmp3lame",
                           "-b:a", "128k", str(preview)]
                done = subprocess.run(command, capture_output=True, timeout=300, check=False)
                if done.returncode or not preview.is_file():
                    raise ContractError("人声试听", "无法生成浏览器试听音频")
            shutil.copyfile(preview, output / "vocal_preview.mp3")
            audio_artifacts.append("vocal_preview.mp3")
            accompaniment_source = folder / "accompaniment_segment.wav"
            accompaniment_preview = folder / "accompaniment_preview.mp3"
            if accompaniment_source.is_file() and not accompaniment_preview.is_file():
                import subprocess
                command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                           "-i", str(accompaniment_source), "-codec:a", "libmp3lame",
                           "-b:a", "128k", str(accompaniment_preview)]
                done = subprocess.run(command, capture_output=True, timeout=300, check=False)
                if done.returncode or not accompaniment_preview.is_file():
                    raise ContractError("伴奏试听", "无法生成伴奏浏览器试听音频")
            if accompaniment_preview.is_file():
                shutil.copyfile(accompaniment_preview, output / "accompaniment_preview.mp3")
                audio_artifacts.append("accompaniment_preview.mp3")
            accompaniment_score_path = folder / "accompaniment_score.json"
            if accompaniment_score_path.is_file():
                from .arrangement import with_performed_times
                from .midi_io import export_midi
                from .render import markdown_score
                accompaniment_score = with_performed_times(json.loads(accompaniment_score_path.read_text(encoding="utf-8")))
                (output / "accompaniment_score.json").write_text(
                    json.dumps(accompaniment_score, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
                (output / "accompaniment_melody.md").write_text(
                    markdown_score(accompaniment_score).replace("原始谱面", "伴奏旋律候选", 1), encoding="utf-8")
                midi, _ = export_midi(accompaniment_score, midi_origin="audio")
                (output / "accompaniment_melody.mid").write_bytes(midi)
                shutil.copyfile(folder / "accompaniment_evidence.json", output / "accompaniment_evidence.json")
                audio_artifacts.extend(("accompaniment_score.json", "accompaniment_melody.md",
                                        "accompaniment_melody.mid", "accompaniment_evidence.json"))
        for artifact_name in audio_artifacts:
            report["artifacts"][artifact_name] = {"path": str((output / artifact_name).resolve()),
                                                  "sha256": sha256((output / artifact_name).read_bytes()).hexdigest()}
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    emit({"phase": "正在打包下载文件", "completed": progress_state["total"] - 1})
    with zipfile.ZipFile(output / "bapuluofu.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(output.iterdir()):
            # The lossless stem can be hundreds of MB; offer it separately.
            if file.name in OUTPUT_NAMES and file.name not in ("bapuluofu.zip", "vocal_segment.wav"):
                archive.write(file, file.name)
    emit({"status": "complete", "phase": "处理完成", "completed": progress_state["total"]})
    return {"job_id": job_id, "name": name, "score": score, "arrangement": arrangement,
            "report": report, "bars": render_events(score, arrangement),
            "original_bars": render_events(score),
            "accompaniment_score": json.loads((output / "accompaniment_score.json").read_text(encoding="utf-8")) if (output / "accompaniment_score.json").is_file() else None,
            "accompaniment_bars": render_events(json.loads((output / "accompaniment_score.json").read_text(encoding="utf-8"))) if (output / "accompaniment_score.json").is_file() else None,
            "output_path": str(output.resolve()),
            "downloads": {name: f"/files/{job_id}/{name}" for name in sorted(OUTPUT_NAMES) if (output / name).is_file()}}


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, jobs_root=None):
        super().__init__(address, Handler)
        self.token = secrets.token_urlsafe(32)
        self.jobs_root = jobs_root or ROOT / "runtime" / "ui" / "jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.gate = threading.BoundedSemaphore(2)
        self.progress = {}
        self.progress_lock = threading.Lock()
        self.workspace_id = sha256(str(ROOT).encode()).hexdigest()[:16]

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.server_port}"


class Handler(BaseHTTPRequestHandler):
    server_version = "BapuluofuLocal/0.2"

    def log_message(self, fmt, *args):
        # No uploaded content or tokens in logs.
        return

    def reply(self, code, data: bytes, content_type="application/json; charset=utf-8", download=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        self.end_headers()
        self.wfile.write(data)

    def json_reply(self, code, payload):
        self.reply(code, json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))

    def file_reply(self, file: Path, name: str, *, head_only: bool = False):
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(file.stat().st_size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if name not in ("vocal_segment.wav", "vocal_preview.mp3", "accompaniment_preview.mp3"):
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.end_headers()
        if not head_only:
            try:
                with file.open("rb") as source:
                    shutil.copyfileobj(source, self.wfile, length=1024 * 1024)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_HEAD(self):
        if not self.valid_host():
            return self.json_reply(403, {"error": "请使用本机地址打开界面"})
        path = unquote(urlsplit(self.path).path)
        pieces = path.split("/")
        if len(pieces) == 4 and pieces[1] == "files":
            _, _, job, name = pieces
            if len(job) == 24 and all(c in "0123456789abcdef" for c in job) and name in OUTPUT_NAMES:
                file = self.server.jobs_root / job / "output" / name
                if file.is_file():
                    return self.file_reply(file, name, head_only=True)
        self.send_error(404)

    def valid_host(self):
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

    def do_GET(self):
        if not self.valid_host():
            return self.json_reply(403, {"error": "请使用本机地址打开界面"})
        path = unquote(urlsplit(self.path).path)
        if path == "/health":
            return self.json_reply(200, {"product": "扒谱洛夫", "workspace_id": self.server.workspace_id})
        if path.startswith("/api/progress/"):
            if not secrets.compare_digest(self.headers.get("X-Bapuluofu-Token", ""), self.server.token):
                return self.json_reply(403, {"error": "页面验证已失效，请刷新此本机页面"})
            progress_id = path.removeprefix("/api/progress/")
            if len(progress_id) != 32 or any(c not in "0123456789abcdef" for c in progress_id):
                return self.json_reply(404, {"error": "找不到进度"})
            with self.server.progress_lock:
                current = self.server.progress.get(progress_id)
                result = dict(current) if current else None
            return self.json_reply(200, result) if result else self.json_reply(404, {"error": "找不到进度"})
        if path == "/":
            html = (WEB / "index.html").read_text(encoding="utf-8")
            html = html.replace("__TOKEN__", self.server.token).replace("__VERSION__", __version__)
            return self.reply(200, html.encode("utf-8"), "text/html; charset=utf-8")
        if path in ("/app.js", "/app.css"):
            return self.reply(200, (WEB / path[1:]).read_bytes(),
                              "application/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8")
        if path.startswith("/files/"):
            pieces = path.split("/")
            if len(pieces) != 4:
                return self.json_reply(404, {"error": "找不到文件"})
            _, _, job, name = pieces
            if len(job) != 24 or any(c not in "0123456789abcdef" for c in job) or name not in OUTPUT_NAMES:
                return self.json_reply(404, {"error": "找不到文件"})
            file = self.server.jobs_root / job / "output" / name
            if not file.is_file():
                return self.json_reply(404, {"error": "文件尚未生成"})
            return self.file_reply(file, name)
        return self.json_reply(404, {"error": "找不到页面"})

    def do_POST(self):
        if not self.valid_host() or self.headers.get("Origin") != self.server.origin or not secrets.compare_digest(self.headers.get("X-Bapuluofu-Token", ""), self.server.token):
            return self.json_reply(403, {"error": "页面验证已失效，请刷新此本机页面"})
        if urlsplit(self.path).path != "/api/generate":
            return self.json_reply(404, {"error": "找不到接口"})
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.json_reply(415, {"error": "请求格式应为 JSON"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            size = 0
        if not 0 < size <= MAX_REQUEST:
            return self.json_reply(413, {"error": "请求过大；单个音频文件最多 96 MB"})
        if not self.server.gate.acquire(blocking=False):
            return self.json_reply(429, {"error": "正在生成其他乐谱，请稍后重试"})
        progress_id = None
        progress_registered = False

        def update_progress(update):
            if progress_registered:
                with self.server.progress_lock:
                    self.server.progress[progress_id].update(update)

        try:
            payload = json.loads(self.rfile.read(size).decode("utf-8"))
            progress_id = payload.get("progress_id") if isinstance(payload, dict) else None
            if progress_id is not None:
                if (not isinstance(progress_id, str) or len(progress_id) != 32 or
                        any(c not in "0123456789abcdef" for c in progress_id)):
                    raise ContractError("进度编号", "无效")
                with self.server.progress_lock:
                    if progress_id in self.server.progress:
                        raise ContractError("进度编号", "已经使用，请重新生成")
                    if len(self.server.progress) >= 64:
                        self.server.progress.pop(next(iter(self.server.progress)))
                    self.server.progress[progress_id] = {"status": "running", "phase": "正在接收文件",
                                                          "completed": 0, "total": None,
                                                          "chunks_completed": 0, "chunks_total": None}
                    progress_registered = True
            result = generate(payload, self.server.jobs_root, progress=update_progress)
            self.json_reply(200, result)
        except (ContractError, ValueError, UnicodeError, TypeError, KeyError) as exc:
            update_progress({"status": "failed", "phase": "处理失败"})
            self.json_reply(400, {"error": str(exc)})
        except Exception:
            import traceback
            traceback.print_exc()
            update_progress({"status": "failed", "phase": "处理失败"})
            self.json_reply(500, {"error": "生成失败，请查看 runtime/ui/server.log"})
        finally:
            self.server.gate.release()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="扒谱洛夫 ui")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    state_path = ROOT / "runtime" / "ui" / "server.json"
    workspace = sha256(str(ROOT).encode()).hexdigest()[:16]
    if state_path.exists():
        try:
            old = json.loads(state_path.read_text(encoding="utf-8"))
            url = old["url"]
            parts = urlsplit(url)
            if parts.scheme != "http" or parts.hostname != "127.0.0.1":
                raise ValueError("invalid local server")
            with build_opener(ProxyHandler({})).open(url + "/health", timeout=1) as response:
                health = json.load(response)
            if health.get("workspace_id") == workspace:
                if not args.no_browser:
                    webbrowser.open(url)
                return 0
        except (OSError, ValueError, KeyError):
            pass
    try:
        server = LocalServer(("127.0.0.1", args.port))
    except OSError:
        server = LocalServer(("127.0.0.1", 0))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"url": server.origin, "version": __version__,
                                     "started_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    if not args.no_browser:
        webbrowser.open(server.origin)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
