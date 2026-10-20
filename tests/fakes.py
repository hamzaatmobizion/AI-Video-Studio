"""Test doubles. These live ONLY in tests/ — app code never ships fakes."""
import hashlib
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from worker.generator import ClipRequest, GenerationError

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def make_clip(path: Path, seconds: float = 8.0, size: str = "720x1280") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=24:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-shortest",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
        check=True)
    return path


class TestPatternGenerator:
    """Worker-side generator that renders an FFmpeg test pattern, or raises scripted errors."""

    __test__ = False

    def __init__(self, script: list | None = None, seconds: float = 8.0, size: str = "720x1280"):
        self.profile = json.loads((Path(__file__).parent.parent / "worker" / "profile.json").read_text())
        self.script = list(script or [])  # items: Exception instance, "ok", ("short", secs), callable
        self.seconds, self.size = seconds, size
        self.calls: list[ClipRequest] = []

    def generate(self, req: ClipRequest, should_cancel) -> Path:
        self.calls.append(req)
        step = self.script.pop(0) if self.script else "ok"
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            return step(req, should_cancel)
        secs = step[1] if isinstance(step, tuple) else self.seconds
        return make_clip(req.out_dir / f"attempt-{req.attempt}.mp4", secs, self.size)

    def plan(self, req, should_cancel) -> list[dict]:
        self.calls.append(req)
        step = self.script.pop(0) if self.script else "ok"
        if isinstance(step, BaseException):
            raise step
        return [{"action": f"{req.prompt} (part {i})"} for i in range(1, req.scene_count + 1)]


# ---- web app side: fake worker + fake Google behind one MockTransport ---------
class FakeWorker:
    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.submits: list[dict] = []
        self.cancelled: list[str] = []
        self.submit_response: tuple[int, dict] | None = None
        self.gemini: dict = {"state": "connected", "model": "Veo"}
        self.status_errors = 0  # next N status calls return 503
        self.video_bytes: bytes | None = None
        self.clips: dict[str, bytes] = {}  # per-job video bytes, overriding video_bytes
        self.paused: dict | None = None  # /health "account_paused"
        self.seq = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != "Bearer worker-key-123456789012345":
            return httpx.Response(401, json={"detail": "Missing or wrong API key."})
        path = request.url.path
        if request.method == "POST" and path in ("/videos", "/plans"):
            ctype = request.headers.get("content-type", "")
            self.submits.append({"multipart": ctype.startswith("multipart"), "body": request.content[:200], "path": path,
                                 "json": json.loads(request.content) if ctype.startswith("application/json") else None,
                                 "images": request.content.count(b'name="images"')})
            if self.submit_response:
                code, body = self.submit_response
                return httpx.Response(code, json=body)
            self.seq += 1
            jid = f"job{self.seq}"
            self.jobs[jid] = {"status": "queued", "error": None}
            return httpx.Response(200, json={"job_id": jid})
        m = re.fullmatch(r"/videos/(\w+)", path)
        if m and request.method == "GET":
            if self.status_errors:
                self.status_errors -= 1
                return httpx.Response(503, json={"detail": "busy"})
            job = self.jobs.get(m.group(1))
            if not job:
                return httpx.Response(404, json={"detail": "No such job."})
            body = {"status": job["status"], "error": job["error"], "video_url": None, "final_prompt": "p"}
            if job["status"] == "done" and "scenes" in job:
                body["scenes"] = job["scenes"]
            elif job["status"] == "done":
                body["video_url"] = f"/videos/{m.group(1)}/file"
            return httpx.Response(200, json=body)
        if m and request.method == "DELETE":
            self.cancelled.append(m.group(1))
            return httpx.Response(200, json={"ok": True})
        m = re.fullmatch(r"/videos/(\w+)/file", path)
        if m:
            return httpx.Response(200, content=self.clips.get(m.group(1), self.video_bytes or b""))
        if path == "/health":
            return httpx.Response(200, json={"ok": True, "account_paused": self.paused,
                                             "login_window_open": self.gemini.get("login_window_open", False),
                                             "gemini": self.gemini})
        if path == "/gemini/resume" and request.method == "POST":
            self.resumed = getattr(self, "resumed", 0) + 1
            self.paused = None
            return httpx.Response(200, json=self.gemini)
        if path == "/gemini/status":
            return httpx.Response(200, json=self.gemini)
        return httpx.Response(404, json={"detail": "nope"})


