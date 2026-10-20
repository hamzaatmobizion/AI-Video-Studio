"""HTTP client for the studio worker's /videos contract. The app polls; the worker never calls back."""
import os
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from app.interfaces import GeneratorError, JobStatus, WorkerHealth
from app.media import MAX_VIDEO_BYTES, video_kind

_UNREACHABLE = "The video worker isn't reachable. Is it running? (./run.sh starts it.)"


def _detail(r: httpx.Response) -> str:
    try:
        body = r.json()
        msg = body.get("detail") or body.get("error")
        if isinstance(msg, str) and msg:
            return msg
    except Exception:
        pass
    return f"The video worker answered with HTTP {r.status_code}."


class WorkerClient:
    def __init__(self, base_url: str, api_key: str, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self._auth = {"Authorization": f"Bearer {api_key}"}
        self._http = httpx.Client(transport=transport, timeout=httpx.Timeout(30.0, read=120.0))

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            r = self._http.request(method, self.base_url + path, headers=self._auth, **kw)
        except httpx.HTTPError as e:
            raise GeneratorError(_UNREACHABLE, final=False) from e
        if r.status_code >= 500:
            raise GeneratorError(_detail(r), final=False)
        return r

    def submit(self, prompt: str, aspect: str, ref: str, images: list[Path]) -> str:
        data = {"prompt": prompt, "aspect": aspect, "ref": ref}
        if images:
            files = [("images", (p.name, p.read_bytes(), "application/octet-stream")) for p in images]
            r = self._request("POST", "/videos", data=data, files=files)
        else:
            r = self._request("POST", "/videos", json=data)
        if r.status_code >= 400:
            raise GeneratorError(_detail(r), final=True)
        job_id = r.json().get("job_id")
        if not job_id:
            raise GeneratorError("The video worker didn't return a job id.", final=True)
        return str(job_id)

    def plan(self, prompt: str, aspect: str, ref: str, scenes: int, cast: list[dict] | None = None,
             style: str = "", language: str = "") -> str:
        """Ask the worker to have Gemini write a `scenes`-scene script from `prompt`, using `cast`
        ([{name, appearance, outfit, voice, personality}]); returns a job id."""
        body = {"prompt": prompt, "aspect": aspect, "ref": ref, "scenes": scenes}
        body.update({k: v for k, v in (("cast", cast), ("style", style), ("language", language)) if v})
        r = self._request("POST", "/plans", json=body)
        if r.status_code >= 400:
            raise GeneratorError(_detail(r), final=True)
        job_id = r.json().get("job_id")
        if not job_id:
            raise GeneratorError("The video worker didn't return a job id.", final=True)
        return str(job_id)

    def check(self, job_id: str) -> JobStatus:
        r = self._request("GET", f"/videos/{job_id}")
        if r.status_code >= 400:
            raise GeneratorError(_detail(r), final=True)
        b = r.json()
        status = b.get("status")
        if status not in ("queued", "running", "done", "failed"):
            raise GeneratorError(f"The video worker sent an unknown status: {status!r}.", final=False)
        return JobStatus(status=status, error=b.get("error"), video_url=b.get("video_url"),
                         final_prompt=b.get("final_prompt"),
                         scenes=b.get("scenes") if isinstance(b.get("scenes"), list) else None)

    def download(self, video_url: str, dest: Path) -> int:
        url = urljoin(self.base_url + "/", video_url)
        mine, theirs = urlparse(self.base_url), urlparse(url)
        same_origin = (mine.scheme, mine.netloc) == (theirs.scheme, theirs.netloc)
        headers = self._auth if same_origin else {}
        part = dest.with_suffix(dest.suffix + ".part")
        size = 0
        try:
            with self._http.stream("GET", url, headers=headers) as r:
                if r.status_code >= 500:
                    raise GeneratorError(f"Download failed (HTTP {r.status_code}).", final=False)
                if r.status_code >= 400:
                    raise GeneratorError(f"Download failed (HTTP {r.status_code}).", final=True)
                with open(part, "wb") as f:
                    for chunk in r.iter_bytes():
                        size += len(chunk)
                        if size > MAX_VIDEO_BYTES:
                            raise GeneratorError("The video is larger than 500 MB.", final=True)
                        f.write(chunk)
        except httpx.HTTPError as e:
            part.unlink(missing_ok=True)
            raise GeneratorError("Couldn't download the video from the worker.", final=False) from e
        except GeneratorError:
            part.unlink(missing_ok=True)
            raise
        with open(part, "rb") as f:
            kind = video_kind(f.read(16))
        if kind is None:
            part.unlink(missing_ok=True)
            raise GeneratorError("The worker returned something that isn't an MP4/MOV/WebM video.", final=True)
        os.replace(part, dest)
        return size

    def cancel(self, job_id: str) -> None:
        try:
            self._http.delete(f"{self.base_url}/videos/{job_id}", headers=self._auth)
        except httpx.HTTPError:
            pass  # best effort

    def ping(self) -> WorkerHealth:
        try:
            r = self._http.get(self.base_url + "/health", headers=self._auth, timeout=5.0)
        except httpx.HTTPError:
            return WorkerHealth(False, _UNREACHABLE)
        if r.status_code == 401:
            return WorkerHealth(False, "The worker rejected the API key (WORKER_API_KEY must equal STUDIO_API_KEY).")
        if r.status_code != 200:
            return WorkerHealth(False, _detail(r))
        body = r.json()
        return WorkerHealth(True, "Connected", body)

    # ---- Gemini sign-in passthrough (Settings page) --------------------
    def gemini(self, method: str, path: str) -> dict:
        r = self._request(method, f"/gemini/{path}", timeout=180.0)
        if r.status_code >= 400:
            raise GeneratorError(_detail(r), final=True)
        return r.json()
