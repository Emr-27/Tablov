"""外部后端出现超时、非零退出或半写入时，绝不能产生有效缓存。"""
from hashlib import sha256
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harmonica_transcriber.errors import ContractError
from harmonica_transcriber.runner_protocol import RunnerFailure, run_worker

MOCK = r'''
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
import time
p = argparse.ArgumentParser()
p.add_argument('--request')
a = p.parse_args()
r = json.loads(Path(a.request).read_text(encoding='utf-8'))
mode = r['backend_config']['mode']
out = Path(r['output_dir'])
if mode == 'timeout':
    time.sleep(4)
if mode == 'nonzero':
    sys.exit(7)
artifact = out / 'values.bin'
artifact.write_bytes(bytes([0, 1, 2, 3]))
if mode == 'partial':
    sys.exit(0)
manifest = {'protocol_version': '1.0', 'job_id': r['job_id'], 'backend': r['backend'],
            'backend_version': r['backend_version'], 'input_sha256': r['input']['sha256'],
            'status': 'complete', 'time': {'sample_rate': 16000, 'hop_samples': 160,
            'frame_center': 'center', 'source_start_sec': r['segment_start_sec'],
            'frame_count': 1, 'left_padding_samples': 0, 'right_padding_samples': 0,
            'trim_left_samples': 0, 'trim_right_samples': 0},
            'artifacts': [{'path': 'values.bin', 'size_bytes': 4,
                           'sha256': sha256(artifact.read_bytes()).hexdigest()}]}
if mode == 'bad_manifest':
    manifest['artifacts'][0]['sha256'] = '0' * 64
if mode == 'bad_schema':
    del manifest['time']['frame_count']
(out / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
'''


def request_for(audio: Path, mode: str = "ok", job: str = "job-1") -> dict:
    return {"protocol_version": "1.0", "job_id": job, "backend": "mock",
            "backend_version": "mock-v1", "backend_config": {"mode": mode},
            "weight_sha256": None, "config_sha256": None, "dependency_lock_sha256": None,
            "device_policy": "cpu", "input": {"path": str(audio.resolve()),
            "sha256": sha256(audio.read_bytes()).hexdigest()},
            "source_recording_id": "test-source", "segment_start_sec": 1.25}


class RunnerTests(unittest.TestCase):
    def test_valid_result_cache_reuse_and_failures_do_not_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / "mock.py"
            script.write_text(MOCK, encoding="utf-8")
            audio = root / "audio.pcm"
            audio.write_bytes(b"fake-audio-for-contract-test")
            jobs = root / "jobs"
            first = run_worker(request_for(audio), interpreter=Path(sys.executable), script=script, jobs_root=jobs)
            self.assertFalse(first["cache_hit"])
            second = run_worker(request_for(audio, job="job-2"), interpreter=Path(sys.executable), script=script, jobs_root=jobs)
            self.assertTrue(second["cache_hit"])
            self.assertEqual(first["manifest"], second["manifest"])
            self.assertEqual(len(list((jobs / "cache").iterdir())), 1)
            for mode, code in (("timeout", "TIMEOUT"), ("nonzero", "EXIT_NONZERO"),
                               ("partial", "MANIFEST_INVALID"), ("bad_manifest", "MANIFEST_INVALID"),
                               ("bad_schema", "MANIFEST_INVALID")):
                with self.assertRaises(RunnerFailure) as caught:
                    run_worker(request_for(audio, mode=mode, job="job-" + mode),
                               interpreter=Path(sys.executable), script=script,
                               jobs_root=jobs, timeout_sec=0.2 if mode == "timeout" else 2)
                self.assertEqual(caught.exception.code, code)
                self.assertTrue((caught.exception.job_dir / "runner_report.json").exists())
                self.assertEqual(len(list((jobs / "cache").iterdir())), 1)

    def test_changed_input_hash_is_rejected_before_cache_lookup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / "mock.py"
            script.write_text(MOCK, encoding="utf-8")
            audio = root / "audio.pcm"
            audio.write_bytes(b"first")
            request = request_for(audio)
            run_worker(request, interpreter=Path(sys.executable), script=script, jobs_root=root / "jobs")
            audio.write_bytes(b"second")
            with self.assertRaises(ContractError):
                run_worker(request, interpreter=Path(sys.executable), script=script, jobs_root=root / "jobs")


if __name__ == "__main__":
    unittest.main()
