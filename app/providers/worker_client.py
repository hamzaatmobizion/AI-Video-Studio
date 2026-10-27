"""HTTP client for the studio worker's /videos contract. The app polls; the worker never calls back."""
import json
import os
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from app.interfaces import GeneratorError, JobStatus, WorkerHealth
from app.media import MAX_IMAGE_BYTES, MAX_VIDEO_BYTES, image_kind, video_kind

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
             style: str = "", language: str = "", rules: str = "") -> str:
        """Ask the worker to have Gemini write a `scenes`-scene script from `prompt`, using `cast`
        ([{name, appearance, outfit, voice, personality}]) and the video type's `rules`; returns a job id."""
        body = {"prompt": prompt, "aspect": aspect, "ref": ref, "scenes": scenes}
        body.update({k: v for k, v in (("cast", cast), ("style", style), ("language", language), ("rules", rules))
                     if v})
        r = self._request("POST", "/plans", json=body)
        if r.status_code >= 400:
            raise GeneratorError(_detail(r), final=True)
        job_id = r.json().get("job_id")
        if not job_id:
            raise GeneratorError("The video worker didn't return a job id.", final=True)
        return str(job_id)

    def portrait(self, description: str, style: str, ref: str) -> str:
        """Ask the worker to have Gemini draw one character portrait of `description`; returns a job id."""
        r = self._request("POST", "/portraits", json={"description": description, "style": style, "ref": ref})
        if r.status_code >= 400:
            raise GeneratorError(_detail(r), final=True)
        job_id = r.json().get("job_id")
        if not job_id:
            raise GeneratorError("The video worker didn't return a job id.", final=True)
        return str(job_id)

    def _post_with_images(self, path: str, body: dict, images: list[Path], videos: list[Path] = (),
                          stills: list[Path] = ()) -> str:
        files = [("images", (p.name, p.read_bytes(), "application/octet-stream")) for p in images]
        files += [("videos", (p.name, p.read_bytes(), "video/mp4")) for p in videos]
        files += [("stills", (p.name, p.read_bytes(), "image/jpeg")) for p in stills]
        r = self._request("POST", path, data={"body": json.dumps(body)}, files=files or None)
        if r.status_code >= 400:
            raise GeneratorError(_detail(r), final=True)
        job_id = r.json().get("job_id")
        if not job_id:
            raise GeneratorError("The video worker didn't return a job id.", final=True)
        return str(job_id)

    def still(self, description: str, aspect: str, ref: str, images: list[Path], names: list[str],
              previous: bool) -> str:
        """Ask the worker to have Gemini draw a scene's first frame from the characters' pictures (`names`, in
        order) and, when `previous`, the previous shot's last frame after them; returns a job id."""
        return self._post_with_images("/stills", {"description": description, "aspect": aspect, "ref": ref,
                                                  "names": names, "previous": previous}, images)

    def review_clip(self, names: list[str], action: str, lines: list[dict], ref: str, pictures: list[Path],
                    clip: Path, previous: Path | None = None, outfits: dict[str, str] | None = None,
                    stills: list[Path] | None = None) -> str:
        """Ask the worker to have Gemini watch `clip` (sound included) and check it against the characters'
        `pictures` (one per name), the `previous` clip, the scene's `action` and its `lines` ([{name, line,
        position}]); returns a job id."""
        body = {"names": names, "previous": previous is not None, "action": action, "lines": lines, "ref": ref,
                "outfits": outfits or {}}
        return self._post_with_images("/checks", body, pictures, [*([previous] if previous else []), clip],
                                      stills or [])

    def check(self, job_id: str) -> JobStatus:
        r = self._request("GET", f"/videos/{job_id}")
        if r.status_code >= 400:
            raise GeneratorError(_detail(r), final=True)
        b = r.json()
        status = b.get("status")
        if status not in ("queued", "running", "done", "failed"):
            raise GeneratorError(f"The video worker sent an unknown status: {status!r}.", final=False)
        return JobStatus(status=status, error=b.get("error"), video_url=b.get("video_url"),
                         final_prompt=b.get("final_prompt"), image_url=b.get("image_url"),
                         scenes=b.get("scenes") if isinstance(b.get("scenes"), list) else None,
                         bible=b.get("bible") if isinstance(b.get("bible"), dict) else None,
                         check=b.get("check") if isinstance(b.get("check"), dict) else None)

    def download(self, video_url: str, dest: Path) -> int:
        part = dest.with_suffix(dest.suffix + ".part")
        size = self._fetch(video_url, part, MAX_VIDEO_BYTES, "video", "500 MB")
        with open(part, "rb") as f:
            kind = video_kind(f.read(16))
        if kind is None:
            part.unlink(missing_ok=True)
            raise GeneratorError("The worker returned something that isn't an MP4/MOV/WebM video.", final=True)
        os.replace(part, dest)
        return size

    def download_image(self, image_url: str, dest_stem: Path) -> Path:
        """Download a portrait to `dest_stem` + its real extension (.jpg/.png/.webp)."""
        part = dest_stem.with_suffix(".part")
        self._fetch(image_url, part, MAX_IMAGE_BYTES * 2, "image", "20 MB")
        with open(part, "rb") as f:
            kind = image_kind(f.read(16))
        if kind is None:
            part.unlink(missing_ok=True)
            raise GeneratorError("The worker returned something that isn't a JPG, PNG or WebP image.", final=True)
        dest = dest_stem.with_suffix(f".{kind}")
        os.replace(part, dest)
        return dest

    def _fetch(self, url: str, part: Path, limit: int, what: str, limit_text: str) -> int:
        url = urljoin(self.base_url + "/", url)
        mine, theirs = urlparse(self.base_url), urlparse(url)
        same_origin = (mine.scheme, mine.netloc) == (theirs.scheme, theirs.netloc)
        headers = self._auth if same_origin else {}
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
                        if size > limit:
                            raise GeneratorError(f"The {what} is larger than {limit_text}.", final=True)
                        f.write(chunk)
        except httpx.HTTPError as e:
            part.unlink(missing_ok=True)
            raise GeneratorError(f"Couldn't download the {what} from the worker.", final=False) from e
        except GeneratorError:
            part.unlink(missing_ok=True)
            raise
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
