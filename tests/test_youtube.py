import io
import json

import pytest

from autopublisher.youtube import (
    CHUNK_SIZE,
    YouTubeAuthError,
    YouTubeClient,
    YouTubeError,
    YouTubeQuotaError,
)


class FakeHTTP:
    """Scripted replacement for YouTubeClient._http, recording every call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, data, headers):
        self.calls.append({"method": method, "url": url, "data": data, "headers": headers})
        return self.responses.pop(0)


def make_client(fake):
    client = YouTubeClient("cid", "csecret", "rtoken")
    client._http = fake
    return client


TOKEN_OK = (200, {}, json.dumps({"access_token": "tok"}).encode())


class TestUpload:
    def test_single_chunk_upload(self):
        fake = FakeHTTP([
            TOKEN_OK,
            (200, {"Location": "https://upload/session"}, b""),
            (200, {}, json.dumps({"id": "vid123"}).encode()),
        ])
        client = make_client(fake)
        video_id = client.upload(io.BytesIO(b"x" * 100), 100,
                                 metadata={"title": "T", "description": "D", "tags": ["a"]}, privacy="public")
        assert video_id == "vid123"
        init, put = fake.calls[1], fake.calls[2]
        assert init["headers"]["X-Upload-Content-Length"] == "100"
        assert json.loads(init["data"])["status"]["privacyStatus"] == "public"
        assert put["headers"]["Content-Range"] == "bytes 0-99/100"

    def test_multi_chunk_upload_follows_308(self):
        size = CHUNK_SIZE + 10
        fake = FakeHTTP([
            TOKEN_OK,
            (200, {"Location": "https://upload/session"}, b""),
            (308, {}, b""),
            (201, {}, json.dumps({"id": "vid456"}).encode()),
        ])
        client = make_client(fake)
        video_id = client.upload(io.BytesIO(b"x" * size), size,
                                 metadata={"title": "T", "description": "", "tags": []}, privacy="unlisted")
        assert video_id == "vid456"
        ranges = [c["headers"]["Content-Range"] for c in fake.calls[2:]]
        assert ranges == [
            f"bytes 0-{CHUNK_SIZE - 1}/{size}",
            f"bytes {CHUNK_SIZE}-{size - 1}/{size}",
        ]

    def test_init_failure_raises(self):
        fake = FakeHTTP([TOKEN_OK, (403, {}, b"quota")])
        with pytest.raises(YouTubeError, match="upload init failed"):
            make_client(fake).upload(io.BytesIO(b""), 0, metadata={"title": "T"}, privacy="public")

    def test_truncated_source_raises(self):
        # the short first chunk is accepted (308); the empty read after it must raise
        fake = FakeHTTP([TOKEN_OK, (200, {"Location": "https://s"}, b""), (308, {}, b"")])
        with pytest.raises(YouTubeError, match="ended early"):
            make_client(fake).upload(io.BytesIO(b"xy"), 100, metadata={"title": "T"}, privacy="public")


class TestAuth:
    def test_401_triggers_one_refresh_and_retry(self):
        fake = FakeHTTP([
            TOKEN_OK,
            (401, {}, b""),
            (200, {}, json.dumps({"access_token": "tok2"}).encode()),
            (200, {}, b"{}"),
        ])
        client = make_client(fake)
        status, _, _ = client._call("GET", "https://api/x")
        assert status == 200
        assert fake.calls[-1]["headers"]["Authorization"] == "Bearer tok2"

    def test_refresh_failure_raises(self):
        fake = FakeHTTP([(400, {}, b"invalid_grant")])
        with pytest.raises(YouTubeError, match="token refresh failed"):
            make_client(fake)._call("GET", "https://api/x")


class TestSetPrivacy:
    def test_update_status_private_with_publish_at(self):
        fake = FakeHTTP([TOKEN_OK, (200, {}, b"{}")])
        make_client(fake).update_status("vid123", "private", publish_at="2099-01-01T10:00:00Z", synthetic=True)
        body = json.loads(fake.calls[1]["data"])
        assert body["status"] == {"privacyStatus": "private", "selfDeclaredMadeForKids": False,
                                  "containsSyntheticMedia": True, "publishAt": "2099-01-01T10:00:00Z"}

    def test_publish_at_requires_private(self):
        with pytest.raises(YouTubeError, match="publishAt"):
            YouTubeClient.build_body({"title": "T"}, "public", "2099-01-01T10:00:00Z")

    def test_auth_and_quota_errors_are_classified(self):
        fake = FakeHTTP([(400, {}, b'{"error": "invalid_grant"}')])
        with pytest.raises(YouTubeAuthError):
            make_client(fake).update_status("v", "private")
        fake = FakeHTTP([TOKEN_OK, (403, {}, b'{"error": {"errors": [{"reason": "quotaExceeded"}]}}')])
        with pytest.raises(YouTubeQuotaError):
            make_client(fake).update_status("v", "private")

    def test_get_video_and_thumbnail(self):
        fake = FakeHTTP([TOKEN_OK, (200, {}, json.dumps({"items": [{"id": "v", "status": {"privacyStatus": "private"}}]}).encode()),
                         (200, {}, b"{}")])
        client = make_client(fake)
        assert client.get_video("v")["status"]["privacyStatus"] == "private"
        client.set_thumbnail("v", b"\xff\xd8")
        assert fake.calls[2]["headers"]["Content-Type"] == "image/jpeg"
