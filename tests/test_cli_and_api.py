from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from chantier.api.app import create_app
from chantier.cli.main import main


@pytest.fixture
def configured(env, monkeypatch):
    env.configure({"local": ("good_agent.py", "local")}, {"implement/implement": ["local"]})
    env.write_spec("spec_001_parser", "Parser")
    monkeypatch.setenv("CHANTIER_HOME", str(env.home))
    monkeypatch.setattr("chantier.cli.main.console.width", 240)
    return env


def test_cli_walkthrough(configured, capsys):
    assert main(["sync"]) == 0
    assert "demo/spec_001_parser" in capsys.readouterr().out
    assert main(["attention"]) == 0
    assert "spec waiting for approval" in capsys.readouterr().out
    assert main(["approve", "demo/spec_001_parser"]) == 0
    assert main(["tick", "--wait"]) == 0
    assert "→ demo/spec_001_parser" in capsys.readouterr().out
    assert main(["board"]) == 0
    assert "To merge (1)" in capsys.readouterr().out
    assert main(["show", "demo/spec_001_parser"]) == 0
    assert "succeeded" in capsys.readouterr().out
    assert main(["metrics"]) == 0
    assert "estimated savings" in capsys.readouterr().out
    assert main(["approve", "demo/spec_001_parser"]) == 0  # merge gate
    assert main(["board"]) == 0
    assert "Done (1)" in capsys.readouterr().out


def test_cli_reports_errors_without_traceback(configured, capsys):
    assert main(["approve", "demo/nope"]) == 1
    assert "unknown ticket" in capsys.readouterr().out


def test_init_writes_an_example_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CHANTIER_HOME", str(tmp_path / "h"))
    assert main(["init"]) == 0
    assert "executors:" in (tmp_path / "h" / "config.yaml").read_text()


def test_api_board_and_gates(configured):
    from chantier.bootstrap import build

    app = build(configured.home)
    client = TestClient(create_app(app))
    app.board.sync_project("demo")
    assert client.get("/").status_code == 200
    tickets = client.get("/api/tickets").json()
    assert [t["stage"] for t in tickets] == ["draft"]
    assert client.post("/api/tickets/demo/spec_001_parser/approve").json()["stage"] == "ready"
    assert client.post("/api/tickets/demo/spec_001_parser/approve").status_code == 409
    assert client.get("/api/tickets/demo/missing").status_code == 404
    detail = client.get("/api/tickets/demo/spec_001_parser").json()
    assert [x["to"] for x in detail["transitions"]] == ["ready"]
    assert client.get("/api/metrics").json()["savings"]["local_runs"] == 0
    assert client.get("/api/attention").json() == []