class FakeGoogle:
    def __init__(self):
        self.files: dict[str, dict] = {}  # id → {name, parents, mime, data, trashed}
        self.sessions: dict[str, dict] = {}
        self.revoked: list[str] = []
        self.upload_failures: list[int] = []  # status codes for the next upload PUTs
        self.quota_full = False
        self.corrupt_md5 = False
        self.seq = 0
        self.token_calls = 0

    def _id(self) -> str:
        self.seq += 1
        return f"f{self.seq}"

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        host, path = url.host, url.path
        if host == "oauth2.googleapis.com" and path == "/token":
            form = parse_qs(request.content.decode())
            self.token_calls += 1
            if form.get("grant_type") == ["authorization_code"]:
                if form.get("code") == ["no-drive-code"]:  # user unticked Drive on the consent screen
                    return httpx.Response(200, json={"access_token": "at-1", "refresh_token": "rt-nodrive",
                                                     "expires_in": 3600, "scope": "openid email"})
                if form.get("code") != ["good-code"]:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(200, json={"access_token": "at-1", "refresh_token": "rt-secret",
                                                 "expires_in": 3600,
                                                 "scope": "openid email https://www.googleapis.com/auth/drive.file"})
            if form.get("refresh_token") == ["rt-secret"]:
                return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
            return httpx.Response(400, json={"error": "invalid_grant"})
        if host == "oauth2.googleapis.com" and path == "/revoke":
            self.revoked.append(parse_qs(request.content.decode())["token"][0])
            return httpx.Response(200)
        if host == "openidconnect.googleapis.com":
            return httpx.Response(200, json={"email": "owner@example.com"})
        if request.headers.get("authorization") != "Bearer at-1" and "upload_id" not in str(url):
            return httpx.Response(401, json={"error": {"message": "auth"}})
        if host == "www.googleapis.com":
            return self._drive(request)
        return httpx.Response(404)

    def _drive(self, request: httpx.Request) -> httpx.Response:
        url, path, q = request.url, request.url.path, dict(request.url.params)
        if path == "/upload/drive/v3/files" and request.method == "POST":
            if self.quota_full:
                return httpx.Response(403, json={"error": {"message": "full", "errors": [{"reason": "storageQuotaExceeded"}]}})
            meta = json.loads(request.content)
            sid = f"s{len(self.sessions) + 1}"
            self.sessions[sid] = meta
            return httpx.Response(200, headers={"location": f"https://www.googleapis.com/upload/drive/v3/files?upload_id={sid}"})
        if path == "/upload/drive/v3/files" and request.method == "PUT":
            if self.upload_failures:
                return httpx.Response(self.upload_failures.pop(0), json={"error": {"message": "backend"}})
            meta = self.sessions[q["upload_id"]]
            fid = self._id()
            data = request.read()
            self.files[fid] = {"name": meta["name"], "parents": meta["parents"], "mime": "video/mp4", "data": data,
                               "trashed": False}
            return httpx.Response(200, json={"id": fid})
        if path == "/drive/v3/files" and request.method == "GET":
            query = q.get("q", "")
            out = []
            for fid, f in self.files.items():
                if f["trashed"] or f["mime"] != "application/vnd.google-apps.folder":
                    continue
                m = re.search(r"name='((?:[^'\\]|\\.)*)'", query)
                if m and f["name"] != m.group(1).replace("\\'", "'"):
                    continue
                parent = re.search(r"'([^']+)' in parents", query).group(1)
                if parent not in f["parents"]:
                    continue
                out.append({"id": fid, "name": f["name"]})
            return httpx.Response(200, json={"files": out})
        if path == "/drive/v3/files" and request.method == "POST":
            meta = json.loads(request.content)
            fid = self._id()
            self.files[fid] = {"name": meta["name"], "parents": meta.get("parents", ["root"]),
                               "mime": meta["mimeType"], "data": b"", "trashed": False}
            return httpx.Response(200, json={"id": fid, "name": meta["name"]})
        m = re.fullmatch(r"/drive/v3/files/(\w+)", path)
        if m:
            f = self.files.get(m.group(1))
            if f is None:
                return httpx.Response(404, json={"error": {"message": "not found"}})
            if request.method == "PATCH":
                f.update(json.loads(request.content))
                return httpx.Response(200, json={"id": m.group(1)})
            if q.get("alt") == "media":
                data = f["data"]
                rng = request.headers.get("range")
                if rng:
                    a, _, b = rng.removeprefix("bytes=").partition("-")
                    start = int(a) if a else len(data) - int(b)
                    end = int(b) if (a and b) else len(data) - 1
                    if start >= len(data):
                        return httpx.Response(416)
                    chunk = data[start:end + 1]
                    return httpx.Response(206, content=chunk, headers={
                        "content-range": f"bytes {start}-{start + len(chunk) - 1}/{len(data)}",
                        "content-length": str(len(chunk))})
                return httpx.Response(200, content=data, headers={"content-length": str(len(data))})
            md5 = hashlib.md5(f["data"]).hexdigest()
            if self.corrupt_md5:
                md5 = "0" * 32
            return httpx.Response(200, json={"id": m.group(1), "name": f["name"], "size": str(len(f["data"])),
                                             "md5Checksum": md5, "trashed": f["trashed"]})
        return httpx.Response(404)


class FakeNet:
    """Routes by host: the worker on 127.0.0.1:8100, everything else to FakeGoogle."""

    def __init__(self):
        self.worker = FakeWorker()
        self.google = FakeGoogle()
        self.down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "127.0.0.1" and request.url.port == 8100:
            if self.down:
                raise httpx.ConnectError("refused", request=request)
            return self.worker.handle(request)
        return self.google.handle(request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def failing_once(exc: Exception):
    """Generator step that raises `exc`."""
    def step(req, should_cancel):
        raise exc
    return step


__all__ = ["PNG", "JPG", "make_clip", "TestPatternGenerator", "FakeNet", "GenerationError"]
