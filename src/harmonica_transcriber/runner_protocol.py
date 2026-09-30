"""音频外部后端的版本化任务协议；本模块不加载任何音频模型。"""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from .errors import ContractError
from .rationals import number

PROTOCOL_VERSION = "1.0"


class RunnerFailure(Exception):
    def __init__(self, code: str, message: str, job_dir: Path):
        self.code = code
        self.job_dir = job_dir
        super().__init__(message)


def file_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strict_json(path: Path) -> dict:
    def reject(value):
        raise ValueError("非有限 JSON 数值：" + value)
    obj = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject)
    if not isinstance(obj, dict):
        raise ValueError("根节点必须是对象")
    return obj


def validate_request(request: dict) -> dict:
    if not isinstance(request, dict) or request.get("protocol_version") != PROTOCOL_VERSION:
        raise ContractError("runner.protocol_version", "需要 RunnerRequest 1.0")
    for field in ("job_id", "backend", "source_recording_id"):
        if not isinstance(request.get(field), str) or not request[field]:
            raise ContractError("runner." + field, "需要非空字符串")
    if not isinstance(request.get("backend_config"), dict):
        raise ContractError("runner.backend_config", "需要配置对象")
    if not isinstance(request.get("backend_version"), str) or not request["backend_version"]:
        raise ContractError("runner.backend_version", "需要后端版本")
    for field in ("weight_sha256", "config_sha256", "dependency_lock_sha256"):
        digest = request.get(field)
        if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ContractError("runner." + field, "应为小写 SHA-256 或 null")
    if request.get("device_policy") not in ("auto", "cpu", "cuda"):
        raise ContractError("runner.device_policy", "需要 auto/cpu/cuda")
    value = number(request.get("segment_start_sec"), "runner.segment_start_sec")
    if value < 0:
        raise ContractError("runner.segment_start_sec", "绝对起点不能为负")
    input_spec = request.get("input")
    if not isinstance(input_spec, dict) or not isinstance(input_spec.get("path"), str):
        raise ContractError("runner.input", "需要输入路径与 SHA-256")
    path = Path(input_spec["path"])
    if not path.is_absolute() or not path.is_file():
        raise ContractError("runner.input.path", "需要现有绝对文件路径")
    digest = input_spec.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ContractError("runner.input.sha256", "需要小写 SHA-256")
    if file_hash(path) != digest:
        raise ContractError("runner.input.sha256", "与当前输入文件内容不一致")
    return request


def validate_manifest(manifest_path: Path, request: dict, *, enforce_job_id: bool = True) -> dict:
    try:
        manifest = _strict_json(manifest_path)
    except (OSError, ValueError, UnicodeError) as exc:
        raise ContractError("runner.manifest", f"缺失或无效：{exc}") from exc
    fields = [("protocol_version", PROTOCOL_VERSION), ("backend", request["backend"]),
              ("backend_version", request["backend_version"]),
              ("input_sha256", request["input"]["sha256"]), ("status", "complete")]
    if enforce_job_id:
        fields.append(("job_id", request["job_id"]))
    elif not isinstance(manifest.get("job_id"), str) or not manifest["job_id"]:
        raise ContractError("runner.manifest.job_id", "缓存产物缺少原始任务 ID")
    for field, expected in fields:
        if manifest.get(field) != expected:
            raise ContractError("runner.manifest." + field, f"与请求不一致，应为 {expected}")
    time_spec = manifest.get("time")
    if not isinstance(time_spec, dict):
        raise ContractError("runner.manifest.time", "缺少时间元数据")
    for field in ("sample_rate", "hop_samples"):
        value = time_spec.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ContractError("runner.manifest.time." + field, "必须为正整数")
    for field in ("frame_count", "left_padding_samples", "right_padding_samples",
                  "trim_left_samples", "trim_right_samples"):
        value = time_spec.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ContractError("runner.manifest.time." + field, "必须为非负整数")
    if time_spec.get("frame_center") not in ("left", "center"):
        raise ContractError("runner.manifest.time.frame_center", "应为 left/center")
    if abs(number(time_spec.get("source_start_sec"), "runner.manifest.time.source_start_sec")
           - request["segment_start_sec"]) > 1e-9:
        raise ContractError("runner.manifest.time.source_start_sec", "片段绝对起点不一致")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ContractError("runner.manifest.artifacts", "至少需要一个可校验产物")
    root = manifest_path.parent.resolve()
    seen = set()
    for i, item in enumerate(artifacts):
        field = f"runner.manifest.artifacts[{i}]"
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ContractError(field, "需要相对路径")
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts or item["path"] in seen:
            raise ContractError(field + ".path", "路径必须唯一且留在任务目录内")
        seen.add(item["path"])
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise ContractError(field + ".path", "产物不存在或路径越界")
        if target.stat().st_size != item.get("size_bytes") or file_hash(target) != item.get("sha256"):
            raise ContractError(field, "产物大小或 SHA-256 不匹配")
    return manifest


