"""V2 T01–T11 中 Phase 0 已声明支持的可重复验收。"""
from copy import deepcopy
from fractions import Fraction
from itertools import product
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from harmonica_transcriber.arrangement import arrange, with_performed_times
from harmonica_transcriber.cli import main
from harmonica_transcriber.errors import ContractError
from harmonica_transcriber.harmonica import Fingering, candidates, optimize, pitch_for, transition, validate_profile
from harmonica_transcriber.midi_io import _read_tracks, export_midi, import_midi, sidecar_for
from harmonica_transcriber.models import validate_score
from harmonica_transcriber.render import bar_boundaries, markdown_score, render_events
from harmonica_transcriber.theory import key_from_text, parse_jianpu, spell_pitch
from harmonica_transcriber.time_map import q_to_sec


def sample():
    return json.loads((ROOT / "examples" / "g_major_four_notes.json").read_text(encoding="utf-8-sig"))


def profile():
    return validate_profile(json.loads((ROOT / "profiles" / "chromatic_12_c_conservative.json").read_text(encoding="utf-8")))


def event(eid, kind, at, duration, pitch=None, phrase="p1", reason=None, **extra):
    value = {"id": eid, "kind": kind, "start_q": at, "duration_q": duration}
    if kind == "note":
        value.update(pitch_midi=pitch, phrase_id=phrase, spelling_hint=None)
    else:
        value["reason"] = reason or ("confirmed_silence" if kind == "rest" else "not_resolved")
    value.update(extra)
    return value


def vlq(n):
    parts = [n & 127]
    n >>= 7
    while n:
        parts.insert(0, (n & 127) | 128)
        n >>= 7
    return bytes(parts)


def smf_track(items, finish):
    out = bytearray()
    last = 0
    for at, data in sorted(items, key=lambda x: x[0]):
        out += vlq(at-last) + data
        last = at
    out += vlq(finish-last) + b"\xff\x2f\x00"
    return b"MTrk" + struct.pack(">I", len(out)) + out


def independent_midi(notes, *, kind=1, extra_track=None):
    meta = [(0, b"\xff\x51\x03\x0f\x42\x40"),
            (0, b"\xff\x58\x04\x04\x02\x18\x08"),
            (1920, b"\xff\x51\x03\x07\xa1\x20")]
    musical = []
    for start, end, pitch in notes:
        musical.extend([(start, bytes((0x90, pitch, 80))), (end, bytes((0x80, pitch, 0)))])
    musical += extra_track or []
    finish = max([2400] + [at for at, _ in musical])
    if kind == 0:
        return b"MThd" + struct.pack(">IHHH", 6, 0, 1, 480) + smf_track(meta + musical, finish)
    return b"MThd" + struct.pack(">IHHH", 6, kind, 2, 480) + smf_track(meta, finish) + smf_track(musical, finish)


