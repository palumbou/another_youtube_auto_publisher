from autopublisher.pipeline import ready_events_from_records


def test_sqs_batch_with_eventbridge_bodies():
    event = {"Records": [{"body": '{"time": "2026-09-20T10:00:00Z", "detail": {"bucket": {"name": "b"}, '
                                  '"object": {"key": "incoming/p/j/READY", "version-id": "v1"}}}'}]}
    events = ready_events_from_records(event)
    assert [e.key for e in events] == ["incoming/p/j/READY"]
    assert events[0].version_id == "v1"


def test_sqs_batch_with_s3_notification_bodies():
    event = {"Records": [{"body": '{"Records": [{"eventTime": "2026-09-20T10:00:00.000Z", '
                                  '"s3": {"bucket": {"name": "b"}, "object": {"key": "incoming/p/j/READY"}}}]}'}]}
    assert ready_events_from_records(event)[0].bucket == "b"


def test_bare_eventbridge_event():
    event = {"time": "2026-09-20T10:00:00Z", "detail": {"bucket": {"name": "b"}, "object": {"key": "x/READY"}}}
    assert len(ready_events_from_records(event)) == 1


def test_unknown_shape_yields_nothing():
    assert ready_events_from_records({"action": "finalize"}) == []


def test_sqs_partial_batch_failure_reporting(monkeypatch):
    from autopublisher import pipeline

    class Boom:
        def handle_ready(self, ready):
            raise RuntimeError("store down")

    monkeypatch.setattr(pipeline, "_stores", lambda: (None, None))
    monkeypatch.setattr(pipeline, "Ingestor", lambda *a, **k: Boom())
    monkeypatch.setenv("BUCKET", "b")
    event = {"Records": [{"messageId": "m1", "body": '{"detail": {"bucket": {"name": "b"}, "object": {"key": "incoming/p/j/READY"}}}'}]}
    out = pipeline.ingest(event, settings=__import__("tests.helpers", fromlist=["settings"]).settings())
    assert out["batchItemFailures"] == [{"itemIdentifier": "m1"}]
    assert out["results"][0]["status"] == "ERROR"
