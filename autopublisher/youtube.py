"""Minimal YouTube Data API v3 client, stdlib-only (no google-api-python-client).

Auth is a long-lived OAuth refresh token (obtained once with scripts/authorize.py,
stored in Secrets Manager). Uploads use the resumable protocol and stream from any
file-like reader, so S3 bodies can be piped straight through without touching disk.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = (
    "https://www.googleapis.com/upload/youtube/v3/videos"
    "?uploadType=resumable&part=snippet,status"
)
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"

CHUNK_SIZE = 8 * 1024 * 1024  # must be a multiple of 256 KiB
DEFAULT_CATEGORY = "22"  # People & Blogs


class YouTubeError(RuntimeError):
    pass


class YouTubeAuthError(YouTubeError):
    """Refresh token invalid or revoked: an owner must re-authorize (recoverable state)."""


class YouTubeQuotaError(YouTubeError):
    """Daily quota exhausted: surface it, do not retry until the reset."""


THUMBNAILS_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"


def classify(status: int, body: bytes) -> type[YouTubeError]:
    text = body[:2000].decode("utf-8", "replace")
    if status in (401,) or "invalid_grant" in text or "invalid_token" in text:
        return YouTubeAuthError
    if status == 403 and ("quotaExceeded" in text or "dailyLimitExceeded" in text or "uploadLimitExceeded" in text):
        return YouTubeQuotaError
    return YouTubeError


class YouTubeClient:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self._access_token = ""

    # -- HTTP plumbing ------------------------------------------------------

    def _http(self, method: str, url: str, data: bytes | None,
              headers: dict) -> tuple[int, dict, bytes]:
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as e:
            # 308 Resume Incomplete is the expected keep-going signal of the
            # resumable protocol; urllib surfaces it as an error for PUT.
            return e.code, dict(e.headers), e.read()

    def _refresh_access_token(self) -> None:
        data = urllib.parse.urlencode({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
        }).encode()
        status, _, body = self._http(
            "POST", TOKEN_URL, data,
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        if status != 200:
            raise classify(status, body)(f"token refresh failed ({status}): {body[:300]!r}")
        self._access_token = json.loads(body)["access_token"]

    def _call(self, method: str, url: str, data: bytes | None = None,
              headers: dict | None = None) -> tuple[int, dict, bytes]:
        if not self._access_token:
            self._refresh_access_token()
        merged = {"Authorization": f"Bearer {self._access_token}", **(headers or {})}
        status, resp_headers, body = self._http(method, url, data, merged)
        if status == 401:  # expired mid-run: refresh once and retry
            self._refresh_access_token()
            merged["Authorization"] = f"Bearer {self._access_token}"
            status, resp_headers, body = self._http(method, url, data, merged)
        return status, resp_headers, body

    # -- API ----------------------------------------------------------------

    @staticmethod
    def build_body(metadata: dict, privacy: str, publish_at: str = "") -> dict:
        """videos.insert body: snippet and status from reviewed metadata."""
        snippet = {
            "title": metadata["title"],
            "description": metadata.get("description", ""),
            "tags": metadata.get("tags", []),
            "categoryId": metadata.get("category_id", DEFAULT_CATEGORY),
        }
        if metadata.get("default_language"):
            snippet["defaultLanguage"] = metadata["default_language"]
            snippet["defaultAudioLanguage"] = metadata["default_language"]
        status = {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": bool(metadata.get("made_for_kids", False)),
            "containsSyntheticMedia": bool(metadata.get("contains_synthetic_media", False)),
        }
        if publish_at:
            if privacy != "private":
                raise YouTubeError("publishAt requires privacyStatus=private")
            status["publishAt"] = publish_at
        return {"snippet": snippet, "status": status}

    def upload(self, reader, size: int, *, metadata: dict, privacy: str = "private",
               publish_at: str = "", notify_subscribers: bool = False,
               on_progress=None) -> str:
        """Resumable upload streamed from `reader` (needs .read(n)). Returns video id."""
        body = json.dumps(self.build_body(metadata, privacy, publish_at)).encode()
        url = UPLOAD_URL + f"&notifySubscribers={'true' if notify_subscribers else 'false'}"
        status, headers, resp = self._call(
            "POST", url, body,
            {
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": "video/mp4",
                "X-Upload-Content-Length": str(size),
            },
        )
        if status != 200:
            raise classify(status, resp)(f"upload init failed ({status}): {resp[:300]!r}")
        session_url = headers.get("Location") or headers.get("location")
        if not session_url:
            raise YouTubeError("upload init returned no session Location")

        offset = 0
        while offset < size:
            chunk = reader.read(min(CHUNK_SIZE, size - offset))
            if not chunk:
                raise YouTubeError(f"source ended early at byte {offset} of {size}")
            end = offset + len(chunk) - 1
            status, _, body = self._call(
                "PUT", session_url, chunk,
                {
                    "Content-Type": "video/mp4",
                    "Content-Range": f"bytes {offset}-{end}/{size}",
                },
            )
            if status in (200, 201):
                return json.loads(body)["id"]
            if status != 308:
                raise classify(status, body)(f"chunk upload failed ({status}): {body[:300]!r}")
            offset = end + 1
            if on_progress:
                on_progress(offset, size)
        raise YouTubeError("upload finished without a completion response")

    def get_video(self, video_id: str) -> dict:
        status, _, resp = self._call("GET", f"{VIDEOS_URL}?part=status,snippet,processingDetails&id={video_id}")
        if status != 200:
            raise classify(status, resp)(f"videos.list failed ({status}): {resp[:300]!r}")
        items = json.loads(resp).get("items", [])
        if not items:
            raise YouTubeError(f"video {video_id} not found on the channel")
        return items[0]

    def update_status(self, video_id: str, privacy: str, publish_at: str = "",
                      made_for_kids: bool = False, synthetic: bool = False) -> None:
        status_body = {"privacyStatus": privacy, "selfDeclaredMadeForKids": made_for_kids,
                       "containsSyntheticMedia": synthetic}
        if publish_at:
            if privacy != "private":
                raise YouTubeError("publishAt requires privacyStatus=private")
            status_body["publishAt"] = publish_at
        body = json.dumps({"id": video_id, "status": status_body}).encode()
        status, _, resp = self._call(
            "PUT", f"{VIDEOS_URL}?part=status", body,
            {"Content-Type": "application/json; charset=UTF-8"},
        )
        if status != 200:
            raise classify(status, resp)(f"videos.update({video_id}) failed ({status}): {resp[:300]!r}")

    def set_thumbnail(self, video_id: str, image: bytes, content_type: str = "image/jpeg") -> None:
        status, _, resp = self._call(
            "POST", f"{THUMBNAILS_URL}?videoId={video_id}", image, {"Content-Type": content_type},
        )
        if status != 200:
            raise classify(status, resp)(f"thumbnails.set({video_id}) failed ({status}): {resp[:300]!r}")
