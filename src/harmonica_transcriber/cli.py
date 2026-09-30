"""扒谱洛夫 Phase 0：人工 Score 或指定单旋律 MIDI 的确定性导出。"""
import argparse
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback

from . import __version__
from .arrangement import arrange, with_performed_times
from .errors import ContractError
from .harmonica import profile_hash, validate_profile
from .midi_io import export_midi, import_midi, sidecar_for
from .models import validate_score
from .rationals import read_q, str_q
from .render import markdown_score
from .theory import active_key, spell_pitch
from .time_map import q_to_sec

PRODUCT_NAME = "扒谱洛夫"
DEFAULT_PROFILE = Path(__file__).resolve().parents[2] / "profiles" / "chromatic_12_c_conservative.json"


def _json_bytes(data: object) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False, allow_nan=False) + "\n").encode("utf-8")


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(str(path), f"无效 UTF-8 JSON：{exc}") from exc


def _profile(path: Path) -> dict:
    if path.suffix.lower() not in (".json",):
        raise ContractError("--profile", "本批使用完整 JSON profile；YAML 转换可在后续依赖确认后接入")
    return validate_profile(_load_json(path))


def _arranged_score(score: dict, arrangement: dict) -> dict:
    result = deepcopy(score)
    result["revision_id"] = arrangement["revision_id"]
    result["key_map"] = deepcopy(arrangement["key_map"])
    result["changes"] += deepcopy(arrangement["changes"])
    by_id = {event["id"]: event for event in arrangement["events"]}
    for event in result["events"]:
        if event["kind"] != "note":
            continue
        event["pitch_midi"] = by_id[event["id"]]["played_pitch_midi"]
        key = active_key(result["key_map"], read_q(event["start_q"], "event.start_q"))
        _, event["spelling_hint"] = spell_pitch(event["pitch_midi"], key)
    return validate_score(result)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="扒谱洛夫 / harmonica-export",
                                     description="人工 Score JSON 或指定单旋律 MIDI → 数字简谱、口琴 TAB 和 MIDI。")
    parser.add_argument("input", type=Path, help="Score 2.0 JSON 或 PPQ type 0/1 MIDI")
    parser.add_argument("--output", required=True, type=Path, help="输出目录")
    parser.add_argument("--profile", type=Path, help="完整 JSON 物理音表；默认保守 12 孔 C 调")
    parser.add_argument("--instrument", choices=["chromatic-12-c"], help="默认保守音表别名")
    parser.add_argument("--transpose", default="0", help="整数半音移调；original 等于 0")
    parser.add_argument("--octave-policy", default="reject", choices=["reject", "phrase"])
    parser.add_argument("--midi-origin", choices=["score-start", "audio"], help="MIDI 起点语义")
    parser.add_argument("--track", type=int, help="MIDI 音符轨，0 起")
    parser.add_argument("--channel", type=int, help="MIDI 音符通道，0 起")
    parser.add_argument("--key", help="MIDI 缺少调号时的调性，例如 G major")
    parser.add_argument("--do-midi", type=int, help="无点 1 的绝对 MIDI 音高")
    parser.add_argument("--strict", action="store_true", help="unknown/不可吹时仍输出报告并返回 4")
    parser.add_argument("--overwrite", action="store_true", help="允许替换同名产物")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--version", action="version", version=f"{PRODUCT_NAME} {__version__}")
    return parser


def _validate_args(args) -> int:
    if args.profile and args.instrument:
        raise ContractError("--profile/--instrument", "两个乐器选择参数不可同时指定")
    if args.octave_policy != "reject":
        raise ContractError("--octave-policy", "句级八度搜索属于后续阶段；当前只支持 reject")
    if args.transpose == "original":
        return 0
    if args.transpose == "easy":
        raise ContractError("--transpose", "easy 搜索属于后续阶段；当前只支持显式整数")
    try:
        return int(args.transpose)
    except ValueError as exc:
        raise ContractError("--transpose", "应为整数、original；easy 暂未实现") from exc


def _make_report(score: dict, arrangement: dict, profile: dict, input_path: Path,
                 input_hash: str, origin: str, midi_infos: dict, warnings: list[str], elapsed: float) -> dict:
    unknowns = [e for e in score["events"] if e["kind"] == "unknown"]
    unplayable = [e["id"] for e in arrangement["events"] if e.get("playable") is False]
    alignment_uncertain = score.get("meter_alignment_status") == "unconfirmed"
    unknown_seconds = sum(q_to_sec(score["time_map"], read_q(e["start_q"], "event.start_q") + read_q(e["duration_q"], "event.duration_q"))
                          - q_to_sec(score["time_map"], read_q(e["start_q"], "event.start_q")) for e in unknowns)
    return {
        "product": PRODUCT_NAME, "version": __version__, "requested_phase": 0,
        "implemented_capability": "deterministic-json-midi-export", "status": "partial" if unknowns or unplayable or alignment_uncertain else "complete",
        "source": {"path": str(input_path.resolve()), "sha256": input_hash, "kind": score["source"]["kind"]},
        "source_revision_id": score["revision_id"], "arrangement_revision_id": arrangement["revision_id"],
        "profile": {"id": profile["profile_id"], "version": profile["profile_version"],
                    "sha256": profile_hash(profile), "scope_note": profile["scope_note"]},
        "time_map": {"kind": score["time_map"]["kind"], "origin_q": score["time_map"]["origin_q"],
                     "origin_sec": score["time_map"]["origin_sec"], "midi_origin": origin,
                     "meter_alignment_status": score.get("meter_alignment_status", "declared")},
        "key_map": arrangement["key_map"], "score_duration_q": str_q(read_q(score["score_end_q"], "score_end_q") - read_q(score["score_start_q"], "score_start_q")),
        "processed_duration_sec": q_to_sec(score["time_map"], read_q(score["score_end_q"], "score_end_q")) - q_to_sec(score["time_map"], read_q(score["score_start_q"], "score_start_q")),
        "unknown_duration_sec": unknown_seconds, "unknown_event_ids": [e["id"] for e in unknowns],
        "unplayable_event_ids": unplayable, "all_notes_playable": not unplayable,
        "cost": arrangement["cost"], "changes": arrangement["changes"],
        "midi_exports": midi_infos, "scores": {"activity": None, "pitch": None, "selection": None},
        "model": None, "device": None, "cache_hit": False,
        "elapsed_sec": elapsed, "warnings": warnings, "errors": [], "artifacts": {}
    }


