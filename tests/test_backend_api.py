"""ApiBackend 测试：本地 stub HTTP 服务，不联网、不读真实密钥。"""

from __future__ import annotations

import json
import threading
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from codingwiki.backends import ApiBackend
from codingwiki.backends.protocol import BackendStatus, TaskPacket


class _Stub:
    """可编排响应的 OpenAI 兼容 stub：fail_times 次 500 后返回 content。"""

    def __init__(self, content: str, fail_times: int = 0, status: int = 200) -> None:
        self.content = content
        self.fail_times = fail_times
        self.status = status
        self.hits = 0
        self.last_body: dict[str, object] = {}

    def make_handler(self) -> type[BaseHTTPRequestHandler]:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server 约定
                stub.hits += 1
                if stub.status != 200:
                    self.send_response(stub.status)
                    self.end_headers()
                    self.wfile.write(b'{"error": "denied"}')
                    return
                if stub.hits <= stub.fail_times:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b'{"error": "boom"}')
                    return
                length = int(self.headers.get("Content-Length", "0"))
                stub.last_body = json.loads(self.rfile.read(length).decode("utf-8"))
                payload = {
                    "choices": [{"message": {"content": stub.content}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                }
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return

        return Handler


@pytest.fixture
def stub_server() -> Generator[_Stub, None, None]:
    stub = _Stub(content="{}")
    server = ThreadingHTTPServer(("127.0.0.1", 0), stub.make_handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stub.base_url = f"http://127.0.0.1:{server.server_address[1]}"  # type: ignore[attr-defined]
    try:
        yield stub
    finally:
        server.shutdown()
        server.server_close()


def _packet(tmp_path: Path, kind: str = "generate") -> TaskPacket:
    workspace = tmp_path / "job"
    (workspace / "context" / "snippets").mkdir(parents=True)
    (workspace / "output").mkdir()
    (workspace / "task.md").write_text("# 任务：写页面", encoding="utf-8")
    (workspace / "constraints.md").write_text("禁止空话", encoding="utf-8")
    (workspace / "meta.json").write_text(
        json.dumps(
            {
                "task_id": "t1",
                "page_id": "module/src",
                "snapshot_id": "sample@rev1",
                "evidence": [{"id": "E1", "file": "src/a.c", "symbol": "f", "line_hint": 3}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (workspace / "context" / "facts.json").write_text('{"symbols": []}', encoding="utf-8")
    (workspace / "context" / "snippets" / "E1.txt").write_text(
        "# src/a.c:3 f\nint f(void);", encoding="utf-8"
    )
    if kind == "ask":
        (workspace / "context" / "question.txt").write_text("Engine 是什么", encoding="utf-8")
        (workspace / "context" / "passages.json").write_text(
            json.dumps([{"sid": "S1", "reference": "src/a.c", "trust_tier": "code", "text": "x"}]),
            encoding="utf-8",
        )
    return TaskPacket(
        workspace=workspace,
        task_file=workspace / "task.md",
        output_dir=workspace / "output",
        timeout_s=30,
        metadata={"kind": kind},
    )


def _backend(stub: _Stub, key: str = "sk-test") -> ApiBackend:
    return (
        ApiBackend(
            base_url=stub.base_url,  # type: ignore[attr-defined]
            model="stub-model",
            api_key_file="",
            api_key_env="STUB_KEY",
            timeout_s=10,
            max_retries=2,
        )
        if key
        else ApiBackend(
            base_url=stub.base_url,  # type: ignore[attr-defined]
            model="stub-model",
            api_key_file="",
            api_key_env="MISSING_KEY",
            timeout_s=10,
            max_retries=2,
        )
    )


def test_generate_writes_page_and_merges_evidence(
    tmp_path: Path, stub_server: _Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_KEY", "sk-test")
    stub_server.content = json.dumps(
        {
            "page_markdown": "# src\n\n> 负责文件级调度。\n\n函数 `f` 定义于 src/a.c。[E1]\n",
            "result": {"claims": [{"id": "C1", "text": "…[E1]", "evidence": ["E1"]}]},
        },
        ensure_ascii=False,
    )
    packet = _packet(tmp_path)
    result = _backend(stub_server).run(packet)

    assert result.status is BackendStatus.SUCCEEDED
    assert result.tokens_in == 100 and result.tokens_out == 50
    page = (packet.output_dir / "page.md").read_text(encoding="utf-8")
    assert "负责文件级调度" in page
    merged = json.loads((packet.output_dir / "result.json").read_text(encoding="utf-8"))
    assert merged["page_id"] == "module/src"  # 来自 meta，不由模型决定
    assert merged["snapshot"] == "sample@rev1"
    assert merged["evidence"][0]["id"] == "E1"
    # 请求里包含源码片段与约束
    sent = json.dumps(stub_server.last_body, ensure_ascii=False)
    assert "int f(void)" in sent
    assert "禁止空话" in sent


def test_ask_writes_answer(
    tmp_path: Path, stub_server: _Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_KEY", "sk-test")
    stub_server.content = json.dumps(
        {
            "answer_markdown": "Engine 是核心类。[S1]",
            "result": {
                "question": "Engine 是什么",
                "answer_type": "answered",
                "citations": ["S1"],
                "confidence": "medium",
            },
        },
        ensure_ascii=False,
    )
    packet = _packet(tmp_path, kind="ask")
    result = _backend(stub_server).run(packet)

    assert result.status is BackendStatus.SUCCEEDED
    assert (packet.output_dir / "answer.md").is_file()
    assert "S1" in (packet.output_dir / "answer.md").read_text(encoding="utf-8")


def test_invalid_json_maps_to_invalid_output(
    tmp_path: Path, stub_server: _Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_KEY", "sk-test")
    stub_server.content = "抱歉，我无法按要求输出 JSON。"
    result = _backend(stub_server).run(_packet(tmp_path))

    assert result.status is BackendStatus.INVALID_OUTPUT
    assert "JSON" in result.error


def test_retries_on_server_error(
    tmp_path: Path, stub_server: _Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_KEY", "sk-test")
    stub_server.fail_times = 1
    stub_server.content = json.dumps(
        {"page_markdown": "# ok", "result": {"claims": [{"id": "C1", "evidence": ["E1"]}]}}
    )
    result = _backend(stub_server).run(_packet(tmp_path))

    assert result.status is BackendStatus.SUCCEEDED
    assert stub_server.hits == 2  # 第一次 500，第二次成功


def test_auth_failure_is_reported_without_key_leak(
    tmp_path: Path, stub_server: _Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_KEY", "sk-test")
    stub_server.status = 401
    result = _backend(stub_server).run(_packet(tmp_path))

    assert result.status is BackendStatus.FAILED
    assert "鉴权失败" in result.error
    assert "sk-test" not in result.error


def test_missing_key_fails_fast(tmp_path: Path, stub_server: _Stub) -> None:
    result = _backend(stub_server, key="").run(_packet(tmp_path))

    assert result.status is BackendStatus.FAILED
    assert "密钥" in result.error
    assert stub_server.hits == 0  # 没有密钥就不该发请求


def test_key_file_takes_priority(
    tmp_path: Path, stub_server: _Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STUB_KEY", raising=False)
    key_file = tmp_path / "key.txt"
    key_file.write_text("sk-from-file\n", encoding="utf-8")
    stub_server.content = json.dumps(
        {"page_markdown": "# ok", "result": {"claims": [{"id": "C1", "evidence": ["E1"]}]}}
    )
    backend = ApiBackend(
        base_url=stub_server.base_url,  # type: ignore[attr-defined]
        model="stub-model",
        api_key_file=str(key_file),
        api_key_env="STUB_KEY",
        timeout_s=10,
        max_retries=1,
    )
    result = backend.run(_packet(tmp_path))

    assert result.status is BackendStatus.SUCCEEDED
