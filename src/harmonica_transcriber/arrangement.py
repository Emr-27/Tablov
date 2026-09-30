from copy import deepcopy

from .errors import ContractError
from .harmonica import assign_fingerings
from .rationals import integer, read_q
from .theory import transpose_key
from .time_map import q_to_sec


def with_performed_times(score: dict) -> dict:
    result = deepcopy(score)
    for event in result["events"]:
        q = read_q(event["start_q"], "event.start_q")
        end = q + read_q(event["duration_q"], "event.duration_q")
        event["performed"] = {"start_sec": q_to_sec(score["time_map"], q),
                              "end_sec": q_to_sec(score["time_map"], end)}
    return result


def arrange(score: dict, profile: dict, transpose: int = 0) -> dict:
    integer(transpose, "--transpose", -127, 127)
    key_map = [transpose_key(key, transpose) for key in score["key_map"]]
    events = []
    changes = []
    for source in score["events"]:
        event = {"id": source["id"], "kind": source["kind"], "start_q": source["start_q"],
                 "duration_q": source["duration_q"], "phrase_id": source.get("phrase_id"),
                 "source_event_id": source["id"]}
        if source["kind"] == "note":
            pitch = source["pitch_midi"] + transpose
            if not 0 <= pitch <= 127:
                raise ContractError("--transpose", f"事件 {source['id']} 移调后超出 MIDI 0–127")
            event.update(source_pitch_midi=source["pitch_midi"], played_pitch_midi=pitch,
                         phrase_octave_shift=0)
            if transpose:
                changes.append({"action": "global_transpose", "event_id": source["id"],
                                "before": source["pitch_midi"], "after": pitch,
                                "reason": "user_requested", "rule_version": "phase0-v1"})
        else:
            event["reason"] = source["reason"]
        q = read_q(source["start_q"], "event.start_q")
        end = q + read_q(source["duration_q"], "event.duration_q")
        event["performed"] = {"start_sec": q_to_sec(score["time_map"], q),
                              "end_sec": q_to_sec(score["time_map"], end)}
        events.append(event)
    cost = assign_fingerings(events, profile, score["time_map"])
    return {"schema_version": "2.0", "revision_id": score["revision_id"] + f"-arr-T{transpose:+d}",
            "source_revision_id": score["revision_id"], "global_transpose": transpose,
            "octave_policy": "reject", "key_map": key_map, "profile_id": profile["profile_id"],
            "profile_version": profile["profile_version"], "events": events, "changes": changes,
            "cost": cost}

