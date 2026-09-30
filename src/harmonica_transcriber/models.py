"""Score 2.0 的字段校验；不改变用户的原始观测数据。"""
from copy import deepcopy
from fractions import Fraction

from .errors import ContractError
from .rationals import integer, number, read_q, str_q
from .theory import midi_spelling, validate_key
from .time_map import validate_time_map


def _check_map(score: dict, field: str, start: Fraction):
    items = score.get(field)
    if not isinstance(items, list) or not items:
        raise ContractError(field, "至少需要一项")
    previous = None
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ContractError(f"{field}[{i}]", "应为对象")
        q = read_q(item.get("at_q"), f"{field}[{i}].at_q")
        if previous is not None and q <= previous:
            raise ContractError(field, "位置必须严格递增")
        previous = q
    if read_q(items[0]["at_q"], field + "[0].at_q") > start:
        raise ContractError(field, "初始项未在谱面起点生效")
    return items


def _bar_length(meter: dict, field: str) -> Fraction:
    numerator = integer(meter.get("numerator"), field + ".numerator", 1, 99)
    denominator = integer(meter.get("denominator"), field + ".denominator", 1, 64)
    if denominator & (denominator - 1):
        raise ContractError(field + ".denominator", "拍号分母应为 2 的幂")
    if meter.get("source") not in ("user", "midi", "estimated", "assumed"):
        raise ContractError(field + ".source", "拍号来源必须明确")
    return Fraction(numerator * 4, denominator)


def validate_score(data: object) -> dict:
    if not isinstance(data, dict):
        raise ContractError("score", "根节点应为对象")
    score = deepcopy(data)
    if score.get("schema_version") != "2.0":
        raise ContractError("schema_version", "仅支持 2.0")
    if not isinstance(score.get("revision_id"), str) or not score["revision_id"].strip():
        raise ContractError("revision_id", "必须是非空字符串")
    source = score.get("source")
    if not isinstance(source, dict) or source.get("kind") not in ("manual", "midi", "audio"):
        raise ContractError("source.kind", "应为 manual/midi/audio")
    start = read_q(score.get("score_start_q"), "score_start_q")
    end = read_q(score.get("score_end_q"), "score_end_q")
    if end < start:
        raise ContractError("score_end_q", "不得早于 score_start_q")
    first_full = read_q(score.get("first_full_bar_q"), "first_full_bar_q")
    if first_full < start:
        # score_start may be after first full bar when score is an excerpt.
        pass
    validate_time_map(score.get("time_map"), start, end)

    meters = _check_map(score, "meter_map", start)
    lengths = [_bar_length(meter, f"meter_map[{i}]") for i, meter in enumerate(meters)]
    for i in range(1, len(meters)):
        at = read_q(meters[i]["at_q"], f"meter_map[{i}].at_q")
        if at < first_full:
            raise ContractError(f"meter_map[{i}].at_q", "后续拍号变化不得位于弱起段")
        previous_at = read_q(meters[i-1]["at_q"], f"meter_map[{i-1}].at_q")
        anchor = max(first_full, previous_at)
        if (at - anchor) / lengths[i-1] != int((at - anchor) / lengths[i-1]):
            raise ContractError(f"meter_map[{i}].at_q", "拍号只能在完整小节边界变化")
    keys = _check_map(score, "key_map", start)
    for i, key in enumerate(keys):
        validate_key(key, f"key_map[{i}]")

    events = score.get("events")
    if not isinstance(events, list):
        raise ContractError("events", "应为数组")
    seen: set[str] = set()
    cursor = start
    tuplets: dict[str, list[dict]] = {}
    for i, event in enumerate(events):
        field = f"events[{i}]"
        if not isinstance(event, dict):
            raise ContractError(field, "应为对象")
        eid = event.get("id")
        if not isinstance(eid, str) or not eid or eid in seen:
            raise ContractError(field + ".id", "ID 必须非空且唯一")
        seen.add(eid)
        kind = event.get("kind")
        if kind not in ("note", "rest", "unknown"):
            raise ContractError(field + ".kind", "应为 note/rest/unknown")
        at = read_q(event.get("start_q"), field + ".start_q")
        duration = read_q(event.get("duration_q"), field + ".duration_q")
        if duration <= 0:
            raise ContractError(field + ".duration_q", "时值必须大于 0")
        if at != cursor:
            reason = "重叠或顺序错误" if at < cursor else "有未显式标记的空白；请插入 rest/unknown"
            raise ContractError(field + ".start_q", reason + f"，期望 {str_q(cursor)}")
        cursor = at + duration
        if kind == "note":
            pitch = integer(event.get("pitch_midi"), field + ".pitch_midi", 0, 127)
            if not isinstance(event.get("phrase_id"), str) or not event["phrase_id"]:
                raise ContractError(field + ".phrase_id", "音符必须属于乐句")
            if event.get("spelling_hint") is not None and midi_spelling(event["spelling_hint"]) != pitch:
                raise ContractError(field + ".spelling_hint", "与 pitch_midi 不一致")
            if event.get("reason") is not None:
                raise ContractError(field + ".reason", "音符不使用休止/未知原因")
            if "review_required" in event and type(event["review_required"]) is not bool:
                raise ContractError(field + ".review_required", "应为布尔值")
            if event.get("review_required") and event.get("inference_method") != "candidate_or_short_gap":
                raise ContractError(field + ".inference_method", "推测音需说明补全方法")
        else:
            if "pitch_midi" in event or "spelling_hint" in event:
                raise ContractError(field, "休止/未知不能携带虚构音高")
            if not isinstance(event.get("reason"), str) or not event["reason"]:
                raise ContractError(field + ".reason", "休止/未知需要原因")
        if event.get("observed") is not None:
            observed = event["observed"]
            if not isinstance(observed, dict):
                raise ContractError(field + ".observed", "应为对象")
            beginning = number(observed.get("start_sec"), field + ".observed.start_sec")
            ending = number(observed.get("end_sec"), field + ".observed.end_sec")
            if beginning < 0 or ending <= beginning:
                raise ContractError(field + ".observed", "应为非负、左闭右开绝对秒区间")
        for id_field in ("tuplet_group_id", "tie_group_id"):
            if event.get(id_field) is not None and (not isinstance(event[id_field], str) or not event[id_field]):
                raise ContractError(field + "." + id_field, "组 ID 应为非空字符串")
        if kind != "note" and event.get("tie_group_id"):
            raise ContractError(field + ".tie_group_id", "休止/未知不允许 tie")
        if event.get("tuplet_group_id"):
            tuplets.setdefault(event["tuplet_group_id"], []).append(event)
    if cursor != end:
        raise ContractError("score_end_q", f"事件只覆盖到 {str_q(cursor)}；末段请显式标记")
    for group_id, group in tuplets.items():
        if len(group) != 3 or any(read_q(e["duration_q"], "tuplet.duration_q") != Fraction(1, 3) for e in group):
            raise ContractError("tuplet_group_id", f"{group_id} 当前只支持三个等长的 1/3q 元素")
        if any(read_q(group[i+1]["start_q"], "tuplet.start_q") != read_q(group[i]["start_q"], "tuplet.start_q") + Fraction(1, 3) for i in range(2)):
            raise ContractError("tuplet_group_id", f"{group_id} 成员必须连续")
    if not isinstance(score.get("changes"), list):
        raise ContractError("changes", "应为数组")
    return score
