"""Exercise the real HTTP upload, correction and export workflow."""
import base64
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler
import zipfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from harmonica_transcriber.webui import LocalServer

class WebUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.server = LocalServer(("127.0.0.1", 0), Path(cls.temp.name))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def request(self, path, payload=None, token=True, headers=None):
        headers = dict(headers or {})
        if payload is not None:
            headers.update({"Content-Type": "application/json", "Origin": self.server.origin})
            if token:
                headers["X-Bapuluofu-Token"] = self.server.token
        data = json.dumps(payload).encode() if payload is not None else None
        try:
            with build_opener(ProxyHandler({})).open(Request(self.server.origin + path, data=data, headers=headers), timeout=10) as response:
                return response.status, response.read()
        except HTTPError as exc:
            return exc.code, exc.read()

    def post(self, payload):
        status, data = self.request("/api/generate", payload)
        self.assertEqual(status, 200, data.decode())
        return json.loads(data)

    @staticmethod
    def upload(name, data):
        return {"name": name, "data": base64.b64encode(data).decode()}

    def test_demo_and_downloads(self):
        status, html = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(self.server.token.encode(), html)
        for path in ["/app.js", "/app.css", "/health"]:
            self.assertEqual(self.request(path)[0], 200)
        result = self.post({"example": True})
        self.assertEqual(result["report"]["status"], "complete")
        self.assertEqual([e["tab"] for bar in result["bars"] for e in bar], ["5D", "6B", "7B", "6D+"])
        status, archive = self.request(result["downloads"]["bapuluofu.zip"])
        self.assertEqual(status, 200)
        with build_opener(ProxyHandler({})).open(
            Request(self.server.origin + result["downloads"]["bapuluofu.zip"], method="HEAD"), timeout=10
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(int(response.headers["Content-Length"]), len(archive))
            self.assertEqual(response.read(), b"")
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            self.assertEqual(len(z.namelist()), 10)
            self.assertIn(b"MThd", z.read("melody_harmonica.mid"))
        self.assertIn("5D", self.request(result["downloads"]["harmonica_tab.md"])[1].decode())

    def test_upload_transpose_edit_and_midi_roundtrip(self):
        source = (ROOT / "examples" / "g_major_four_notes.json").read_bytes()
        result = self.post({"input": self.upload("我的乐谱.json", source), "settings": {"transpose": 12}})
        self.assertEqual(result["score"]["events"][0]["pitch_midi"], 74)
        self.assertEqual(result["arrangement"]["events"][0]["played_pitch_midi"], 86)
        edited = self.post({"score": result["score"], "edits": [{"id": "n1", "pitch_midi": 75}], "settings": {"transpose": 0}})
        self.assertEqual(edited["score"]["events"][0]["pitch_midi"], 75)
        self.assertEqual(edited["score"]["events"][0]["duration_q"], "1")
        self.assertEqual(edited["score"]["changes"][-1]["producer"], "user")
        self.assertNotEqual(edited["job_id"], result["job_id"])
        midi = self.request(edited["downloads"]["melody_original.mid"])[1]
        sidecar = self.request(edited["downloads"]["melody_original.mid.json"])[1]
        restored = self.post({"input": self.upload("旋律.mid", midi), "sidecar": self.upload("旋律.mid.json", sidecar)})
        self.assertEqual(restored["score"]["events"][0]["pitch_midi"], 75)
        self.assertEqual(restored["report"]["status"], "complete")

    def test_errors_and_local_access(self):
        self.assertEqual(self.request("/api/generate", {"example": True}, token=False)[0], 403)
        for payload in [
            {"example": True, "settings": {"transpose": 49}},
            {"input": self.upload("audio.mp3", b"audio")},
            {"input": self.upload("broken.json", b"{broken")},
        ]:
            status, data = self.request("/api/generate", payload)
            self.assertEqual(status, 400)
            self.assertTrue(json.loads(data)["error"])
        self.assertEqual(self.request("/files/../../README.md")[0], 404)

    def test_progress_endpoint_tracks_live_job_and_requires_token(self):
        progress_id = "a" * 32
        started = threading.Event()
        release = threading.Event()
        response = []

        def fake_generate(payload, jobs_root, progress=None):
            progress({"status": "running", "phase": "正在处理第 2/3 段", "completed": 1,
                      "total": 5, "chunks_completed": 1, "chunks_total": 3})
            started.set()
            self.assertTrue(release.wait(5))
            progress({"status": "complete", "phase": "处理完成", "completed": 5,
                      "total": 5, "chunks_completed": 3, "chunks_total": 3})
            return {"ok": True}

        with patch("harmonica_transcriber.webui.generate", side_effect=fake_generate):
            worker = threading.Thread(target=lambda: response.append(
                self.request("/api/generate", {"example": True, "progress_id": progress_id})))
            worker.start()
            try:
                self.assertTrue(started.wait(5))
                url = "/api/progress/" + progress_id
                self.assertEqual(self.request(url)[0], 403)
                status, body = self.request(url, headers={"X-Bapuluofu-Token": self.server.token})
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["chunks_completed"], 1)
            finally:
                release.set()
                worker.join(5)
        self.assertEqual(response[0][0], 200)
        status, body = self.request(url, headers={"X-Bapuluofu-Token": self.server.token})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "complete")

if __name__ == "__main__":
    unittest.main()
