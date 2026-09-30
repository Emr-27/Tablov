"""不执行外部模型的本机资源静态探测。"""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys

from . import __version__
from .errors import ContractError
from .harmonica import validate_profile


def doctor_main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(prog="扒谱洛夫 doctor")
    parser.add_argument("--config", type=Path, default=root / "configs" / "local_backends.local.json")
    parser.add_argument("--output", type=Path, default=root / "runtime" / "doctor.json")
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        if config.get("config_version") != "1.0" or not isinstance(config.get("resources"), dict):
            raise ContractError("doctor.config", "资源配置版本或结构无效")
        resources = {}
        for name, value in config["resources"].items():
            if not isinstance(value, str):
                raise ContractError("doctor.config.resources." + name, "路径应为字符串")
            p = Path(value)
            stat = p.stat() if p.is_file() else None
            resources[name] = {"path": str(p), "available_static": stat is not None,
                               "size_bytes": stat.st_size if stat else None,
                               "dependency_checked": None, "model_loaded": None,
                               "inference_checked": None, "timing_checked": None}
        profile = json.loads((root / "profiles" / "chromatic_12_c_conservative.json").read_text(encoding="utf-8"))
        validate_profile(profile)
        report = {"product": "扒谱洛夫", "version": __version__,
                  "checked_at": datetime.now(timezone.utc).isoformat(),
                  "scope": "路径和独立核心环境静态检查；没有启动旧解释器、加载权重或运行音频推理",
                  "core": {"python": sys.version.split()[0], "executable": sys.executable,
                           "separate_prefix": sys.prefix != sys.base_prefix,
                           "torch_visible_in_core": importlib.util.find_spec("torch") is not None,
                           "profile_valid": True}, "resources": resources}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"扒谱洛夫：资源状态已写入 {args.output.resolve()}")
        return 0
    except (OSError, ValueError, ContractError) as exc:
        print(f"扒谱洛夫 doctor 失败：{exc}", file=sys.stderr)
        return 3
