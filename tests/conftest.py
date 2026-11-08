"""Shared test helpers and fixtures.

Tests run against the fictional demo archive (``email_archive_explorer.demo``),
generated once per session into a temporary directory. Tests that need a real
Outlook PST (no library can write one) are opt-in: set ``EAE_TEST_PST`` to the
path of any ``.pst`` file to run them.
"""

import os
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from email_archive_explorer.demo import build_messages, write_demo


def local_client(app, token: bool = True) -> TestClient:
    """A TestClient that looks like the real browser UI: loopback Host header and
    (optionally) the per-process session token that index.html hands the UI."""
    client = TestClient(app, base_url="http://127.0.0.1")
    if token:
        client.headers["X-EAE-Token"] = app.state.token
    return client


@pytest.fixture(scope="session")
def demo(tmp_path_factory):
    """``{"eml": <EML tree>, "mbox": <MBOX file>}`` of the demo archive."""
    return write_demo(tmp_path_factory.mktemp("demo"))


@pytest.fixture(scope="session")
def demo_eml(demo) -> Path:
    return demo["eml"]


@pytest.fixture(scope="session")
def demo_mbox(demo) -> Path:
    return demo["mbox"]


@pytest.fixture(scope="session")
def demo_counts():
    """Expected totals for the demo EML tree, derived from the generator."""
    eml, mbox = build_messages()
    return {
        "messages": len(eml),
        "attachments": sum(len(m.attachments) for m in eml),
        "folders": {m.folder for m in eml},
        "mbox_messages": len(mbox),
    }


def pst_from_env() -> Path | None:
    p = os.environ.get("EAE_TEST_PST")
    return Path(p) if p and Path(p).is_file() else None


@pytest.fixture(scope="session")
def real_pst() -> Path:
    """A real Outlook PST for driver-level tests; skipped unless EAE_TEST_PST is set."""
    p = pst_from_env()
    if p is None:
        pytest.skip("set EAE_TEST_PST=/path/to/archive.pst to run PST tests")
    return p
