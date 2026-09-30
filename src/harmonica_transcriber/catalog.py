"""只读登记现有 WAV；源组、真值与逐样本对齐不会由文件名臆测确认。"""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import struct
import sys

from .errors import ContractError

SUFFIXES = ("_vocals_Vocals_noreverb", "_vocals_Instrumental", "_vocals_Vocals", "_vocals", "_other")


def _wav_header(path: Path) -> dict:
    with path.open("rb") as file:
        header = file.read(12)
        if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
            raise ContractError(path.name, "不是受支持的 RIFF/WAVE")
        fmt = None
        data_bytes = None
        while True:
            chunk = file.read(8)
            if len(chunk) == 0:
                break
            if len(chunk) != 8:
                raise ContractError(path.name, "WAV chunk 截断")
            label, size = struct.unpack("<4sI", chunk)
            if label == b"fmt ":
                body = file.read(size)
                if len(body) < 16:
                    raise ContractError(path.name, "fmt chunk 不完整")
                audio_format, channels, rate, byte_rate, block_align, bits = struct.unpack_from("<HHIIHH", body)
                fmt = {"audio_format_code": audio_format, "channels": channels,
                       "sample_rate": rate, "byte_rate": byte_rate,
                       "block_align": block_align, "bits_per_sample": bits}
            elif label == b"data":
                data_bytes = size
                file.seek(size, 1)
            else:
                file.seek(size, 1)
            if size & 1:
                file.seek(1, 1)
        if fmt is None or data_bytes is None:
            raise ContractError(path.name, "缺少 fmt 或 data chunk")
        if not fmt["sample_rate"] or not fmt["block_align"] or data_bytes % fmt["block_align"]:
            raise ContractError(path.name, "采样率、block_align 或 data 长度无效")
        samples = data_bytes // fmt["block_align"]
        return {**fmt, "data_bytes": data_bytes, "sample_count": samples,
                "duration_sec": samples / fmt["sample_rate"]}


def catalog_main(argv: list[str]) -> int:
    project = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(prog="扒谱洛夫 catalog")
    parser.add_argument("--root", type=Path, help="只读扫描的 WAV 目录；默认取本机配置")
    parser.add_argument("--output", type=Path, default=project / "runtime" / "audio_catalog.json")
    args = parser.parse_args(argv)
    try:
        if args.root:
            root = args.root.resolve()
        else:
            config = json.loads((project / "configs" / "local_backends.local.json").read_text(encoding="utf-8"))
            root = Path(config["audio_asset_root"]).resolve()
        if not root.is_dir():
            raise ContractError("--root", f"找不到目录：{root}")
        files = sorted(root.glob("*.wav"), key=lambda p: p.name)
        entries = []
        for path in files:
            stem = path.stem
            suffix = next((s for s in SUFFIXES if stem.endswith(s)), None)
            group = stem[:-len(suffix)] if suffix else stem
            digest = sha256()
            with path.open("rb") as file:
                for block in iter(lambda: file.read(1024 * 1024), b""):
                    digest.update(block)
            entries.append({"path": str(path.resolve()), "sha256": digest.hexdigest(),
                            "name": path.name, "source_group_candidate": group,
                            "processing_suffix": suffix, "source_identity_confirmed": False,
                            "sample_alignment_confirmed": False, "ground_truth_available": False,
                            "split": None, "wav": _wav_header(path)})
        groups = sorted({entry["source_group_candidate"] for entry in entries})
        report = {"product": "扒谱洛夫", "catalog_version": "1.0",
                  "checked_at": datetime.now(timezone.utc).isoformat(),
                  "root": str(root), "method": "只读文件哈希与 WAV 头；未试听、未核对真值/逐样本对齐",
                  "file_count": len(entries), "source_group_candidate_count": len(groups),
                  "source_group_candidates": groups, "files": entries}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"扒谱洛夫：已登记 {len(entries)} 个 WAV、{len(groups)} 个候选源组；{args.output.resolve()}")
        return 0
    except (OSError, ValueError, KeyError, ContractError) as exc:
        print(f"扒谱洛夫 catalog 失败：{exc}", file=sys.stderr)
        return 3
