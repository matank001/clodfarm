import os
import socket
import stat
import subprocess
import sys
import uuid

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="session")
def ddb_endpoint():
    """A DynamoDB endpoint: FARM_TEST_DYNAMODB (e.g. DynamoDB Local) or an in-process moto server."""
    if os.environ.get("FARM_TEST_DYNAMODB"):
        yield os.environ["FARM_TEST_DYNAMODB"]
        return
    from moto.server import ThreadedMotoServer
    port = _free_port()
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port)
    server.start()
    yield f"http://127.0.0.1:{port}"
    server.stop()


@pytest.fixture(params=["sqlite", "dynamodb"])
def backend(request):
    """Every store and farm test runs on both backends."""
    return request.param


@pytest.fixture
def env(tmp_path, backend, request, monkeypatch):
    """A clean farm environment: own table (or SQLite file), workspace, Claude home and a fake claude binary."""
    table = "t" + uuid.uuid4().hex[:10]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(f"#!/bin/sh\nexec {sys.executable} {HERE}/fake_claude.py \"$@\"\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    values = {
        "FARM_TABLE": table,
        "FARM_STORE": backend,
        "FARM_DB": str(tmp_path / "farm.db"),
        "AWS_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "FARM_WORKSPACE": str(tmp_path / "workspace"),
        "FARM_CLAUDE_BIN": str(fake),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-home"),
        "FAKE_CLAUDE_LOG": str(tmp_path / "claude.log"),
        "FARM_IDLE_SLEEP": "1",
        "FARM_PLANNER": "0",
        "FARM_PUSH": "0",
        "FARM_MAX_WORKERS": "3",
        "FARM_NAME": "test",
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    if backend == "dynamodb":
        values["FARM_DYNAMODB_ENDPOINT"] = request.getfixturevalue("ddb_endpoint")
    else:
        monkeypatch.delenv("FARM_DYNAMODB_ENDPOINT", raising=False)
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    for k in ("CLAUDE_CODE_OAUTH_TOKEN", "FARM_TASK_ID", "FARM_REPO_URL"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "claude-home").mkdir()
    return tmp_path


@pytest.fixture
def store(env):
    from claude_farm.config import load
    from claude_farm.store import Store
    s = Store.from_config(load())
    s.ensure_table()
    return s


def cli(*args, check=True, extra_env=None):
    """Run the CLI in a subprocess, as an agent would."""
    return subprocess.run([sys.executable, "-m", "claude_farm", *args], capture_output=True, text=True, check=check,
                          env={**os.environ, **(extra_env or {})})
