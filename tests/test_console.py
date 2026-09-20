import pytest

from autopublisher import domain
from autopublisher.console import (
    Console,
    GatewayJwtAuthenticator,
    LocalAuthenticator,
    Request,
    ip_allowed,
    request_from_lambda,
    response_to_lambda,
)
from autopublisher.review import ReviewService
from tests import helpers
from tests.test_review import reviewed  # noqa: F401 - fixture reuse

LOCAL_IP = "127.0.0.1"


@pytest.fixture
def console(reviewed):  # noqa: F811
    settings = helpers.settings()
    auth = LocalAuthenticator(settings, password="pw-test")
    calls = []
    app = Console(settings, reviewed["jobs"], reviewed["store"], ReviewService(settings, reviewed["jobs"]), auth,
                  on_reprocess=lambda job, scope, reason: calls.append((job.job_id, scope, reason)))
    login = app.handle(Request("POST", "/login", form={"username": "owner", "password": "pw-test"}, source_ip=LOCAL_IP))
    assert login.status == 303
    cookie = login.cookies[0].split(";")[0].split("=", 1)[1]
    return {"app": app, "cookie": {"autopub_session": cookie}, "calls": calls, "jobs": reviewed["jobs"]}


def get(c, path, **kw):
    return c["app"].handle(Request("GET", path, cookies=c["cookie"], source_ip=LOCAL_IP, **kw))


def post(c, path, form=None, lists=None):
    job = c["jobs"].get_job(helpers.PROJECT, "qav-test-0001")
    form = {"version": str(job.version), **(form or {})}
    return c["app"].handle(Request("POST", path, form=form, form_lists=lists or {}, cookies=c["cookie"], source_ip=LOCAL_IP))


def test_login_required_and_wrong_password_rejected(reviewed):  # noqa: F811
    settings = helpers.settings()
    app = Console(settings, reviewed["jobs"], reviewed["store"], ReviewService(settings, reviewed["jobs"]),
                  LocalAuthenticator(settings, password="pw"))
    assert app.handle(Request("GET", "/", source_ip=LOCAL_IP)).status == 303
    assert app.handle(Request("POST", "/login", form={"username": "owner", "password": "nope"}, source_ip=LOCAL_IP)).status == 401
    assert app.handle(Request("POST", "/jobs/quiz-al-volo/qav-test-0001/approve-master", source_ip=LOCAL_IP)).status == 401


def test_ip_allowlist_fails_closed():
    assert ip_allowed("203.0.113.5", ("203.0.113.0/24",), local_mode=False)
    assert not ip_allowed("198.51.100.1", ("203.0.113.0/24",), local_mode=False)
    assert not ip_allowed("203.0.113.5", (), local_mode=False)
    assert ip_allowed("127.0.0.1", (), local_mode=True)
    assert not ip_allowed("10.0.0.9", (), local_mode=True)
    assert not ip_allowed("garbage", ("203.0.113.0/24",), local_mode=False)


def test_console_denies_foreign_ip(console):
    resp = console["app"].handle(Request("GET", "/", cookies=console["cookie"], source_ip="10.1.2.3"))
    assert resp.status == 403


def test_index_and_job_page_render(console):
    index = get(console, "/")
    assert index.status == 200 and b"qav-test-0001" in index.body and b"kill switch" in index.body.lower()
    job = get(console, "/jobs/quiz-al-volo/qav-test-0001")
    body = job.body.decode()
    assert "Approve master" in body and "short-01" in body and "Transcript" in body and "Reject and reprocess" in body
    assert "/media/" in body and "?t=" in body
    assert get(console, "/jobs/quiz-al-volo/missing").status == 404
    read = get(console, "/api/jobs/quiz-al-volo/qav-test-0001")
    assert read.status == 200 and b'"AWAITING_REVIEW"' in read.body


def test_media_route_needs_valid_token(console):
    body = get(console, "/jobs/quiz-al-volo/qav-test-0001").body.decode()
    import re
    match = re.search(r"src='(/media/[^']+)\?t=([^']+)'", body)
    assert match
    path, token = match.group(1), match.group(2)
    ok = console["app"].handle(Request("GET", path, query={"t": token}, source_ip=LOCAL_IP))
    assert ok.status == 200 and ok.content_type == "video/mp4" and len(ok.body) > 1000
    assert console["app"].handle(Request("GET", path, query={"t": "bad"}, source_ip=LOCAL_IP)).status == 403
    assert console["app"].handle(Request("GET", path, source_ip=LOCAL_IP)).status == 403