def _cache_key(request: dict, interpreter: Path, script: Path) -> str:
    stable = {key: value for key, value in request.items() if key not in ("job_id", "output_dir")}
    stable["runner_script_sha256"] = file_hash(script)
    stable["interpreter_path"] = str(interpreter.resolve())
    return sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def run_worker(request: dict, *, interpreter: Path, script: Path, jobs_root: Path,
               timeout_sec: float = 30.0) -> dict:
    """只有完整 manifest 才进缓存；失败的 staging 保留报告以便诊断。"""
    validate_request(request)
    number(timeout_sec, "runner.timeout_sec", positive=True)
    if not interpreter.is_file() or not script.is_file():
        raise ContractError("runner.worker", "解释器或脚本不存在")
    jobs_root = jobs_root.resolve()
    cache_root = jobs_root / "cache"
    stage_root = jobs_root / "staging"
    cache_root.mkdir(parents=True, exist_ok=True)
    stage_root.mkdir(parents=True, exist_ok=True)
    key = _cache_key(request, interpreter, script)
    cached = cache_root / key
    if cached.exists():
        try:
            validate_manifest(cached / "output" / "manifest.json", request, enforce_job_id=False)
        except ContractError as exc:
            raise RunnerFailure("CACHE_CORRUPT", str(exc), cached) from exc
        return {"cache_hit": True, "path": str(cached), "manifest": str(cached / "output" / "manifest.json")}
    stage = Path(tempfile.mkdtemp(prefix="job-", dir=stage_root))
    output_dir = stage / "output"
    output_dir.mkdir()
    (stage / "tmp").mkdir()
    (stage / "cache").mkdir()
    staged_request = dict(request)
    staged_request["output_dir"] = str(output_dir.resolve())
    request_path = stage / "request.json"
    request_path.write_text(json.dumps(staged_request, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    env = os.environ.copy()
    env.update(PYTHONDONTWRITEBYTECODE="1", TEMP=str(stage / "tmp"), TMP=str(stage / "tmp"),
               XDG_CACHE_HOME=str(stage / "cache"), TORCH_HOME=str(stage / "cache"))
    started = time.monotonic()
    try:
        process = subprocess.run([str(interpreter), "-B", str(script), "--request", str(request_path)],
                                 cwd=stage, env=env, capture_output=True, timeout=timeout_sec,
                                 check=False)
        (stage / "stdout.log").write_bytes(process.stdout)
        (stage / "stderr.log").write_bytes(process.stderr)
        code = process.returncode
        if code:
            failure = "EXIT_NONZERO"
            detail = f"外部 worker 退出码 {code}"
        else:
            failure = None
            detail = None
    except subprocess.TimeoutExpired as exc:
        (stage / "stdout.log").write_bytes(exc.stdout or b"")
        (stage / "stderr.log").write_bytes(exc.stderr or b"")
        code = None
        failure = "TIMEOUT"
        detail = f"超过 {timeout_sec} 秒"
    except OSError as exc:
        (stage / "stdout.log").write_bytes(b"")
        (stage / "stderr.log").write_text(str(exc), encoding="utf-8")
        code = None
        failure = "LAUNCH_FAILURE"
        detail = str(exc)
    manifest_path = output_dir / "manifest.json"
    if failure is None:
        try:
            validate_manifest(manifest_path, request)
        except ContractError as exc:
            failure, detail = "MANIFEST_INVALID", str(exc)
    report = {"protocol_version": PROTOCOL_VERSION, "job_id": request["job_id"],
              "backend": request["backend"], "status": "failed" if failure else "complete",
              "failure_code": failure, "failure_detail": detail, "exit_code": code,
              "elapsed_sec": time.monotonic() - started, "cache_key": key,
              "stdout_path": "stdout.log", "stderr_path": "stderr.log"}
    (stage / "runner_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if failure:
        raise RunnerFailure(failure, detail or failure, stage)
    os.replace(stage, cached)
    return {"cache_hit": False, "path": str(cached), "manifest": str(cached / "output" / "manifest.json")}
