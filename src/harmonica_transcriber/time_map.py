"""四分音符有理数位置与音频绝对秒的单一映射。"""
from fractions import Fraction

from .errors import ContractError
from .rationals import number, read_q


def _range(time_map: dict, score_start: Fraction, score_end: Fraction):
    origin = read_q(time_map.get("origin_q"), "time_map.origin_q")
    origin_sec = number(time_map.get("origin_sec"), "time_map.origin_sec")
    return origin, origin_sec, min(origin, score_start), max(origin, score_end)


def validate_time_map(time_map: dict, score_start: Fraction, score_end: Fraction) -> None:
    if not isinstance(time_map, dict):
        raise ContractError("time_map", "应为时间映射对象")
    origin, origin_sec, lower, upper = _range(time_map, score_start, score_end)
    kind = time_map.get("kind")
    if kind == "tempo":
        events = time_map.get("tempo_events")
        if not isinstance(events, list) or not events:
            raise ContractError("time_map.tempo_events", "至少需要一个速度事件")
        previous = None
        for i, event in enumerate(events):
            if not isinstance(event, dict):
                raise ContractError(f"time_map.tempo_events[{i}]", "应为对象")
            q = read_q(event.get("at_q"), f"time_map.tempo_events[{i}].at_q")
            number(event.get("bpm_q"), f"time_map.tempo_events[{i}].bpm_q", positive=True)
            if previous is not None and q <= previous:
                raise ContractError("time_map.tempo_events", "速度事件位置必须严格递增")
            previous = q
        if read_q(events[0]["at_q"], "time_map.tempo_events[0].at_q") > lower:
            raise ContractError("time_map.tempo_events[0]", "首速度未覆盖时间图下界")
    elif kind == "anchors":
        anchors = time_map.get("anchors")
        if not isinstance(anchors, list) or len(anchors) < 2:
            raise ContractError("time_map.anchors", "至少需要两个锚点")
        previous_q = previous_sec = None
        for i, anchor in enumerate(anchors):
            if not isinstance(anchor, dict):
                raise ContractError(f"time_map.anchors[{i}]", "应为对象")
            q = read_q(anchor.get("q"), f"time_map.anchors[{i}].q")
            sec = number(anchor.get("sec"), f"time_map.anchors[{i}].sec")
            if previous_q is not None and (q <= previous_q or sec <= previous_sec):
                raise ContractError("time_map.anchors", "q 和 sec 必须严格递增")
            previous_q, previous_sec = q, sec
        if read_q(anchors[0]["q"], "time_map.anchors[0].q") > lower or previous_q < upper:
            raise ContractError("time_map.anchors", "锚点必须覆盖包含 origin 的完整乐谱范围")
        if abs(q_to_sec(time_map, origin) - origin_sec) > 1e-7:
            raise ContractError("time_map.origin_sec", "声明的原点与锚点插值不一致")
    else:
        raise ContractError("time_map.kind", "仅支持 tempo 或 anchors")


def q_to_sec(time_map: dict, q: Fraction) -> float:
    origin = read_q(time_map["origin_q"], "time_map.origin_q")
    origin_sec = float(time_map["origin_sec"])
    if time_map["kind"] == "anchors":
        anchors = [(read_q(a["q"], "anchor.q"), float(a["sec"])) for a in time_map["anchors"]]
        if not anchors[0][0] <= q <= anchors[-1][0]:
            raise ContractError("time_map", "请求位置在锚点覆盖范围外")
        for (left_q, left_s), (right_q, right_s) in zip(anchors, anchors[1:]):
            if left_q <= q <= right_q:
                return left_s + float((q - left_q) / (right_q - left_q)) * (right_s - left_s)
    if q == origin:
        return origin_sec
    left, right = sorted((origin, q))
    events = [(read_q(e["at_q"], "tempo.at_q"), float(e["bpm_q"])) for e in time_map["tempo_events"]]
    if left < events[0][0]:
        raise ContractError("time_map", "请求位置在速度图覆盖范围外")
    cuts = [left] + [at for at, _ in events if left < at < right] + [right]
    seconds = 0.0
    for start, end in zip(cuts, cuts[1:]):
        bpm = next(bpm for at, bpm in reversed(events) if at <= start)
        seconds += float(end - start) * 60.0 / bpm
    return origin_sec + (seconds if q >= origin else -seconds)


def sec_to_q(time_map: dict, sec: float, low: Fraction, high: Fraction) -> Fraction:
    """仅用于诊断；导入 MIDI 总是直接读取精确 tick。"""
    lo, hi = low, high
    if not q_to_sec(time_map, lo) <= sec <= q_to_sec(time_map, hi):
        raise ContractError("time_map", "请求秒数在映射范围外")
    for _ in range(72):
        mid = (lo + hi) / 2
        if q_to_sec(time_map, mid) < sec:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def bpm_at(time_map: dict, q: Fraction) -> float:
    if time_map["kind"] == "anchors":
        anchors = [(read_q(a["q"], "anchor.q"), float(a["sec"])) for a in time_map["anchors"]]
        for (left_q, left_s), (right_q, right_s) in zip(anchors, anchors[1:]):
            if left_q <= q < right_q or q == anchors[-1][0] == right_q:
                return 60.0 * float(right_q - left_q) / (right_s - left_s)
        raise ContractError("time_map", "位置在锚点外")
    events = [(read_q(e["at_q"], "tempo.at_q"), float(e["bpm_q"])) for e in time_map["tempo_events"]]
    for at, bpm in reversed(events):
        if at <= q:
            return bpm
    raise ContractError("time_map", "速度未覆盖位置")
