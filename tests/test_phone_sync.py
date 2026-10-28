import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from transfer.phone_sync import SyncError, sync, status, verify_session


def make_session(folder: Path, sid: str, takes=(("take-001.wav", b"RIFF" + b"a" * 100),), manifest=True, corrupt=None):
    d = folder / "sessions" / sid
    d.mkdir(parents=True)
    meta = []
    for name, data in takes:
        (d / name).write_bytes(data)
        meta.append({"file": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                     "status": "kept", "duration_ms": 30000, "task_id": "calm-01"})
    if corrupt:
        (d / corrupt).write_bytes(b"changed in transit")
    if manifest:
        (d / "manifest.json").write_text(json.dumps({"session": sid, "takes": meta}))


class FakePhone:
    """Stands in for adb: a folder laid out like the app's files directory."""
    def __init__(self, files: Path, devices=("ABC123",), fail_pull=()):
        self.files, self._devices, self.fail_pull, self.pulls = files, list(devices), set(fail_pull), 0

    def devices(self): return self._devices
    def list_sessions(self): return sorted(p.name for p in (self.files / "sessions").iterdir())
    def has_manifest(self, sid): return (self.files / "sessions" / sid / "manifest.json").exists()

    def pull_session(self, sid, dest):
        self.pulls += 1
        if sid in self.fail_pull:   # cable pulled half way: a partial copy is left behind
            dest.mkdir(parents=True); (dest / "take-001.wav").write_bytes(b"RIFF")
            raise SyncError("the phone stopped responding")
        shutil.copytree(self.files / "sessions" / sid, dest)

    def push_receipt(self, sid, src):
        (self.files / "receipts").mkdir(exist_ok=True)
        dest = self.files / "receipts" / f"{sid}.json"
        dest.unlink(missing_ok=True)          # like adb push: replaces the phone's copy
        shutil.copyfile(src, dest)


@pytest.fixture
def setup(tmp_path):
    phone_files, ds = tmp_path / "phone", tmp_path / "ds"
    ds.mkdir()
    return phone_files, ds


def test_copy_verify_receipt_and_readonly(setup):
    phone_files, ds = setup
    make_session(phone_files, "20261101-090000", takes=(("take-001.wav", b"RIFF" + b"x" * 500), ("take-002.mp4", b"\0\0\0 ftypmp42" * 40)))
    r = sync(FakePhone(phone_files), ds)
    assert r["copied"] == ["20261101-090000"] and not r["failed"]
    final = ds / "raw/phone/20261101-090000"
    assert not os.access(final / "take-001.wav", os.W_OK)
    receipt = json.loads((phone_files / "receipts/20261101-090000.json").read_text())
    assert set(receipt["verified"]) == {"take-001.wav", "take-002.mp4"}
    assert status(ds)["sessions"] == 1 and status(ds)["kept_minutes"] == 1.0


def test_corrupted_file_is_rejected_and_not_receipted(setup):
    phone_files, ds = setup
    make_session(phone_files, "20261101-090000", corrupt="take-001.wav")
    r = sync(FakePhone(phone_files), ds)
    assert "fingerprint" in r["failed"]["20261101-090000"] and not r["copied"]
    assert not (ds / "raw/phone/20261101-090000").exists()
    assert not (phone_files / "receipts").exists()


def test_interrupted_copy_resumes_cleanly(setup):
    phone_files, ds = setup
    make_session(phone_files, "20261101-090000")
    make_session(phone_files, "20261101-180000")
    first = sync(FakePhone(phone_files, fail_pull={"20261101-180000"}), ds)
    assert first["copied"] == ["20261101-090000"] and "20261101-180000" in first["failed"]
    p = FakePhone(phone_files)
    second = sync(p, ds)                   # cable back in: only the missing session is copied
    assert second["copied"] == ["20261101-180000"] and second["already_had"] == ["20261101-090000"]
    assert p.pulls == 1
    assert not list((ds / "staging/phone").glob("*.partial"))


def test_unfinished_sessions_wait(setup):
    phone_files, ds = setup
    make_session(phone_files, "20261101-090000", manifest=False)
    assert sync(FakePhone(phone_files), ds)["unfinished"] == ["20261101-090000"]


def test_no_phone_or_two_phones(setup):
    phone_files, ds = setup
    make_session(phone_files, "20261101-090000")
    with pytest.raises(SyncError, match="No phone"):
        sync(FakePhone(phone_files, devices=()), ds)
    with pytest.raises(SyncError, match="More than one"):
        sync(FakePhone(phone_files, devices=("a", "b")), ds)


def test_malicious_file_names_rejected(tmp_path):
    d = tmp_path / "s"; d.mkdir()
    (d / "manifest.json").write_text(json.dumps({"takes": [{"file": "../../etc/x.wav", "bytes": 1, "sha256": "0"}]}))
    with pytest.raises(SyncError, match="unexpected"):
        verify_session(d)
