"""wiki generate --backend api 端到端测试（本地 stub HTTP，不联网、不读真实密钥）。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "facts" / "sample"

_PAGE = """
# 页面概览

> 一句话职责：把仓库的目录、入口与依赖整理成可下钻的阅读路径。

## 导航

- 项目总览、架构、模块、流程、接口与配置页都可从本页进入。[E1]
- 模块页回答「负责什么、入口在哪、依赖谁」。[E1]

## 仓库画像

当前仓库包含多个目录级模块，事实层提供文件、符号、调用、构建、配置与测试六类信息。[E1]
所有统计都来自同一快照，页面之间的数字可以互相核对。[E1]

## 项目画像

项目按目录组织实现，模块通过公开入口协作，示例与测试从入口驱动整条链路。[E1]
阅读时先看规模与分层，再看模块职责，最后沿流程页核对调用顺序。[E1]

## 架构分层

| 层 | 目录/模块 | 职责 |
|---|---|---|
| 系统页 | home / overview / architecture | 全局导航与约束 [E1] |
| 模块层 | 目录级模块 | 维护实现与公开入口 [E1] |
| 流程层 | flow 页 | 串起入口到结果 [E1] |

## 核心模块

模块表按符号规模排序；流入高的模块通常被多个上层流程复用。[E1]
模块之间只通过公开入口协作，避免内部数据结构跨层泄漏。[E1]

## 分层

先分系统页、模块页与流程页，再在模块内按文件组织实现。[E1]
每一层都能在出处段找到对应的 file:line。[E1]

## 模块关系

| 模块 | 流入 | 流出 |
|---|---|---|
| 核心模块 | 多次 | 少量 |
| 上层模块 | 少量 | 多次 |

调用方向由解析到的调用边统计，跨模块调用会同时计入流入与流出。[E1]

## 关键约束

- 公开入口只在头文件声明，实现文件不暴露内部符号。[E1]
- 模块不得反向依赖上层示例，示例只调用公开入口。[E1]
- 构建目标与源文件的一致性由事实层质量报告给出。[E1]

## 典型调用链

入口 → 公开函数 → 模块内部实现 → 结果缓冲区。[E1]
流程页把这条链路拆成可核对的步骤，每一步都有出处。[E1]

## 职责边界

- 负责：把输入参数转换为可验证的输出结果，并通过公开函数暴露给调用方。[E1]
- 不负责：进程生命周期、外部网络与持久化存储。[E1]

## 关键数据结构

| 类型 | 位置 | 作用 |
|---|---|---|
| `Engine` | `src/engine.h` | 保存配置并执行核心计算。[E1] |

## 对外入口

| 入口 | 签名 | 使用场景 |
|---|---|---|
| `helper` | `int helper(void)` | 由上层流程调用。[E1] |

## 参数与返回

参数与返回值由头文件声明决定；调用方必须传入有效句柄与缓冲区。[E1]
实现侧在入口处完成边界检查，错误通过返回码向上传递。[E1]

## 调用方

入口被上层流程与测试驱动调用；改动签名会同时影响这些调用方。[E1]

## 依赖与影响

- 依赖：配置对象与公开头文件。[E1]
- 被谁使用：上层流程与测试入口通过公开函数调用本模块。[E1]

## 步骤

1. 读取配置并校验输入。[E1]
2. 调用核心函数完成计算。[E1]
3. 把结果写回调用方缓冲区并返回状态码。[E1]

## 触发入口

入口由 main 或测试驱动触发，调用条件由参数与配置共同决定。[E1]

## 配置项

| 配置 | 位置 | 作用 |
|---|---|---|
| `config.yaml` | 仓库根目录 | 控制运行参数 [E1] |

## 影响范围

配置变化会影响入口选择与计算路径，修改后需要重新跑测试。[E1]

## 构建系统

仓库使用 CMake 组织构建，目标声明与源文件清单由事实层核对。[E1]

## 主要目标

| 目标 | 类型 | 来源 |
|---|---|---|
| 库目标 | library | CMakeLists.txt [E1] |

## 测试框架

测试事实列出了用例、框架与位置，便于从入口反查覆盖。[E1]

## 主要测试

| 测试 | 框架 | 位置 |
|---|---|---|
| 示例测试 | pytest | tests/ [E1] |

## 术语

| 术语 | 代码位置 | 含义 |
|---|---|---|
| `Engine` | `src/engine.h` | 核心对象 [E1] |

## 出处

