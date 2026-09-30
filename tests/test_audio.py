"""Real local pYIN/FFmpeg smoke tests plus deterministic timing checks."""
import base64
import json
from hashlib import sha256
import math
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
import zipfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from harmonica_transcriber.audio import estimate_tempo, transcribe, resources, notes_to_score
from harmonica_transcriber.cli import run
from harmonica_transcriber.render import markdown_score, render_events
from harmonica_transcriber.webui import generate
from types import SimpleNamespace

def sine_fixture(path):
    sr = 16000
    data = []
    notes = [523.251, 587.33, 659.255, 698.456]
    def append(seconds, hz):
        for i in range(int(sr*seconds)):
            fade = min(1.0, i/180, (int(sr*seconds)-i)/180) if hz else 0
            value = 0.28 * math.sin(2*math.pi*hz*i/sr) * max(0, fade) if hz else 0
            data.append(round(value*32767))
    append(.25, 0)
    for hz in notes:
        append(.45, hz)
        append(.05, 0)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(sr)
        wav.writeframes(struct.pack("<" + "h"*len(data), *data))

class AudioTests(unittest.TestCase):
    def test_auto_tempo_recognizes_click_track(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wav_path = root / "beat.wav"
            sample_rate = 11025
            samples = []
            for index in range(12 * sample_rate):
                in_beat = index % round(sample_rate * 0.6)
                value = (0.7 * math.sin(2 * math.pi * 1300 * in_beat / sample_rate) *
                         math.exp(-in_beat / 140) if in_beat < 1200 else 0)
                samples.append(round(value * 32767))
            with wave.open(str(wav_path), "wb") as output:
                output.setnchannels(1); output.setsampwidth(2); output.setframerate(sample_rate)
                output.writeframes(struct.pack("<" + "h" * len(samples), *samples))
            result = estimate_tempo(wav_path, root / "job", 0, 12)
            self.assertAlmostEqual(result["bpm"], 100, delta=3)
            self.assertEqual(result["input_sha256"], sha256(wav_path.read_bytes()).hexdigest())
            frames = [{"sec": round(index * .016, 6), "rms": .2, "hz": 261.626,
                       "candidate_hz": 261.626, "voiced": True, "periodicity": .95}
                      for index in range(30)]
            evidence = {"backend": "test", "backend_version": "1", "input_sha256": result["input_sha256"],
                        "source_start_sec": 0, "decoded_duration_sec": .48,
                        "frames": frames, "onset_frames": []}
            with patch("harmonica_transcriber.audio.extract_evidence", return_value=evidence):
                score, manifest = transcribe(wav_path, root / "transcribed", source_name="beat.wav",
                                             start_sec=0, duration_sec=12, bpm="auto", key_text="C major")
            self.assertTrue(score["events"])
            self.assertAlmostEqual(manifest["tempo_bpm_q"], 100, delta=3)
            self.assertEqual(manifest["tempo_source"], "estimated_spectral_flux_unconfirmed")
            self.assertTrue((root / "transcribed" / "tempo_evidence.json").is_file())

    def test_short_vocal_pitch_wobble_is_absorbed(self):
        frames = []
        for index, midi in enumerate([60] * 12 + [61] * 3 + [60] * 12):
            hz = 440 * 2 ** ((midi - 69) / 12)
            frames.append({"sec": round(index * .016, 6), "rms": .2,
                           "hz": hz, "candidate_hz": hz, "voiced": True,
                           "periodicity": 1.0})
        evidence = {"backend": "rmvpe-local", "input_sha256": "a" * 64,
                    "source_start_sec": 0.0, "decoded_duration_sec": len(frames) * .016,
                    "frames": frames, "onset_frames": []}
        score, summary = notes_to_score(evidence, source_name="test", bpm=120, key_text="C major")
        self.assertEqual({event["pitch_midi"] for event in score["events"] if event["kind"] == "note"}, {60})
        self.assertGreater(summary["smoothed_pitch_frames"], 0)

    def test_ambiguous_accompaniment_candidates_stay_unknown(self):
        frames = [{"sec": round(index * .016, 6), "rms": .2, "hz": None,
                   "candidate_hz": 261.626, "voiced": False, "periodicity": 0.0}
                  for index in range(40)]
        evidence = {"backend": "fft-yin-highpass-candidate", "input_sha256": "a" * 64,
                    "source_start_sec": 0.0, "decoded_duration_sec": len(frames) * .016,
                    "frames": frames, "onset_frames": []}
        score, summary = notes_to_score(evidence, source_name="test", bpm=120,
                                        key_text="C major", allow_no_notes=True, min_note_frames=10)
        self.assertEqual(summary["note_count"], 0)
        self.assertTrue(all(event["kind"] == "unknown" for event in score["events"]))

    def test_short_weak_pitch_is_written_as_reviewable_note(self):
        hz = 261.626
        def frame(index, *, strong=False, candidate=False):
            return {"sec": round(index * .016, 6), "rms": .2,
                    "hz": hz if strong else None, "voiced": strong,
                    "periodicity": .9 if strong else 0,
                    "candidate_hz": hz if candidate or strong else None}
        frames = ([frame(i, strong=True) for i in range(10)] +
                  [frame(i, candidate=True) for i in range(10, 18)] +
                  [frame(i, strong=True) for i in range(18, 30)])
        evidence = {"input_sha256": "a" * 64, "source_start_sec": 0.0,
                    "decoded_duration_sec": .48, "frames": frames, "onset_frames": []}
        score, summary = notes_to_score(evidence, source_name="test", bpm=120, key_text="C major")
        self.assertEqual(summary["unknown_count"], 0)
        self.assertGreater(summary["inferred_note_count"], 0)
        self.assertTrue(any(part.get("review_required") for bar in render_events(score) for part in bar))
        self.assertIn("1*:", markdown_score(score))
        for index in range(10, 18):
            frames[index] = frame(index)
        frames[30:30] = [frame(i) for i in range(30, 46)]
        evidence["frames"] = frames
        evidence["decoded_duration_sec"] = .736
        _, longer = notes_to_score(evidence, source_name="test", bpm=120, key_text="C major",
                                   allow_no_notes=True)
        self.assertGreater(longer["unknown_count"], 0)

    def test_accompaniment_candidate_is_exported_and_kept_after_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wav = root / "four.wav"
            sine_fixture(wav)

            def fake_separator(path, job, start, duration):
                output = job / "msst" / "output"
                output.mkdir(parents=True)
                vocal = output / "segment_vocals.wav"
                accompaniment = output / "segment_other.wav"
                shutil.copyfile(wav, vocal)
                shutil.copyfile(wav, accompaniment)
                return vocal, {"backend": "test-separator", "vocal_sha256": sha256(vocal.read_bytes()).hexdigest(),
                               "accompaniment_sha256": sha256(accompaniment.read_bytes()).hexdigest()}

            with patch("harmonica_transcriber.separation.separate_vocals", side_effect=fake_separator):
                first = generate({"input": {"name": wav.name, "data": base64.b64encode(wav.read_bytes()).decode()},
                                  "settings": {"audio_start": 0, "audio_duration": 2.25,
                                               "audio_bpm": 120, "audio_mode": "vocal"}}, root / "jobs")
            self.assertIn("accompaniment_melody.mid", first["downloads"])
            self.assertIn("accompaniment_preview.mp3", first["downloads"])
            self.assertTrue(first["accompaniment_bars"])
            self.assertGreater(first["report"]["audio"]["accompaniment"]["note_count"], 0)
            second = generate({"score": first["score"], "parent_job_id": first["job_id"], "edits": [],
                               "settings": {"transpose": 12}}, root / "jobs")
            self.assertEqual(first["accompaniment_score"]["events"], second["accompaniment_score"]["events"])
            self.assertIn("accompaniment_preview.mp3", second["downloads"])

    def test_full_song_chunks_join_on_original_timeline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wav = root / "four.wav"
            sine_fixture(wav)
            updates = []
            with patch("harmonica_transcriber.audio.CHUNK_SECONDS", 1.25):
                result = generate({"input": {"name": wav.name, "data": base64.b64encode(wav.read_bytes()).decode()},
                                   "settings": {"audio_full": True, "audio_mode": "direct", "audio_bpm": 120,
                                                "audio_start": 0, "audio_duration": 30}}, root / "jobs",
                                  progress=updates.append)
            manifest = result["report"]["audio"]
            self.assertTrue(manifest["full_song"])
            self.assertEqual(len(manifest["chunks"]), 2)
            self.assertAlmostEqual(manifest["decoded_duration_sec"], 2.25, delta=0.02)
            self.assertEqual(manifest["chunks"][1]["start_sec"], 1.25)
            pitches = [event["pitch_midi"] for event in result["score"]["events"] if event["kind"] == "note"]
            self.assertEqual(list(dict.fromkeys(pitches))[:4], [72, 74, 76, 77])
            self.assertAlmostEqual(result["report"]["processed_duration_sec"], 2.25, delta=0.15)
            self.assertIn("audio_manifest.json", result["downloads"])
            self.assertEqual([(item["chunks_completed"], item["chunks_total"]) for item in updates
                              if item["phase"].startswith("正在处理第")], [(0, 2), (1, 2)])
            self.assertEqual((updates[-1]["status"], updates[-1]["completed"], updates[-1]["total"]),
                             ("complete", 4, 4))

    def test_full_vocal_song_keeps_joined_preview_after_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wav = root / "four.wav"
            sine_fixture(wav)
            _, ffmpeg = resources()

            def fake_separator(path, job, start, duration):
                vocal = job / "msst" / "output" / "segment_vocals.wav"
                vocal.parent.mkdir(parents=True)
                subprocess.run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
                                "-ss", str(start), "-t", str(duration), str(vocal)], check=True)
                shutil.copyfile(vocal, vocal.with_name("segment_other.wav"))
                return vocal, {"backend": "test-separator", "vocal_sha256": sha256(vocal.read_bytes()).hexdigest()}

            with patch("harmonica_transcriber.audio.CHUNK_SECONDS", 1.25), \
                 patch("harmonica_transcriber.separation.separate_vocals", side_effect=fake_separator):
                first = generate({"input": {"name": wav.name, "data": base64.b64encode(wav.read_bytes()).decode()},
                                  "settings": {"audio_full": True, "audio_mode": "vocal", "audio_bpm": 120,
                                               "audio_start": 0, "audio_duration": 30}}, root / "jobs")
            self.assertEqual(len(first["report"]["audio"]["separation"]["chunks"]), 2)
            self.assertIn("vocal_preview.mp3", first["downloads"])
            self.assertIn("vocal_segment.wav", first["downloads"])
            self.assertIn("accompaniment_melody.mid", first["downloads"])
            with zipfile.ZipFile(Path(first["output_path"]) / "bapuluofu.zip") as archive:
                self.assertNotIn("vocal_segment.wav", archive.namelist())
                self.assertIn("accompaniment_preview.mp3", archive.namelist())
            self.assertEqual(first["report"]["audio"]["accompaniment"]["frame_count"],
                             first["report"]["audio"]["frame_count"])
            second = generate({"score": first["score"], "parent_job_id": first["job_id"], "edits": [],
                               "settings": {"transpose": 12}}, root / "jobs")
            self.assertEqual(second["report"]["audio"]["chunks"], first["report"]["audio"]["chunks"])
            self.assertEqual((Path(first["output_path"]) / "vocal_preview.mp3").read_bytes(),
                             (Path(second["output_path"]) / "vocal_preview.mp3").read_bytes())
            self.assertEqual(first["accompaniment_score"]["events"], second["accompaniment_score"]["events"])

    def test_vocal_evidence_reused_for_octave_arrangement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wav = root / "four.wav"
            sine_fixture(wav)

            def fake_separator(path, job, start, duration):
                vocal = job / "msst" / "output" / "segment_vocals.wav"
                vocal.parent.mkdir(parents=True)
                shutil.copyfile(wav, vocal)
                return vocal, {"backend": "test-separator", "vocal_sha256": sha256(vocal.read_bytes()).hexdigest()}

            jobs = root / "jobs"
            with patch("harmonica_transcriber.separation.separate_vocals", side_effect=fake_separator):
                first = generate({"input": {"name": wav.name, "data": base64.b64encode(wav.read_bytes()).decode()},
                                  "settings": {"audio_start": 0, "audio_duration": 2, "audio_bpm": 120,
                                               "audio_mode": "vocal", "transpose": 0}}, jobs)
            second = generate({"score": first["score"], "parent_job_id": first["job_id"], "edits": [],
                               "settings": {"transpose": 12}}, jobs)
            self.assertEqual(second["arrangement"]["global_transpose"], 12)
            self.assertEqual(first["score"]["source"]["sha256"], second["score"]["source"]["sha256"])
            self.assertIn("vocal_segment.wav", second["downloads"])
            self.assertEqual((Path(first["output_path"]) / "vocal_segment.wav").read_bytes(),
                             (Path(second["output_path"]) / "vocal_segment.wav").read_bytes())

    def test_real_wav_mp3_and_audio_only_mp4(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wav = root / "four.wav"
            sine_fixture(wav)
            _, ffmpeg = resources()
            mp3 = root / "four.mp3"
            subprocess.run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                            "-i", str(wav), "-codec:a", "libmp3lame", str(mp3)], check=True)
            mp4 = root / "four.mp4"
            subprocess.run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                            "-i", str(wav), "-codec:a", "aac", str(mp4)], check=True)
            for source in (wav, mp3, mp4):
                with self.subTest(source=source.suffix):
                    job = root / source.suffix[1:]
                    job.mkdir()
                    score, manifest = transcribe(source, job, source_name=source.name,
                                                 start_sec=0, duration_sec=3, bpm=120,
                                                 key_text="C major")
                    pitches = [e["pitch_midi"] for e in score["events"] if e["kind"] == "note"]
                    self.assertGreaterEqual(len(pitches), 4, pitches)
                    self.assertEqual([72, 74, 76, 77], list(dict.fromkeys(pitches))[:4])
                    self.assertEqual(manifest["backend"], "fft-yin")
                    self.assertEqual(score["meter_alignment_status"], "unconfirmed")
                    score_path = job / "score.json"
                    score_path.write_text(json.dumps(score), encoding="utf-8")
                    output, code = run(SimpleNamespace(input=score_path, output=job/"output",
                        profile=None, instrument=None, transpose="0", octave_policy="reject",
                        midi_origin=None, track=None, channel=None, key=None, do_midi=None,
                        strict=False, overwrite=False, debug=False))
                    self.assertEqual(code, 0)
                    self.assertTrue((output / "harmonica_tab.md").is_file())
                    self.assertEqual(json.loads((output / "report.json").read_text(encoding="utf-8"))["status"], "partial")
            result = generate({"input": {"name": "four.wav", "data": base64.b64encode(wav.read_bytes()).decode()},
                               "settings": {"audio_start": 0, "audio_duration": 3, "audio_bpm": 120,
                                            "audio_key": "C major", "transpose": 0}}, root / "web-jobs")
            self.assertEqual(result["name"], "four.wav")
            self.assertEqual(result["report"]["status"], "partial")
            self.assertIn("pitch_evidence.json", result["downloads"])
            self.assertTrue((Path(result["output_path"]) / "bapuluofu.zip").is_file())
            first = next(e for e in result["score"]["events"] if e["kind"] == "note")
            corrected = generate({"score": result["score"], "parent_job_id": result["job_id"],
                                  "edits": [{"id": first["id"], "pitch_midi": first["pitch_midi"] + 1}],
                                  "settings": {"transpose": 0}}, root / "web-jobs")
            self.assertIn("pitch_evidence.json", corrected["downloads"])
            self.assertEqual(corrected["score"]["events"][result["score"]["events"].index(first)]["pitch_midi"], first["pitch_midi"] + 1)
            demo = generate({"example_audio": True, "settings": {"audio_duration": 3,
                             "audio_bpm": 120, "audio_key": "C major"}}, root / "demo-jobs")
            self.assertEqual(demo["name"], "c_major_four_notes.wav")

if __name__ == "__main__":
    unittest.main()
