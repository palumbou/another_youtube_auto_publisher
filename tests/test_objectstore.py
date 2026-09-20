import pytest

from autopublisher.objectstore import FilesystemObjectStore, UnsafeKeyError, check_key


@pytest.mark.parametrize("key", ["../x", "a/../../b", "/abs", "a\\b", "a/./b", "x\x00y", ""])
def test_unsafe_keys_are_rejected(key):
    with pytest.raises(UnsafeKeyError):
        check_key(key)


def test_filesystem_store_round_trip_and_version(tmp_path):
    store = FilesystemObjectStore(tmp_path)
    info = store.put_bytes("incoming/p/j/READY", b"")
    assert store.head("incoming/p/j/READY") == info
    v1 = store.put_bytes("incoming/p/j/source/master.mp4", b"one").version_id
    v2 = store.put_bytes("incoming/p/j/source/master.mp4", b"two").version_id
    assert v1 != v2
    with pytest.raises(FileNotFoundError):
        store.get_bytes("incoming/p/j/source/master.mp4", v1)
    assert store.get_bytes("incoming/p/j/source/master.mp4", v2) == b"two"
    assert [o.key for o in store.list("incoming/p/j/")] == [
        "incoming/p/j/READY", "incoming/p/j/source/master.mp4"]
    assert store.sha256("incoming/p/j/source/master.mp4").startswith("3fc4ccfe")


def test_filesystem_store_cannot_escape_root(tmp_path):
    store = FilesystemObjectStore(tmp_path / "bucket")
    with pytest.raises(UnsafeKeyError):
        store.head("../outside")
