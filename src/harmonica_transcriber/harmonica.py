"""正向物理音表、反向候选及同一成本目标上的指法 DP。"""
from dataclasses import dataclass
from fractions import Fraction
from hashlib import sha256
import json
import math

from .errors import ContractError
from .rationals import integer, read_q
from .time_map import q_to_sec

POSITIONS = ("blow_out", "draw_out", "blow_in", "draw_in")
COST_VERSION = "dp-v1"


@dataclass(frozen=True, order=True)
class Fingering:
    hole: int
    breath: int  # 0 blow, 1 draw
    slide: int   # 0 out, 1 in

    @property
    def label(self) -> str:
        return f"{self.hole}{'B' if self.breath == 0 else 'D'}{'+' if self.slide else ''}"

    def as_dict(self) -> dict:
        return {"hole": self.hole, "breath": "blow" if self.breath == 0 else "draw",
                "slide": "in" if self.slide else "out", "tab": self.label}


def validate_profile(profile: object) -> dict:
    if not isinstance(profile, dict):
        raise ContractError("profile", "应为对象")
    for field in ("profile_id", "key", "tuning", "scope_note"):
        if not isinstance(profile.get(field), str) or not profile[field]:
            raise ContractError("profile." + field, "必须是非空字符串")
    integer(profile.get("profile_version"), "profile.profile_version", 1, 9999)
    count = integer(profile.get("hole_count"), "profile.hole_count", 1, 24)
    if profile.get("position_order") != list(POSITIONS):
        raise ContractError("profile.position_order", "必须为 blow_out/draw_out/blow_in/draw_in")
    holes = profile.get("holes")
    if not isinstance(holes, dict) or set(holes) != {str(i) for i in range(1, count + 1)}:
        raise ContractError("profile.holes", "必须逐孔给出完整四位置映射")
    for hole in range(1, count + 1):
        row = holes[str(hole)]
        if not isinstance(row, list) or len(row) != 4:
            raise ContractError(f"profile.holes.{hole}", "每孔必须有四个位置")
        for i, pitch in enumerate(row):
            if pitch is not None:
                integer(pitch, f"profile.holes.{hole}[{i}]", 0, 127)
    if profile.get("reference_a_hz") is not None:
        from .rationals import number
        number(profile["reference_a_hz"], "profile.reference_a_hz", positive=True)
    if not isinstance(profile.get("sources"), list) or not profile["sources"]:
        raise ContractError("profile.sources", "必须登记资料来源")
    if profile.get("top_draw_slide_verified") is False and holes.get("12", [None]*4)[3] is not None and count == 12:
        raise ContractError("profile.top_draw_slide_verified", "未验证时最高按键吸不得启用")
    return profile