[E1]: src/engine.h:7 · `Engine` · 快照 sample@rev1
"""


class _Stub:
    def __init__(self) -> None:
        self.hits = 0
        self.last_body: dict[str, object] = {}

    def make_handler(self) -> type[BaseHTTPRequestHandler]:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server 约定
                stub.hits += 1
                length = int(self.headers.get("Content-Length", "0"))
                stub.last_body = json.loads(self.rfile.read(length).decode("utf-8"))
                payload = {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "page_markdown": _PAGE,
                                        "result": {
                                            "claims": [
                                                {
                                                    "id": "C1",
                                                    "text": "公开入口由头文件暴露。[E1]",
                                                    "evidence": ["E1"],
                                                }
                                            ]
                                        },
                                    },
                                    ensure_ascii=False,
                                )
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                }
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
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
    stub = _Stub()
    server = ThreadingHTTPServer(("127.0.0.1", 0), stub.make_handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stub.base_url = f"http://127.0.0.1:{server.server_address[1]}"  # type: ignore[attr-defined]
    try:
        yield stub
    finally:
        server.shutdown()
        server.server_close()


def _run(args: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "codingwiki", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, timeout=30)


def _prepare(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    origin = tmp_path / "origin"
    shutil.copytree(FIXTURES, origin)
    _git(origin, "init", "-b", "main")
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "fixture")

    home = tmp_path / ".cw"
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    (config_dir / "sample.yaml").write_text(
        f"repo_id: sample\nprovider: git\nurl: {origin}\nbranch: main\n", encoding="utf-8"
    )
    env = {**os.environ, "CODINGWIKI_HOME": str(home), "PYTHONPATH": str(REPO_ROOT / "src")}

    sync = _run(
        ["repo", "sync", "--repo", "sample", "--config-dir", str(config_dir)],
        cwd=tmp_path,
        env=env,
    )
    assert sync.returncode == 0, sync.stderr
    extract = _run(
        ["facts", "extract", "--repo", "sample", "--config-dir", str(config_dir)],
        cwd=tmp_path,
        env=env,
    )
    assert extract.returncode == 0, extract.stderr
    return config_dir, home, env


def _api_config(tmp_path: Path, stub: _Stub, key_file: Path) -> Path:
    path = tmp_path / "api.yaml"
    path.write_text(
        f"""
config_version: "1.0"
model:
  backend: api
  api:
    enabled: true
    base_url: "{stub.base_url}"  # type: ignore[attr-defined]
    model: "stub-model"
    api_key_file: "{key_file}"
    timeout_s: 10
    max_retries: 1
    temperature: 0
    max_tokens: 16384
embedding: {{}}
""",
        encoding="utf-8",
    )
    return path


def test_wiki_generate_api_backend_end_to_end(tmp_path: Path, stub_server: _Stub) -> None:
    config_dir, home, env = _prepare(tmp_path)
    key_file = tmp_path / "key.txt"
    key_file.write_text("sk-test\n", encoding="utf-8")
    app_config = _api_config(tmp_path, stub_server, key_file)

    result = _run(
        [
            "wiki",
            "generate",
            "--repo",
            "sample",
            "--config-dir",
            str(config_dir),
            "--backend",
            "api",
            "--app-config",
            str(app_config),
            "--max-tasks",
            "2",
        ],
        cwd=tmp_path,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "生成完成" in result.stdout
    assert "失败 0" in result.stdout
    assert stub_server.hits > 0
    drafts = home / "wiki" / "sample" / "drafts" / "pages"
    assert any(drafts.rglob("*.md"))
    # 请求里带任务包片段与硬约束，证明走的是真实任务包而非模板
    sent = json.dumps(stub_server.last_body, ensure_ascii=False)
    assert "源码片段" in sent
    assert "硬性约束" in sent


def test_wiki_generate_api_without_config_fails_with_actionable_message(
    tmp_path: Path,
) -> None:
    config_dir, _home, env = _prepare(tmp_path)
    default_config = REPO_ROOT / "configs" / "default.yaml"

    result = _run(
        [
            "wiki",
            "generate",
            "--repo",
            "sample",
            "--config-dir",
            str(config_dir),
            "--backend",
            "api",
            "--app-config",
            str(default_config),
            "--max-tasks",
            "1",
        ],
        cwd=tmp_path,
        env=env,
    )

    assert result.returncode == 2
    assert "model.api.enabled" in result.stderr
    assert "configs/api.local.yaml" in result.stderr


def test_wiki_generate_api_with_missing_key_file_fails(tmp_path: Path, stub_server: _Stub) -> None:
    config_dir, _home, env = _prepare(tmp_path)
    app_config = _api_config(tmp_path, stub_server, tmp_path / "missing-key.txt")

    result = _run(
        [
            "wiki",
            "generate",
            "--repo",
            "sample",
            "--config-dir",
            str(config_dir),
            "--backend",
            "api",
            "--app-config",
            str(app_config),
            "--max-tasks",
            "1",
        ],
        cwd=tmp_path,
        env=env,
    )

    assert result.returncode == 3
    assert "密钥文件不存在" in result.stderr
    assert stub_server.hits == 0
