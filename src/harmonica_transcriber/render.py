"""同一渲染事件流提供简谱和 TAB；小节线只改变显示。"""
from fractions import Fraction
import re

from .errors import ContractError
from .rationals import read_q, str_q
from .theory import active_key, pitch_name, spell_pitch


def _meter_at(meters: list[dict], q: Fraction) -> dict:
    result = meters[0]
    for item in meters:
        if read_q(item["at_q"], "meter.at_q") <= q:
            result = item
        else:
            break
    return result


def bar_boundaries(score: dict) -> list[Fraction]:
    start = read_q(score["score_start_q"], "score_start_q")
    end = read_q(score["score_end_q"], "score_end_q")
    if end == start:
        return [start, end]
    first_full = read_q(score["first_full_bar_q"], "first_full_bar_q")
    meters = score["meter_map"]
    boundaries = [start]
    cursor = start
    while cursor < end:
        if cursor < first_full:
            next_q = first_full
        else:
            active = _meter_at(meters, cursor)
            length = Fraction(active["numerator"] * 4, active["denominator"])
            at = read_q(active["at_q"], "meter.at_q")
            anchor = max(first_full, at)
            next_q = anchor + ((cursor - anchor) // length + 1) * length
        future_changes = [read_q(item["at_q"], "meter.at_q") for item in meters if cursor < read_q(item["at_q"], "meter.at_q") < next_q]
        if future_changes:
            next_q = min(future_changes)
        next_q = min(next_q, end)
        if next_q <= cursor:
            raise ContractError("meter_map", "无法继续生成小节")
        boundaries.append(next_q)
        cursor = next_q
    return boundaries


def render_events(score: dict, arrangement: dict | None = None) -> list[list[dict]]:
    boundaries = bar_boundaries(score)
    by_id = {event["id"]: event for event in arrangement["events"]} if arrangement else {}
    bars = []
    for left, right in zip(boundaries, boundaries[1:]):
        bar = []
        for event in score["events"]:
            at = read_q(event["start_q"], "event.start_q")
            end = at + read_q(event["duration_q"], "event.duration_q")
            part_start, part_end = max(left, at), min(right, end)
            if part_start >= part_end:
                continue
            part = {"id": event["id"], "kind": event["kind"], "start_q": str_q(part_start),
                    "duration_q": str_q(part_end - part_start), "render_parent_id": event["id"],
                    "tie_after": event["kind"] == "note" and part_end < end,
                    "tie_before": event["kind"] == "note" and part_start > at,
                    "tuplet_group_id": event.get("tuplet_group_id")}
            if event["kind"] == "note":
                part["review_required"] = event.get("review_required", False)
                performed = by_id.get(event["id"])
                pitch = performed["played_pitch_midi"] if performed else event["pitch_midi"]
                key_map = arrangement["key_map"] if arrangement else score["key_map"]
                key = active_key(key_map, part_start)
                hint = event.get("spelling_hint") if performed is None or performed["played_pitch_midi"] == event["pitch_midi"] else None
                part["pitch_midi"] = pitch
                part["jianpu"], part["pitch_name"] = spell_pitch(pitch, key, hint)
                if performed is not None:
                    f = performed.get("fingering")
                    part["tab"] = f["tab"] if f else f"X[{pitch_name(pitch)}]"
            bar.append(part)
        bars.append(bar)
    return bars


def _tokens(bar: list[dict], mode: str) -> list[str]:
    rendered = []
    for event in bar:
        if event["kind"] == "rest":
            base = "0"
        elif event["kind"] == "unknown":
            base = "?"
        else:
            base = event["tab"] if mode == "tab" else _export_jianpu(event["jianpu"])
        if event.get("review_required"):
            base += "*"
        rendered.append(base + ":" + event["duration_q"] + (" ~" if event["tie_after"] else ""))
    output = []
    i = 0
    while i < len(rendered):
        group = bar[i].get("tuplet_group_id")
        if group and i + 2 < len(bar) and all(bar[j].get("tuplet_group_id") == group and bar[j]["duration_q"] == "1/3" for j in range(i, i+3)):
            output.append("T3:2(" + " ".join(rendered[i:i+3]) + ")")
            i += 3
        else:
            output.append(rendered[i])
            i += 1
    return output


def _export_jianpu(token: str) -> str:
    """Use visible brackets for octaves in exported text, keeping internal tokens unchanged."""
    match = re.fullmatch(r"([#b]*[1-7])([',]*)", token)
    if not match:
        return token
    note, octave = match.groups()
    if octave.startswith("'"):
        return "[" * len(octave) + note + "]" * len(octave)
    if octave.startswith(","):
        return "(" * len(octave) + note + ")" * len(octave)
    return note


def markdown_score(score: dict, arrangement: dict | None = None, *, tab_only: bool = False) -> str:
    keys = arrangement["key_map"] if arrangement else score["key_map"]
    first = keys[0]
    meter = score["meter_map"][0]
    identity = "口琴编配" if arrangement else "原始谱面"
    title = f"# 扒谱洛夫 · {identity}\n\n"
    header = (f"调性：{first['tonic_spelling']} {first['mode']}；无点 1={pitch_name(first['do_midi'])}；"
              f"拍号：{meter['numerator']}/{meter['denominator']}；时间位置单位：四分音符 q。")
    start = read_q(score["score_start_q"], "score_start_q")
    end = read_q(score["score_end_q"], "score_end_q")
    first_full = read_q(score["first_full_bar_q"], "first_full_bar_q")
    if start < first_full:
        header += f" 弱起：{str_q(first_full-start)}q，首完整小节从 q={str_q(first_full)} 开始。"
    elif start > first_full:
        active = _meter_at(score["meter_map"], start)
        length = Fraction(active["numerator"] * 4, active["denominator"])
        anchor = max(first_full, read_q(active["at_q"], "meter.at_q"))
        if (start-anchor) % length:
            header += f" 谱面从完整小节中的 q={str_q(start)} 开始。"
    if first["mode"] == "minor":
        header += f" 小调采用 la-based，主音 6={first['tonic_spelling']}。"
    if score.get("meter_alignment_status") == "unconfirmed":
        header += " 小节对齐未确认；暂以 MIDI tick 0 为谱面起点。"
    if arrangement:
        header += f" 全局移调：{arrangement['global_transpose']:+d} 半音。"
    bars = render_events(score, arrangement)
    if end > first_full and bars and bars[-1]:
        last_start = read_q(bars[-1][0]["start_q"], "render.start_q")
        active = _meter_at(score["meter_map"], last_start)
        length = Fraction(active["numerator"] * 4, active["denominator"])
        anchor = max(first_full, read_q(active["at_q"], "meter.at_q"))
        if (end-anchor) % length:
            header += f" 尾部残小节：{str_q(end-last_start)}q。"
    jianpu = " | ".join(" ".join(_tokens(bar, "jianpu")) for bar in bars)
    tab = " | ".join(" ".join(_tokens(bar, "tab")) for bar in bars) if arrangement else ""
    if tab_only:
        body = "TAB  | " + tab + " |"
    else:
        body = "简谱 | " + jianpu + " |"
        if arrangement:
            body += "\nTAB  | " + tab + " |"
    footer = ("\n\n文本简谱用 `(1)` 表示低音、`((1))` 表示倍低音、`[1]` 表示高音；升降号写在括号内，如 `[#1]`。"
              "`*` 标记根据候选音高或短缺口补出的待校对音；`?` 表示仍无法判断，`0` 表示低能量休止，"
              "`X[...]` 表示当前音表不可吹；`~` 是同一音跨小节延续。\n")
    return title + header + "\n\n```text\n" + body + "\n```" + footer