def profile_hash(profile: dict) -> str:
    return sha256(json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def pitch_for(profile: dict, fingering: Fingering) -> int | None:
    if not 1 <= fingering.hole <= profile["hole_count"] or fingering.breath not in (0, 1) or fingering.slide not in (0, 1):
        raise ContractError("fingering", "孔位、吹吸或按键无效")
    return profile["holes"][str(fingering.hole)][fingering.slide * 2 + fingering.breath]


def candidates(profile: dict, pitch: int) -> list[Fingering]:
    result = []
    for hole in range(1, profile["hole_count"] + 1):
        for breath in (0, 1):
            for slide in (0, 1):
                f = Fingering(hole, breath, slide)
                if pitch_for(profile, f) == pitch:
                    result.append(f)
    return result


def transition(previous: Fingering, current: Fingering, prev_event: dict, event: dict, time_map: dict) -> dict:
    distance = abs(previous.hole - current.hole)
    hole_cost = float(distance)
    breath_cost = 0.25 if previous.breath != current.breath else 0.0
    slide_cost = 0.40 if previous.slide != current.slide else 0.0
    leap_cost = 0.80 * max(0, distance - 2)
    base = hole_cost + breath_cost + slide_cost + leap_cost
    prev_start = read_q(prev_event["start_q"], "event.start_q")
    prev_end = prev_start + read_q(prev_event["duration_q"], "event.duration_q")
    this_start = read_q(event["start_q"], "event.start_q")
    ioi = q_to_sec(time_map, this_start) - q_to_sec(time_map, prev_start)
    gap = max(0.0, q_to_sec(time_map, this_start) - q_to_sec(time_map, prev_end))
    if event.get("phrase_id") != prev_event.get("phrase_id") or gap >= 0.5:
        return {"hole": hole_cost, "breath": breath_cost, "slide": slide_cost, "leap": leap_cost,
                "speed": 1.0, "relief": 0.0, "reset": True, "total": 0.0}
    speed = min(4.0, max(1.0, 0.25 / max(ioi, 0.02)))
    relief = math.exp(-gap / 0.25)
    return {"hole": hole_cost, "breath": breath_cost, "slide": slide_cost, "leap": leap_cost,
            "speed": speed, "relief": relief, "reset": False, "total": base * speed * relief}


def _better(left: tuple, right: tuple | None) -> bool:
    if right is None:
        return True
    if left[0] < right[0] - 1e-10:
        return True
    if abs(left[0] - right[0]) <= 1e-10 and left[1] < right[1]:
        return True
    return False


def optimize(notes: list[dict], profile: dict, time_map: dict) -> tuple[list[Fingering], dict]:
    """只接受连续的、均有合法候选的逻辑音符。"""
    if not notes:
        return [], {"total": 0.0, "edges": [], "cost_version": COST_VERSION}
    candidate_rows = [candidates(profile, note["played_pitch_midi"]) for note in notes]
    if any(not row for row in candidate_rows):
        raise ContractError("optimizer", "传入不可演奏音符；应先分段")
    states: dict[Fingering, tuple[float, tuple[Fingering, ...]]] = {f: (0.0, (f,)) for f in candidate_rows[0]}
    for i in range(1, len(notes)):
        current_states = {}
        for f in candidate_rows[i]:
            best = None
            for prev, (score, path) in states.items():
                edge = transition(prev, f, notes[i-1], notes[i], time_map)
                proposal = (score + edge["total"], path + (f,))
                if _better(proposal, best):
                    best = proposal
            current_states[f] = best
        states = current_states
    best = None
    for candidate in states.values():
        if _better(candidate, best):
            best = candidate
    assert best is not None
    path = list(best[1])
    edges = [transition(path[i-1], path[i], notes[i-1], notes[i], time_map) for i in range(1, len(notes))]
    return path, {"total": sum(edge["total"] for edge in edges), "edges": edges,
                  "cost_version": COST_VERSION}


def assign_fingerings(events: list[dict], profile: dict, time_map: dict) -> dict:
    solved = []
    costs = []

    def flush():
        if not solved:
            return
        path, detail = optimize(solved, profile, time_map)
        for event, f in zip(solved, path):
            event["fingering"] = f.as_dict()
        costs.append({"event_ids": [e["id"] for e in solved], **detail})
        solved.clear()

    for event in events:
        if event["kind"] == "unknown":
            flush()
        elif event["kind"] == "note":
            if not candidates(profile, event["played_pitch_midi"]):
                flush()
                event["fingering"] = None
                event["playable"] = False
                event["failure_reason"] = "PITCH_UNAVAILABLE_IN_PROFILE"
            else:
                event["playable"] = True
                solved.append(event)
        # Confirmed rests remain between adjacent notes; gap relief comes from seconds.
    flush()
    unknown = any(e["kind"] == "unknown" for e in events)
    unplayable = any(e.get("playable") is False for e in events)
    return {"cost_version": COST_VERSION, "profile_sha256": profile_hash(profile), "subsequences": costs,
            "full_cost_defined": not (unknown or unplayable),
            "total": None if unknown or unplayable else sum(part["total"] for part in costs)}
