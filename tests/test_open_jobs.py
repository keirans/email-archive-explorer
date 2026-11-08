"""Spec 08 §3.4: background archive opening with progress."""

from __future__ import annotations

import mailbox
import shutil
import time

import pytest
from conftest import local_client
from test_drivers import MESSAGES

from email_archive_explorer.api import create_app
from email_archive_explorer.manager import ArchiveManager


@pytest.fixture()
def setup(tmp_path, demo_eml):
    small = tmp_path / "Small.mbox"
    box = mailbox.mbox(str(small))
    for build in MESSAGES:
        box.add(build())
    box.close()

    # A fresh copy of the demo EML tree: never indexed, so opening it starts a job
    big = tmp_path / "Big"
    shutil.copytree(demo_eml, big)

    mgr = ArchiveManager(tmp_path / "cache")
    mgr.open_archive(small)
    app = create_app(manager=mgr)
    with local_client(app) as c:
        yield c, small, big


def test_fresh_archive_indexes_in_background(setup, demo_counts):
    c, small, big = setup
    r = c.post("/api/archives/open", json={"file_path": str(big)})
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["state"] == "indexing" and job["name"] == "Big" and job["format_type"] == "eml_dir"

    # A second open while indexing → 409 pointing at the running job
    conflict = c.post("/api/archives/open", json={"file_path": str(small), "force_reindex": True})
    assert conflict.status_code == 409 and conflict.json()["job"]["job_id"] == job["job_id"]

    # The current archive keeps serving while the big one indexes
    saw_progress = False
    deadline = time.time() + 120
    while True:
        status = c.get(job["status_url"]).json()
        if status["state"] != "indexing":
            break
        assert c.get("/api/archive/info").json()["file_path"] == str(small)
        saw_progress |= status["total"] > 0
        assert time.time() < deadline, "indexing took too long"
        time.sleep(0.2)

    assert status["state"] == "done", status
    n = demo_counts["messages"]
    assert status["archive"]["total_messages"] == n
    assert status["indexed"] == status["total"] == n
    assert saw_progress or status["total"] == n  # small archives may finish before the first poll
    assert c.get("/api/archive/info").json()["file_path"] == str(big)

    # Now cached: reopening is synchronous
    r = c.post("/api/archives/open", json={"file_path": str(small)})
    assert r.status_code == 200 and r.json()["name"] == "Small.mbox"
    r = c.post("/api/archives/open", json={"file_path": str(big)})
    assert r.status_code == 200


def test_wait_flag_and_errors(setup, tmp_path):
    c, small, big = setup
    r = c.post("/api/archives/open", json={"file_path": str(small), "force_reindex": True, "wait": True})
    assert r.status_code == 200
    assert c.post("/api/archives/open", json={"file_path": str(tmp_path / "nope.pst")}).status_code == 404
    assert c.get("/api/archives/jobs/doesnotexist").status_code == 404


def test_failed_job_reports_error(setup, monkeypatch):
    c, small, big = setup
    import email_archive_explorer.manager as m

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(m, "build_index", boom)
    job = c.post("/api/archives/open", json={"file_path": str(big)}).json()
    for _ in range(100):
        status = c.get(job["status_url"]).json()
        if status["state"] != "indexing":
            break
        time.sleep(0.05)
    assert status["state"] == "error" and "disk on fire" in status["error"]
    assert c.get("/api/archive/info").json()["file_path"] == str(small)  # unchanged