class Phase0Tests(unittest.TestCase):
    def test_g_major_vertical_slice_and_profile(self):
        score = validate_score(sample())
        arr = arrange(score, profile())
        self.assertEqual([e["fingering"]["tab"] for e in arr["events"]], ["5D", "6B", "7B", "6D+"])
        self.assertIn("简谱 | 5:1 6:1 1':1 7:1 |", markdown_score(score, arr))
        self.assertIn("TAB  | 5D:1 6B:1 7B:1 6D+:1 |", markdown_score(score, arr))
        self.assertEqual([e["pitch_midi"] for e in score["events"]], [74, 76, 79, 78])
        self.assertIsNone(pitch_for(profile(), Fingering(12, 1, 1)))

    def test_T01_triplet_exact_and_cross_bar_tie(self):
        score = sample()
        score["score_end_q"] = "9/2"
        score["events"] = [event("n1", "note", "0", "1/3", 72, tuplet_group_id="t1"),
                           event("n2", "note", "1/3", "1/3", 74, tuplet_group_id="t1"),
                           event("n3", "note", "2/3", "1/3", 76, tuplet_group_id="t1"),
                           event("r1", "rest", "1", "5/2"),
                           event("n4", "note", "7/2", "1", 72)]
        score = validate_score(score)
        bars = render_events(score)
        parts = [part for bar in bars for part in bar if part["id"] == "n4"]
        self.assertEqual([part["duration_q"] for part in parts], ["1/2", "1/2"])
        self.assertEqual([part["tie_after"] for part in parts], [True, False])
        self.assertIn("T3:2(", markdown_score(score))
        self.assertIn("尾部残小节：1/2q", markdown_score(score))
        midi, _ = export_midi(score)
        _, tracks = _read_tracks(midi)
        self.assertEqual(sum(m["kind"] == "channel" and m["type"] == 0x90 and m["data"][1] > 0 for m in tracks[1]), 4)

    def test_T02_observed_seconds_are_not_performed_seconds(self):
        score = sample()
        score["time_map"]["tempo_events"][0]["bpm_q"] = 82
        score["score_end_q"] = "1/2"
        score["events"] = [event("n1", "note", "0", "1/2", 72,
                                 observed={"start_sec": 0.520, "end_sec": 0.913, "producer": "manual"})]
        result = with_performed_times(validate_score(score))["events"][0]
        self.assertEqual(result["observed"]["start_sec"], 0.520)
        self.assertEqual(result["observed"]["end_sec"], 0.913)
        self.assertAlmostEqual(result["performed"]["end_sec"], 30/82)

    def test_T03_pickup_variable_tempo_and_audio_padding(self):
        score = sample()
        score.update(score_start_q="-1", score_end_q="8", first_full_bar_q="0")
        score["time_map"] = {"kind": "tempo", "origin_q": "0", "origin_sec": 1.25,
                             "tempo_events": [{"at_q": "-1", "bpm_q": 60}, {"at_q": "4", "bpm_q": 120}]}
        score["meter_map"][0]["at_q"] = "-1"
        score["key_map"][0]["at_q"] = "-1"
        score["events"] = [event("pickup", "note", "-1", "1", 72),
                           event("gap", "rest", "0", "4"),
                           event("ending", "note", "4", "4", 74)]
        score = validate_score(score)
        self.assertAlmostEqual(q_to_sec(score["time_map"], Fraction(-1)), 0.25)
        self.assertAlmostEqual(q_to_sec(score["time_map"], Fraction(8)), 7.25)
        self.assertIn("弱起：1q", markdown_score(score))
        midi, info = export_midi(score, midi_origin="audio")
        self.assertEqual(info["padding_ticks"], 240)
        _, tracks = _read_tracks(midi)
        starts = [m["tick"] for m in tracks[1] if m["kind"] == "channel" and m["type"] == 0x90]
        self.assertEqual(starts[0], 240)
        self.assertAlmostEqual(info["actual_padding_sec"], .25)
        with tempfile.TemporaryDirectory() as temp:
            midi_path = Path(temp) / "pickup.mid"
            midi_path.write_bytes(midi)
            Path(str(midi_path) + ".json").write_text(json.dumps(sidecar_for(midi, score, info)), encoding="utf-8")
            restored, _ = import_midi(midi_path, track=1, channel=0)
            self.assertEqual(restored["score_start_q"], "-1")
            self.assertEqual(restored["first_full_bar_q"], "0")
            self.assertEqual(restored["time_map"]["origin_sec"], 1.25)

    def test_anchor_map_and_meter_change_boundaries(self):
        score = sample()
        score["score_end_q"] = "10"
        score["events"] = [event("long-rest", "rest", "0", "10")]
        score["time_map"] = {"kind": "anchors", "origin_q": "0", "origin_sec": 0.0,
                             "anchors": [{"q": "0", "sec": 0.0}, {"q": "4", "sec": 4.0},
                                         {"q": "10", "sec": 7.0}]}
        score["meter_map"].append({"at_q": "4", "numerator": 3, "denominator": 4, "source": "user"})
        score = validate_score(score)
        self.assertEqual(bar_boundaries(score), [Fraction(0), Fraction(4), Fraction(7), Fraction(10)])
        self.assertAlmostEqual(q_to_sec(score["time_map"], Fraction(8)), 6.0)
        midi, info = export_midi(score)
        _, tracks = _read_tracks(midi)
        tempos = [(m["tick"], int.from_bytes(m["data"], "big")) for m in tracks[0] if m["kind"] == "meta" and m["subtype"] == 0x51]
        self.assertEqual(tempos, [(0, 1_000_000), (3840, 500_000)])
        self.assertEqual(info["tempo_rounding"][1]["at_q"], "4")

    def test_profile_rejects_unverified_high_draw_slide(self):
        p = deepcopy(profile())
        p["holes"]["12"][3] = 98
        with self.assertRaises(ContractError):
            validate_profile(p)

    def test_T04_import_reads_global_tempo_and_repeat_roundtrip(self):
        data = independent_midi([(0, 480, 74), (1920, 2400, 76)])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "two_tracks.mid"
            path.write_bytes(data)
            score, _ = import_midi(path, track=1, channel=0, key_text="G major", do_midi=67)
            self.assertEqual([(e["start_q"], e["duration_q"]) for e in score["events"] if e["kind"] == "note"], [("0", "1"), ("4", "1")])
            self.assertAlmostEqual(q_to_sec(score["time_map"], Fraction(4)), 4)
            self.assertAlmostEqual(q_to_sec(score["time_map"], Fraction(5)), 4.5)
            out, _ = export_midi(score)
            output = Path(temp) / "again.mid"
            output.write_bytes(out)
            restored, _ = import_midi(output, track=1, channel=0)
            self.assertEqual([e["pitch_midi"] for e in restored["events"] if e["kind"] == "note"], [74, 76])

    def test_T05_reject_polyphony_type2_and_controller(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.mid"
            for data in [independent_midi([(0, 480, 74), (240, 720, 76)]),
                         independent_midi([(0, 480, 74)], kind=2),
                         independent_midi([(0, 480, 74)], extra_track=[(0, b"\xb0\x40\x7f")]),
                         independent_midi([(0, 480, 74)], extra_track=[(0, b"\xe0\x01\x40")])]:
                path.write_bytes(data)
                with self.assertRaises(ContractError):
                    import_midi(path, track=1, channel=0)
            # Velocity-zero note-on is note-off, and same-tick reattack is valid.
            path.write_bytes(independent_midi([], extra_track=[(0, b"\x90\x4a\x50"),
                                                                  (480, b"\x90\x4a\x00"),
                                                                  (480, b"\x90\x4a\x50"),
                                                                  (960, b"\x90\x4a\x00")]))
            score, _ = import_midi(path, track=1, channel=0)
            self.assertEqual(sum(e["kind"] == "note" for e in score["events"]), 2)

    def test_T06_spelling_minor_and_enharmonics(self):
        g = key_from_text("G major", 67)
        self.assertEqual([spell_pitch(p, g)[0] for p in (67, 78, 79)], ["1", "7", "1'"])
        a = key_from_text("A minor", 60)
        self.assertEqual([spell_pitch(p, a)[0] for p in (57, 60, 69, 68)], ["6,", "1", "6", "#5"])
        c = key_from_text("C major", 60)
        self.assertEqual(spell_pitch(36, c), ("1,,", "C2"))
        self.assertEqual(parse_jianpu("1,,", c), 36)
        self.assertEqual(spell_pitch(61, c, "C#4")[0], "#1")
        self.assertEqual(spell_pitch(61, c, "Db4")[0], "b2")
        for hint in ("C#4", "Db4"):
            token, _ = spell_pitch(61, c, hint)
            self.assertEqual(parse_jianpu(token, c), 61)

    def test_T07_candidates_and_dp_equals_exhaustive(self):
        p = profile()
        self.assertEqual([f.label for f in candidates(p, 72)], ["4B", "4D+", "5B"])
        self.assertEqual({f.label for f in candidates(p, 65)}, {"2D", "2B+"})
        for pitch in range(128):
            for f in candidates(p, pitch):
                self.assertEqual(pitch_for(p, f), pitch)
        score = sample()
        score["score_end_q"] = "5"
        score["events"] = [event(f"n{i}", "note", str(i), "1", p) for i, p in enumerate((64, 65, 67, 72, 74))]
        score = validate_score(score)
        notes = arrange(score, p)["events"]
        chosen, details = optimize(notes, p, score["time_map"])
        rows = [candidates(p, e["played_pitch_midi"]) for e in notes]
        exhaustive = min(sum(transition(path[i-1], path[i], notes[i-1], notes[i], score["time_map"])["total"] for i in range(1, len(notes))) for path in product(*rows))
        self.assertAlmostEqual(details["total"], exhaustive)
        self.assertEqual([f.label for f in chosen], [e["fingering"]["tab"] for e in notes])

    def test_T08_preserve_original_and_explicit_plus_twelve(self):
        score = sample()
        score["score_end_q"] = "2"
        score["key_map"][0].update(tonic_pc=0, tonic_spelling="C", do_midi=60)
        score["events"] = [event("low", "note", "0", "1", 59), event("up", "note", "1", "1", 60)]
        score = validate_score(score)
        plain = arrange(score, profile())
        shifted = arrange(score, profile(), 12)
        self.assertEqual([e["pitch_midi"] for e in score["events"]], [59, 60])
        self.assertEqual([e["played_pitch_midi"] for e in shifted["events"]], [71, 72])
        self.assertEqual(shifted["events"][1]["played_pitch_midi"] - shifted["events"][0]["played_pitch_midi"], 1)
        self.assertIsNone(plain["events"][0]["fingering"])
        self.assertTrue(all(e["playable"] for e in shifted["events"]))

    def test_T09_edit_then_reexport_without_model(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            score = sample()
            (base / "input.json").write_text(json.dumps(score), encoding="utf-8")
            self.assertEqual(main([str(base / "input.json"), "--output", str(base / "out1")]), 0)
            revised = json.loads((base / "out1" / "original_score.json").read_text(encoding="utf-8"))
            revised["revision_id"] = "user-edit-r2"
            revised["events"][0]["duration_q"] = "1/2"
            revised["events"][1].update(start_q="1/2", duration_q="3/2", pitch_midi=77, spelling_hint="F5")
            (base / "edited.json").write_text(json.dumps(revised), encoding="utf-8")
            self.assertEqual(main([str(base / "edited.json"), "--output", str(base / "out2")]), 0)
            arr = json.loads((base / "out2" / "arrangement.json").read_text(encoding="utf-8"))
            report = json.loads((base / "out2" / "report.json").read_text(encoding="utf-8"))
            self.assertEqual([e["id"] for e in arr["events"]], ["n1", "n2", "n3", "n4"])
            self.assertEqual(arr["events"][1]["played_pitch_midi"], 77)
            self.assertIsNone(report["model"])
            self.assertEqual(main([str(base / "edited.json"), "--output", str(base / "out2")]), 2)

    def test_T10_repeated_same_pitch_not_merged_on_export(self):
        score = sample()
        score["score_end_q"] = "3"
        score["events"] = [event("g1", "note", "0", "1", 67), event("g2", "note", "1", "1", 67),
                           event("g3", "note", "2", "1", 67)]
        score = validate_score(score)
        midi, _ = export_midi(score)
        _, tracks = _read_tracks(midi)
        self.assertEqual(sum(m["kind"] == "channel" and m["type"] == 0x90 and m["data"][1] > 0 for m in tracks[1]), 3)

    def test_T11_rest_unknown_and_strict_exit(self):
        score = sample()
        score["score_end_q"] = "6"
        score["events"] = [event("silence", "rest", "0", "2", reason="confirmed_silence"),
                           event("uncertain", "unknown", "2", "2", reason="not_resolved"),
                           event("melody", "note", "4", "2", 74)]
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            path = base / "input.json"
            path.write_text(json.dumps(score), encoding="utf-8")
            self.assertEqual(main([str(path), "--output", str(base / "out")]), 0)
            report = json.loads((base / "out" / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "partial")
            self.assertEqual(report["unknown_event_ids"], ["uncertain"])
            self.assertIn("?:2", (base / "out" / "melody_original.md").read_text(encoding="utf-8"))
            self.assertEqual(main([str(path), "--output", str(base / "strict"), "--strict"]), 4)
            midi = base / "out" / "melody_original.mid"
            no_sidecar = base / "alone.mid"
            no_sidecar.write_bytes(midi.read_bytes())
            restored, _ = import_midi(no_sidecar, track=1, channel=0)
            self.assertEqual(sum(e["kind"] == "unknown" for e in restored["events"]), 1)


if __name__ == "__main__":
    unittest.main()
