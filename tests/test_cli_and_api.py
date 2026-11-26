from __future__ import annotations

import json
import sys

import pytest
from fastapi.testclient import TestClient

from chantier.api.app import create_app
from chantier.cli.main import _parser, main
from test_executors import _FakeOllamaServer


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


def test_cli_bench_parser_and_probe_only(tmp_path, monkeypatch, capsys):
    args = _parser().parse_args([
        "bench", "proj",
        "--models", "m1,m2",
        "--specs", "spec_001,spec_002",
        "--stack", "python",
        "--num-ctx", "16384",
        "--attempts", "3",
        "--limit", "4",
        "--probe-only",
        "--review",
        "--min-tok-s", "20",
        "--min-vram-ratio", "0.95",
        "--json",
    ])
    assert args.project == "proj"
    assert args.models == "m1,m2"
    assert args.specs == "spec_001,spec_002"
    assert args.stack == "python"
    assert args.num_ctx == 16384
    assert args.attempts == 3
    assert args.limit == 4
    assert args.probe_only is True
    assert args.review is True
    assert args.min_tok_s == 20.0
    assert args.min_vram_ratio == 0.95
    assert args.json is True

    monkeypatch.setenv("CHANTIER_HOME", str(tmp_path / "h"))
    monkeypatch.setattr("chantier.cli.main.console.width", 240)
    turns = [
        [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "1", "function": {"name": "write_file",
                                                 "arguments": {"path": "probe.txt", "content": "ok"}}},
                    ],
                },
                "done": True,
                "eval_count": 100,
                "eval_duration": 2_000_000_000,
            }
        ],
        [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "1", "function": {"name": "write_file",
                                                 "arguments": {"path": "probe.txt", "content": "ok"}}},
                    ],
                },
                "done": True,
                "eval_count": 100,
                "eval_duration": 2_000_000_000,
            }
        ],
    ]
    loaded = [{"name": "good:7b", "size": 1_000, "size_vram": 1_000}]
    with _FakeOllamaServer(turns, loaded_models=loaded, tags=["good:7b"]) as server:
        monkeypatch.setenv("OLLAMA_API_BASE", server.url)
        assert main(["bench", "--probe-only", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["mode"] == "probe"
        assert payload["recommended_model"] == "good:7b"
        assert payload["results"][0]["probe"]["tool_calling_ok"] is True

        assert main(["bench", "--probe-only"]) == 0
        out = capsys.readouterr().out
        assert "good:7b" in out
        assert "config.yaml" in out.lower() or "executors:" in out


def test_cli_bench_greenfield_stack_prompt_and_non_tty(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CHANTIER_HOME", str(tmp_path / "h"))
    monkeypatch.setattr("chantier.cli.main.console.width", 240)

    # Non-interactive without --stack exits cleanly with an actionable error.
    class _FakeStdin:
        def __init__(self, tty: bool) -> None:
            self._tty = tty

        def isatty(self) -> bool:
            return self._tty

    monkeypatch.setattr(sys, "stdin", _FakeStdin(False))
    assert main(["bench"]) == 1
    assert "--stack" in capsys.readouterr().out

    # Interactive TTY prompts the user for the stack and passes it to Benchmarker.
    called_configs = []

    class _FakeBenchmarker:
        def needs_greenfield_stack(self, project: str | None) -> bool:
            return project is None

        def run(self, cfg):
            from chantier.application.bench import BenchModelResult, BenchReport
            from chantier.application.ports import ModelProbeResult

            called_configs.append(cfg)
            return BenchReport(
                mode="greenfield",
                target=cfg.stack or "",
                num_ctx=cfg.num_ctx,
                results=[
                    BenchModelResult(
                        model="qwen3:30b",
                        probe=ModelProbeResult("qwen3:30b", True, True, 55.0, 1000, 1000, 1.0),
                        passed_count=1,
                        total_specs=1,
                        total_attempts=1,
                        wall_time_s=4.2,
                    )
                ],
                recommended_model="qwen3:30b",
                config_snippet="executors:\n  ollama:\n    model: qwen3:30b",
            )

    monkeypatch.setattr(sys, "stdin", _FakeStdin(True))
    monkeypatch.setattr("chantier.cli.main.build_benchmarker", lambda: _FakeBenchmarker())
    monkeypatch.setattr("builtins.input", lambda prompt="": "python")

    assert main(["bench"]) == 0
    out = capsys.readouterr().out
    assert called_configs[0].stack == "python"
    assert "qwen3:30b" in out
    assert "executors:" in out


def test_views_bench_does_not_truncate_long_model_names():
    from io import StringIO

    from rich.console import Console

    from chantier.application.bench import BenchModelResult, BenchReport
    from chantier.application.ports import ModelProbeResult
    from chantier.cli import views

    long_name = "hf.co/unsloth/Qwen3.6-35B-A3B-GGUF:UD-IQ3_XXS"
    report = BenchReport(
        mode="probe",
        target="ollama",
        num_ctx=32768,
        results=[
            BenchModelResult(
                model=long_name,
                probe=ModelProbeResult(long_name, True, True, 109.8, 13_900_000_000, 13_900_000_000, 1.0),
            )
        ],
        recommended_model=long_name,
    )
    buf = StringIO()
    Console(file=buf, width=100).print(views.bench(report))
    rendered = buf.getvalue()
    assert "…" not in rendered
    model_col = "".join(
        line.split("│")[2].strip()
        for line in rendered.splitlines()
        if line.startswith("│")
    )
    assert long_name in model_col