def test_full_review_flow_through_http(console):
    base = "/jobs/quiz-al-volo/qav-test-0001"
    # metadata edit with confirmation flags
    resp = post(console, f"{base}/metadata/master", form={"title": "Fiumi d'Italia: il più lungo", "hashtags": "#quiz #geografia",
                                                          "tags": "quiz, geografia", "owner_confirmed_audience": "on",
                                                          "owner_confirmed_audience_present": "1"})
    assert resp.status == 303, resp.body
    # reject with scope triggers reprocess callback
    resp = post(console, f"{base}/reject", form={"reason": "title still vague", "scope": "METADATA_ONLY"})
    assert resp.status == 303
    assert console["calls"] == [("qav-test-0001", "METADATA_ONLY", "title still vague")]
    assert console["jobs"].get_job(helpers.PROJECT, "qav-test-0001").state == domain.REPROCESS_REQUESTED
    # actions are refused while not in review
    assert post(console, f"{base}/approve-master").status == 400
    # missing reason
    job = console["jobs"].get_job(helpers.PROJECT, "qav-test-0001")
    job.state = domain.AWAITING_REVIEW
    console["jobs"].update_job(job, expected_version=job.version)
    assert post(console, f"{base}/reject", form={"reason": "", "scope": "FULL"}).status == 400
    # stale version -> conflict
    stale = {"version": "1", "title": "x", "hashtags": "#a"}
    resp = console["app"].handle(Request("POST", f"{base}/metadata/master", form=stale, cookies=console["cookie"], source_ip=LOCAL_IP))
    assert resp.status == 409
    # batch approve then schedule
    post(console, f"{base}/metadata/short-01", form={"owner_confirmed_audience": "on", "owner_confirmed_audience_present": "1"})
    resp = post(console, f"{base}/approve-batch", form={"include_master": "on"}, lists={"short_ids": ["short-01"]})
    assert resp.status == 303
    assert console["jobs"].get_job(helpers.PROJECT, "qav-test-0001").state == domain.APPROVED
    resp = post(console, f"{base}/schedule", form={"asset_id": "short-01", "publish_at": "2099-03-01T09:30"})
    assert resp.status == 303
    sched = console["jobs"].list_schedules()[0]
    assert sched.publish_at_utc == "2099-03-01T08:30:00Z" and sched.asset_id == "short-01"
    audit = get(console, f"{base}/audit")
    assert audit.status == 200 and b"BATCH_APPROVED" in audit.body and b"SCHEDULED" in audit.body
    assert b"127.0.0.1" in audit.body


def test_gateway_jwt_authenticator_fails_closed():
    auth = GatewayJwtAuthenticator(allowed_subjects=("owner",))
    assert auth.actor(Request("GET", "/")) is None
    assert auth.actor(Request("GET", "/", claims={"sub": "abc", "cognito:username": "owner"})) == "owner"
    assert auth.actor(Request("GET", "/", claims={"sub": "abc", "cognito:username": "intruder"})) is None


def test_lambda_adapters_round_trip():
    event = {"rawPath": "/jobs/p/j/reject", "requestContext": {"http": {"method": "POST", "sourceIp": "203.0.113.9"},
             "authorizer": {"jwt": {"claims": {"sub": "s", "cognito:username": "owner"}}}},
             "body": "reason=x&scope=FULL&short_ids=a&short_ids=b", "cookies": ["autopub_session=t"]}
    req = request_from_lambda(event)
    assert req.form["scope"] == "FULL" and req.form_lists["short_ids"] == ["a", "b"] and req.claims["sub"] == "s"
    from autopublisher.console import Response
    out = response_to_lambda(Response(200, b"\x00\x01", "video/mp4"))
    assert out["isBase64Encoded"] is True and out["body"] == "AAE="
    assert response_to_lambda(Response.json(200, {"a": 1}))["body"] == '{\n  "a": 1\n}'


def test_local_authenticator_refuses_non_local_settings():
    with pytest.raises(Exception, match="LOCAL_MODE"):
        LocalAuthenticator(helpers.settings(local_mode=False))
