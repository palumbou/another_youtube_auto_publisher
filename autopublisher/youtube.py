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
            raise YouTubeError(f"token refresh failed ({status}): {body[:500]!r}")
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

    def upload(self, reader, size: int, *, title: str, description: str,
               tags: list[str], privacy: str,
               category_id: str = DEFAULT_CATEGORY) -> str:
        """Resumable upload streamed from `reader` (needs .read(n)). Returns video id."""
        metadata = json.dumps({
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags,
                "categoryId": category_id,
            },
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
        }).encode()
        status, headers, body = self._call(
            "POST", UPLOAD_URL, metadata,
            {
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": "video/mp4",
                "X-Upload-Content-Length": str(size),
            },
        )
        if status != 200:
            raise YouTubeError(f"upload init failed ({status}): {body[:500]!r}")
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
                raise YouTubeError(f"chunk upload failed ({status}): {body[:500]!r}")
            offset = end + 1
        raise YouTubeError("upload finished without a completion response")

    def set_privacy(self, video_id: str, privacy: str) -> None:
        body = json.dumps({
            "id": video_id,
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
        }).encode()
        status, _, resp = self._call(
            "PUT", f"{VIDEOS_URL}?part=status", body,
            {"Content-Type": "application/json; charset=UTF-8"},
        )
        if status != 200:
            raise YouTubeError(f"set_privacy({video_id}) failed ({status}): {resp[:500]!r}")