def _commit(output: Path, payloads: dict[str, bytes], overwrite: bool) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    existing = [name for name in payloads if (output / name).exists()]
    if existing and not overwrite:
        raise ContractError("--output", "目录中已有同名产物：" + ", ".join(existing))
    with tempfile.TemporaryDirectory(prefix="bapuluofu-stage-", dir=output.parent) as staging_name:
        stage = Path(staging_name)
        for name, content in payloads.items():
            (stage / name).write_bytes(content)
        output.mkdir(parents=True, exist_ok=True)
        for name in payloads:
            os.replace(stage / name, output / name)


def run(args) -> tuple[Path, int]:
    beginning = time.monotonic()
    transpose = _validate_args(args)
    input_path = args.input.resolve()
    if not input_path.is_file():
        raise ContractError("input", f"找不到文件：{input_path}")
    input_hash = sha256(input_path.read_bytes()).hexdigest()
    profile_path = args.profile.resolve() if args.profile else DEFAULT_PROFILE
    if not profile_path.is_file():
        raise ContractError("--profile", f"找不到配置：{profile_path}")
    profile = _profile(profile_path)
    warnings = []
    if input_path.suffix.lower() in (".mid", ".midi"):
        score, warnings = import_midi(input_path, track=args.track, channel=args.channel,
                                      key_text=args.key, do_midi=args.do_midi)
    elif input_path.suffix.lower() == ".json":
        if any(value is not None for value in (args.track, args.channel, args.key, args.do_midi)):
            raise ContractError("input", "--track/--channel/--key/--do-midi 只适用于 MIDI 导入；JSON 调性以文件为准")
        score = validate_score(_load_json(input_path))
    else:
        raise ContractError("input", "本阶段只支持 .json/.mid/.midi；音频识别尚未实现")
    origin = args.midi_origin or ("audio" if score["source"]["kind"] == "audio" else "score-start")
    original = with_performed_times(score)
    arrangement = arrange(score, profile, transpose)
    arranged = _arranged_score(score, arrangement)
    original_midi, original_info = export_midi(original, midi_origin=origin)
    harmonica_midi, harmonica_info = export_midi(arranged, midi_origin=origin)
    payloads = {
        "original_score.json": _json_bytes(original),
        "arrangement.json": _json_bytes(arrangement),
        "melody_original.md": markdown_score(original).encode("utf-8"),
        "melody_harmonica.md": markdown_score(original, arrangement).encode("utf-8"),
        "harmonica_tab.md": markdown_score(original, arrangement, tab_only=True).encode("utf-8"),
        "melody_original.mid": original_midi,
        "melody_harmonica.mid": harmonica_midi,
        "melody_original.mid.json": _json_bytes(sidecar_for(original_midi, original, original_info)),
        "melody_harmonica.mid.json": _json_bytes(sidecar_for(harmonica_midi, arranged, harmonica_info)),
    }
    report = _make_report(score, arrangement, profile, input_path, input_hash, origin,
                          {"original": original_info, "harmonica": harmonica_info}, warnings,
                          time.monotonic() - beginning)
    report["artifacts"] = {name: {"path": str((args.output.resolve() / name)), "sha256": sha256(content).hexdigest()}
                           for name, content in payloads.items()}
    payloads["report.json"] = _json_bytes(report)
    output = args.output.resolve()
    _commit(output, payloads, args.overwrite)
    return output, (4 if args.strict and report["status"] != "complete" else 0)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "ui":
        from .webui import main as ui_main
        return ui_main(argv[1:])
    if argv and argv[0] == "doctor":
        from .doctor import doctor_main
        return doctor_main(argv[1:])
    if argv and argv[0] == "catalog":
        from .catalog import catalog_main
        return catalog_main(argv[1:])
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        output, code = run(args)
        print(f"{PRODUCT_NAME}：已生成 {output}；详见 report.json")
        return code
    except ContractError as exc:
        print(f"{PRODUCT_NAME} 输入错误：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"{PRODUCT_NAME} 运行失败：{exc}", file=sys.stderr)
        if args.debug:
            traceback.print_exc()
        return 3
