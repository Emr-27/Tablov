"""受限 SMF type 0/1 读写。旁车文件在哈希匹配时恢复 MIDI 无法表达的语义。"""
from fractions import Fraction
from hashlib import sha256
import json
import math
from pathlib import Path
import struct

from .errors import ContractError
from .models import validate_score
from .rationals import integer, read_q, str_q
from .theory import MAJOR_SIG, MINOR_SIG, default_do, key_from_text, validate_key
from .time_map import bpm_at, q_to_sec


def _vlq(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for _ in range(4):
        if offset >= len(data):
            raise ContractError("MIDI", "可变长整数截断")
        byte = data[offset]
        offset += 1
        value = (value << 7) | (byte & 0x7f)
        if byte < 128:
            return value, offset
    raise ContractError("MIDI", "不支持超过四字节的可变长整数")


def _write_vlq(value: int) -> bytes:
    if not 0 <= value < 0x10000000:
        raise ContractError("MIDI", "delta tick 超出四字节范围")
    result = [value & 0x7f]
    value >>= 7
    while value:
        result.append((value & 0x7f) | 0x80)
        value >>= 7
    return bytes(reversed(result))


def _read_tracks(content: bytes) -> tuple[int, list[list[dict]]]:
    if content[:4] != b"MThd" or len(content) < 14:
        raise ContractError("MIDI", "缺少有效 MThd")
    header_len = struct.unpack_from(">I", content, 4)[0]
    if header_len < 6 or len(content) < 8 + header_len:
        raise ContractError("MIDI", "头部截断")
    kind, ntracks, division = struct.unpack_from(">HHH", content, 8)
    if kind not in (0, 1) or division & 0x8000 or division == 0:
        raise ContractError("MIDI", "只支持 PPQ type 0/1，不支持 type 2/SMPTE")
    if ntracks < 1 or (kind == 0 and ntracks != 1):
        raise ContractError("MIDI", "轨道数与文件类型不一致")
    pos = 8 + header_len
    tracks = []
    for track_id in range(ntracks):
        if content[pos:pos+4] != b"MTrk" or pos + 8 > len(content):
            raise ContractError("MIDI", f"轨道 {track_id} 缺失")
        length = struct.unpack_from(">I", content, pos+4)[0]
        pos += 8
        end = pos + length
        if end > len(content):
            raise ContractError("MIDI", f"轨道 {track_id} 截断")
        tick = 0
        running = None
        messages = []
        while pos < end:
            delta, pos = _vlq(content, pos)
            tick += delta
            if pos >= end:
                raise ContractError("MIDI", "事件截断")
            status = content[pos]
            if status & 0x80:
                pos += 1
                if status < 0xF0:
                    running = status
                else:
                    running = None
            else:
                if running is None:
                    raise ContractError("MIDI", "running status 缺少状态字节")
                status = running
            if status == 0xFF:
                if pos >= end:
                    raise ContractError("MIDI", "meta 事件截断")
                subtype = content[pos]
                pos += 1
                size, pos = _vlq(content, pos)
                if pos + size > end:
                    raise ContractError("MIDI", "meta 事件长度错误")
                body = content[pos:pos+size]
                pos += size
                messages.append({"tick": tick, "kind": "meta", "subtype": subtype, "data": body,
                                 "track": track_id})
            elif status in (0xF0, 0xF7):
                size, pos = _vlq(content, pos)
                if pos + size > end:
                    raise ContractError("MIDI", "SysEx 事件截断")
                pos += size
            elif 0x80 <= status <= 0xEF:
                high = status & 0xF0
                nbytes = 1 if high in (0xC0, 0xD0) else 2
                if pos + nbytes > end:
                    raise ContractError("MIDI", "通道事件截断")
                body = content[pos:pos+nbytes]
                if any(byte & 0x80 for byte in body):
                    raise ContractError("MIDI", "通道数据字节无效")
                pos += nbytes
                messages.append({"tick": tick, "kind": "channel", "type": high,
                                 "channel": status & 0x0f, "data": body, "track": track_id})
            else:
                raise ContractError("MIDI", f"不支持系统事件 0x{status:02x}")
        tracks.append(messages)
    if pos != len(content):
        raise ContractError("MIDI", "轨道后存在额外字节")
    return division, tracks


def _global_events(tracks: list[list[dict]], subtype: int, size: int) -> list[dict]:
    unique = {}
    for messages in tracks:
        for message in messages:
            if message["kind"] != "meta" or message["subtype"] != subtype:
                continue
            if len(message["data"]) != size:
                raise ContractError("MIDI", f"meta 0x{subtype:02x} 长度错误")
            previous = unique.get(message["tick"])
            if previous is not None and previous != message["data"]:
                raise ContractError("MIDI", f"tick {message['tick']} 有冲突的全局元事件")
            unique[message["tick"]] = message["data"]
    return [{"tick": tick, "data": value} for tick, value in sorted(unique.items())]


def _key_signature(data: bytes) -> tuple[str, str]:
    signature = struct.unpack("b", data[:1])[0]
    mode = "minor" if data[1] == 1 else "major" if data[1] == 0 else None
    table = MINOR_SIG if mode == "minor" else MAJOR_SIG
    if mode is None or not -7 <= signature <= 7:
        raise ContractError("MIDI.key_signature", "调号超出传统范围")
    return next(name for name, count in table.items() if count == signature), mode


def _input_meta_maps(tracks, ppq, *, key_text=None, do_midi=None):
    tempo = _global_events(tracks, 0x51, 3)
    meters = _global_events(tracks, 0x58, 4)
    keys = _global_events(tracks, 0x59, 2)
    if not tempo or tempo[0]["tick"] > 0:
        tempo.insert(0, {"tick": 0, "data": (500000).to_bytes(3, "big"), "assumed": True})
    if not meters or meters[0]["tick"] > 0:
        meters.insert(0, {"tick": 0, "data": bytes((4, 2, 24, 8)), "assumed": True})
    for item in tempo:
        if not int.from_bytes(item["data"], "big"):
            raise ContractError("MIDI.tempo", "微秒每拍必须大于 0")
    tempo_map = [{"at_q": str_q(Fraction(item["tick"], ppq)),
                  "bpm_q": 60000000 / int.from_bytes(item["data"], "big")}
                 for item in tempo]
    meter_map = []
    for item in meters:
        numerator, denominator_exp = item["data"][0], item["data"][1]
        if denominator_exp > 6:
            raise ContractError("MIDI.meter", "本阶段不支持该拍号分母")
        meter_map.append({"at_q": str_q(Fraction(item["tick"], ppq)), "numerator": numerator,
                          "denominator": 2 ** denominator_exp,
                          "source": "assumed" if item.get("assumed") else "midi"})
    key_map = []
    if keys:
        for item in keys:
            name, mode = _key_signature(item["data"])
            value = key_from_text(name + " " + mode, source="midi")
            value["at_q"] = str_q(Fraction(item["tick"], ppq))
            key_map.append(value)
    if key_text:
        provided = key_from_text(key_text, do_midi, source="user")
        if key_map and key_map[0]["at_q"] == "0" and (key_map[0]["tonic_spelling"], key_map[0]["mode"]) != (provided["tonic_spelling"], provided["mode"]):
            raise ContractError("--key", "与 MIDI 首调号冲突；请先修改 MIDI 或去掉该参数")
        if not key_map or key_map[0]["at_q"] != "0":
            key_map.insert(0, provided)
        elif do_midi is not None:
            key_map[0]["do_midi"] = do_midi
            key_map[0]["source"] = "user"
    elif not key_map or key_map[0]["at_q"] != "0":
        value = key_from_text("C major", do_midi, source="assumed")
        key_map.insert(0, value)
    elif do_midi is not None:
        key_map[0]["do_midi"] = do_midi
        key_map[0]["source"] = "user"
    for i, key in enumerate(key_map):
        validate_key(key, f"key_map[{i}]")
    return tempo_map, meter_map, key_map


def import_midi(path: Path, *, track: int | None = None, channel: int | None = None,
                key_text: str | None = None, do_midi: int | None = None) -> tuple[dict, list[str]]:
    content = path.read_bytes()
    ppq, tracks = _read_tracks(content)
    warnings = []
    candidates = [i for i, messages in enumerate(tracks) if any(m["kind"] == "channel" and m["type"] == 0x90 and m["data"][1] > 0 and m["channel"] != 9 for m in messages)]
    if track is None:
        if len(candidates) != 1:
            raise ContractError("--track", "无法自动确定唯一非鼓旋律轨；请指定 --track")
        track = candidates[0]
    integer(track, "--track", 0, len(tracks)-1)
    messages = tracks[track]
    channels = {m["channel"] for m in messages if m["kind"] == "channel" and m["type"] == 0x90 and m["data"][1] > 0}
    if channel is None:
        if len(channels) != 1 or 9 in channels:
            raise ContractError("--channel", "所选轨没有唯一非鼓音符通道；请指定 --channel")
        channel = next(iter(channels))
    integer(channel, "--channel", 0, 15)
    selected = [m for m in messages if m["kind"] == "channel" and m["channel"] == channel]
    if not selected:
        raise ContractError("--channel", "所选通道没有事件")
    active = {}
    notes = []
    for message in selected:
        tick, kind, body = message["tick"], message["type"], message["data"]
        if kind == 0xE0 and (body[0] | body[1] << 7) != 8192:
            raise ContractError("MIDI.pitch_bend", f"tick {tick} 有非零弯音")
        if kind == 0xB0 and body[0] in (64, 65, 66) and body[1] >= 64:
            raise ContractError("MIDI.controller", f"tick {tick} 的踏板/滑音控制影响音符")
        if kind == 0x90 and body[1] > 0:
            if body[0] in active:
                raise ContractError("MIDI.note_on", f"tick {tick} 同音重叠")
            active[body[0]] = tick
        elif kind == 0x80 or (kind == 0x90 and body[1] == 0):
            if body[0] not in active:
                raise ContractError("MIDI.note_off", f"tick {tick} 无配对起音")
            beginning = active.pop(body[0])
            if tick <= beginning:
                raise ContractError("MIDI.note_off", "音符时值必须大于零")
            notes.append((beginning, tick, body[0]))
    if active:
        raise ContractError("MIDI.note_on", "有未配对的音符起音")
    if not notes:
        raise ContractError("MIDI", "所选轨/通道没有音符")
    notes.sort(key=lambda n: (n[0], n[1], n[2]))
    for previous, current in zip(notes, notes[1:]):
        if current[0] < previous[1]:
            raise ContractError("MIDI.polyphony", f"tick {current[0]} 与前音重叠")

    unresolved = []
    for message in messages:
        if message["kind"] == "meta" and message["subtype"] == 0x01 and message["data"].startswith(b"UNRESOLVED"):
            try:
                prefix, event_id, _, duration_text = message["data"].decode("utf-8").rsplit(":", 3)
                if prefix != "UNRESOLVED":
                    raise ValueError("marker prefix")
                duration = read_q(duration_text, "MIDI.UNRESOLVED.duration_q")
                if duration <= 0:
                    raise ValueError("marker duration")
            except (UnicodeError, ValueError, ContractError) as exc:
                raise ContractError("MIDI.UNRESOLVED", "待核对标记损坏；不能当作确认休止") from exc
            unresolved.append((Fraction(message["tick"], ppq), duration, event_id))

    sidecar_path = Path(str(path) + ".json")
    if sidecar_path.exists():
        try:
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            if sidecar.get("midi_sha256") == sha256(content).hexdigest() and not (key_text or do_midi is not None):
                stored = validate_score(sidecar["score"])
                expected = [(int((read_q(e["start_q"], "event.start_q") - read_q(sidecar["q_base"], "q_base")) * ppq) + sidecar["padding_ticks"],
                             int((read_q(e["start_q"], "event.start_q") + read_q(e["duration_q"], "event.duration_q") - read_q(sidecar["q_base"], "q_base")) * ppq) + sidecar["padding_ticks"],
                             e["pitch_midi"]) for e in stored["events"] if e["kind"] == "note"]
                if expected == notes:
                    return stored, ["已用哈希匹配的 MIDI 旁车文件恢复弱起、未知区段及精确 Score 元数据"]
                warnings.append("旁车文件与 MIDI 音符不一致，改按 MIDI 本体导入")
            else:
                warnings.append("旁车文件哈希不匹配或用户指定了调性，按 MIDI 本体导入")
        except (ValueError, KeyError, TypeError, ContractError) as exc:
            warnings.append(f"旁车文件无效，按 MIDI 本体导入：{exc}")

    tempo_events, meter_map, key_map = _input_meta_maps(tracks, ppq, key_text=key_text, do_midi=do_midi)
    score_end = max([Fraction(max(end for _, end, _ in notes), ppq)] + [at + duration for at, duration, _ in unresolved])
    entries = []
    cursor = Fraction(0)
    units = [(Fraction(beginning, ppq), Fraction(ending, ppq), "note", pitch, f"n{i+1}")
             for i, (beginning, ending, pitch) in enumerate(notes)]
    units += [(at, at + duration, "unknown", None, f"unknown-{i+1}-{event_id}")
              for i, (at, duration, event_id) in enumerate(unresolved)]
    units.sort(key=lambda part: (part[0], part[1], part[2]))
    for i, (at, end, kind, pitch, event_id) in enumerate(units):
        if at < cursor:
            raise ContractError("MIDI.UNRESOLVED", "待核对区段与音符或其他区段重叠")
        if at > cursor:
            entries.append({"id": f"r{i+1}", "kind": "rest", "start_q": str_q(cursor),
                            "duration_q": str_q(at-cursor), "reason": "midi_note_gap"})
        if kind == "note":
            entries.append({"id": event_id, "kind": "note", "pitch_midi": pitch,
                            "start_q": str_q(at), "duration_q": str_q(end-at),
                            "phrase_id": "p1", "spelling_hint": None})
        else:
            entries.append({"id": event_id, "kind": "unknown", "start_q": str_q(at),
                            "duration_q": str_q(end-at), "reason": "midi_unresolved_marker"})
        cursor = end
    score = {"schema_version": "2.0", "revision_id": path.stem + "-midi-import-r1",
             "source": {"kind": "midi", "name": path.name, "track": track, "channel": channel, "ppq": ppq},
             "meter_alignment_status": "unconfirmed",
             "score_start_q": "0", "score_end_q": str_q(score_end),
             "time_map": {"kind": "tempo", "origin_q": "0", "origin_sec": 0.0,
                          "tempo_events": tempo_events}, "meter_map": meter_map,
             "first_full_bar_q": "0", "key_map": key_map, "events": entries, "changes": []}
    if not key_text and all(item["source"] == "assumed" for item in key_map):
        warnings.append("MIDI 无调号：临时采用 1=C 记谱参考，未检测出 C 大调")
    warnings.append("无匹配旁车文件：按 MIDI tick 从 q=0 起谱；原弱起与音频零点无法恢复，unknown 仅从有效 UNRESOLVED 标记恢复")
    return validate_score(score), warnings


def _meta(kind: int, body: bytes) -> bytes:
    return bytes((0xff, kind)) + _write_vlq(len(body)) + body


def _track(events: list[tuple[int, int, bytes]], end_tick: int) -> bytes:
    events = sorted(events, key=lambda item: (item[0], item[1]))
    if events and events[-1][0] > end_tick:
        end_tick = events[-1][0]
    events.append((end_tick, 99, _meta(0x2f, b"")))
    out = bytearray()
    previous = 0
    for tick, _, body in events:
        if tick < previous:
            raise ContractError("MIDI", "事件顺序错误")
        out.extend(_write_vlq(tick-previous))
        out.extend(body)
        previous = tick
    return b"MTrk" + struct.pack(">I", len(out)) + out


def _ppq(score: dict) -> int:
    start = read_q(score["score_start_q"], "score_start_q")
    q_values = [read_q(score["score_end_q"], "score_end_q")]
    for event in score["events"]:
        q = read_q(event["start_q"], "event.start_q")
        q_values += [q, q + read_q(event["duration_q"], "event.duration_q")]
    q_values += [read_q(e["at_q"], "map.at_q") for e in score["meter_map"] + score["key_map"] if read_q(e["at_q"], "map.at_q") >= start]
    if score["time_map"]["kind"] == "anchors":
        q_values += [read_q(e["q"], "anchor.q") for e in score["time_map"]["anchors"] if read_q(e["q"], "anchor.q") >= start]
    else:
        q_values += [read_q(e["at_q"], "tempo.at_q") for e in score["time_map"]["tempo_events"] if read_q(e["at_q"], "tempo.at_q") >= start]
    needed = math.lcm(*((value - start).denominator for value in q_values))
    preferred = math.lcm(960, needed)
    result = preferred if preferred <= 32767 else needed
    if result > 32767:
        raise ContractError("MIDI.PPQ", "精确时值所需 PPQ 超出标准范围")
    return result


def _tempo_points(time_map: dict, start: Fraction, end: Fraction) -> list[tuple[Fraction, float]]:
    points = [(start, bpm_at(time_map, start))]
    if time_map["kind"] == "tempo":
        points += [(q, float(e["bpm_q"])) for e in time_map["tempo_events"]
                   if start < (q := read_q(e["at_q"], "tempo.at_q")) < end]
    else:
        points += [(q, bpm_at(time_map, q)) for e in time_map["anchors"]
                   if start < (q := read_q(e["q"], "anchor.q")) < end]
    return points


def export_midi(score: dict, *, midi_origin: str = "score-start") -> tuple[bytes, dict]:
    if midi_origin not in ("score-start", "audio"):
        raise ContractError("--midi-origin", "应为 score-start/audio")
    start = read_q(score["score_start_q"], "score_start_q")
    end = read_q(score["score_end_q"], "score_end_q")
    ppq = _ppq(score)
    initial_bpm = bpm_at(score["time_map"], start)
    source_start_sec = q_to_sec(score["time_map"], start)
    if midi_origin == "audio" and source_start_sec < -1e-9:
        raise ContractError("--midi-origin", "谱面起点早于音频零点，无法生成非负前导")
    padding = round(max(0.0, source_start_sec) * initial_bpm / 60 * ppq) if midi_origin == "audio" else 0

    def tick(q: Fraction) -> int:
        relative = (q-start) * ppq
        if relative.denominator != 1:
            raise ContractError("MIDI.PPQ", "无法精确写入谱面位置")
        result = int(relative) + padding
        if not 0 <= result <= 0x0fffffff:
            raise ContractError("MIDI.tick", "事件超出 SMF delta 表示范围")
        return result

    metadata = []
    tempo_errors = []
    for at, bpm in _tempo_points(score["time_map"], start, end):
        micros = round(60000000 / bpm)
        if not 1 <= micros <= 0xffffff:
            raise ContractError("MIDI.tempo", "速度超出 MIDI 微秒每拍范围")
        when = 0 if at == start else tick(at)
        metadata.append((when, 0, _meta(0x51, micros.to_bytes(3, "big"))))
        tempo_errors.append({"at_q": str_q(at), "seconds_per_q_error": micros / 1e6 - 60 / bpm})
        if at == start and padding:
            metadata.append((padding, 0, _meta(0x51, micros.to_bytes(3, "big"))))
    for field, subtype in (("meter_map", 0x58), ("key_map", 0x59)):
        entries = score[field]
        active = entries[0]
        for item in entries:
            if read_q(item["at_q"], field + ".at_q") <= start:
                active = item
            else:
                break
        relevant = [active] + [item for item in entries if start < read_q(item["at_q"], field + ".at_q") < end]
        for index, item in enumerate(relevant):
            at = start if index == 0 else read_q(item["at_q"], field + ".at_q")
            when = tick(at) if index else 0
            if field == "meter_map":
                denominator = item["denominator"]
                body = bytes((item["numerator"], denominator.bit_length()-1, 24, 8))
            else:
                table = MAJOR_SIG if item["mode"] == "major" else MINOR_SIG
                body = struct.pack("bB", table[item["tonic_spelling"]], 1 if item["mode"] == "minor" else 0)
            metadata.append((when, 1, _meta(subtype, body)))

    notes = []
    for event in score["events"]:
        at = read_q(event["start_q"], "event.start_q")
        end_q = at + read_q(event["duration_q"], "event.duration_q")
        if event["kind"] == "note":
            pitch = event["pitch_midi"]
            notes.append((tick(at), 20, bytes((0x90, pitch, 80))))
            notes.append((tick(end_q), 10, bytes((0x80, pitch, 0))))
        elif event["kind"] == "unknown":
            label = f"UNRESOLVED:{event['id']}:{str_q(at)}:{str_q(end_q-at)}".encode("utf-8")
            notes.append((tick(at), 30, _meta(0x01, label)))
    final_tick = tick(end)
    midi = b"MThd" + struct.pack(">IHHH", 6, 1, 2, ppq) + _track(metadata, final_tick) + _track(notes, final_tick)
    report = {"ppq": ppq, "q_base": str_q(start), "padding_ticks": padding,
              "midi_origin": midi_origin, "first_full_bar_q": score["first_full_bar_q"],
              "source_start_sec": source_start_sec,
              "actual_padding_sec": padding * 60 / (ppq * initial_bpm),
              "padding_error_sec": padding * 60 / (ppq * initial_bpm) - (source_start_sec if midi_origin == "audio" else 0.0),
              "tempo_rounding": tempo_errors,
              "unknown_event_ids": [e["id"] for e in score["events"] if e["kind"] == "unknown"]}
    return midi, report


def sidecar_for(midi: bytes, score: dict, info: dict) -> dict:
    return {"sidecar_version": "1.0", "midi_sha256": sha256(midi).hexdigest(),
            "q_base": info["q_base"], "padding_ticks": info["padding_ticks"],
            "midi_origin": info["midi_origin"], "score": score}
