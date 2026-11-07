"""通用 API 模型后端（OpenAI 兼容 /chat/completions，架构文档 v0.7 §17.4）。

用标准库 urllib，不引入新依赖。职责：
- 把任务包（task.md + constraints.md + context/*）拼成一次对话请求；
- 要求模型返回 JSON（页面正文 + result.json 结构）；
- 解析、落盘 output/、返回统一 BackendResult；
- 密钥只从 api_key_file 或环境变量读取，绝不出现在日志与返回值里。

失败语义：密钥缺失/无效（401/403）→ EnvironmentIssue；网络与 5xx/429 重试后仍失败
→ BackendStatus.FAILED（由上层修复循环决定是否再试）。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from codingwiki.backends.protocol import BackendResult, BackendStatus, TaskPacket
from codingwiki.domain.front_matter import dump_front_matter
from codingwiki.domain.wiki import PageMeta, PageStatus, TaskMeta
from codingwiki.errors import EnvironmentIssue

BACKEND_NAME = "api"
_DEFAULT_PATH = "/chat/completions"
_RETRY_STATUS = {429, 500, 502, 503, 504}

_SYSTEM_PROMPT = """你是代码仓库文档撰写者。严格按任务包约束写作：
- 只使用给定的代码事实与源码片段，不编造；
- 每条结论后标注 [E编号]，编号必须存在于任务包证据列表；
- 输出必须是单个 JSON 对象，不要 Markdown 代码围栏之外的额外解释。
"""

_SPEC_SYSTEM_PROMPT = """你是需求分析助手，负责把需求文档拆成原子需求。
- 只搬运原文，不改写、不补充业务背景；
- 每条需求必须给出 source_quote，逐字取自文档；
- 原文没有的验收标准不要编；不明确处写入 questions。
"""

_SPEC_DRAFT_SYSTEM_PROMPT = """你是技术方案设计者，负责为已编号的需求草拟开发 Spec。
- 需求、编号、证据全部来自任务包，不得新增、改写或遗漏需求；
- 11 章编号与标题固定，没有内容也写「不适用」并给出原因，不得删章；
- 任务最多 3 个文件、最多 300 行，文件只能取 evidence 的 impact.files；
- 追溯矩阵每条需求恰好一行，code_location 必须来自证据的 path:line / symbol_id / wiki:page_id；
- 只输出单个 JSON 对象，不输出 T-/D- 编号（由系统生成）。
"""


class ApiBackend:
    """OpenAI 兼容后端。每次 run 对应一次请求（修复循环由上层驱动）。"""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key_file: str = "",
        api_key_env: str = "CODINGWIKI_API_KEY",
        timeout_s: int = 300,
        max_retries: int = 3,
        temperature: float = 0.2,
        max_tokens: int = 8192,
        api_path: str = _DEFAULT_PATH,
    ) -> None:
        if not base_url.strip():
            raise EnvironmentIssue("API 后端缺少 base_url；请在 configs/api.local.yaml 配置")
        if not model.strip():
            raise EnvironmentIssue("API 后端缺少 model；请在 configs/api.local.yaml 配置")
        self._base_url = base_url.rstrip("/")
        self._path = api_path if api_path.startswith("/") else f"/{api_path}"
        self._model = model
        self._api_key_file = api_key_file
        self._api_key_env = api_key_env
        self._timeout_s = timeout_s
        self._max_retries = max(1, max_retries)
        self._temperature = temperature
        self._max_tokens = max_tokens

    # ---------------------------------------------------------------- 入口

    def run(self, packet: TaskPacket) -> BackendResult:
        started = time.monotonic()
        try:
            kind = _task_kind(packet)
            request_body = _build_request(self, packet, kind)
        except OSError as exc:
            return BackendResult(
                status=BackendStatus.FAILED,
                duration_ms=_elapsed(started),
                model=self._model,
                error=f"任务包不可读: {exc}",
            )

        try:
            api_key = self._read_key()
            payload, usage = self._post(request_body, api_key)
        except EnvironmentIssue as exc:
            return BackendResult(
                status=BackendStatus.FAILED,
                duration_ms=_elapsed(started),
                model=self._model,
                error=str(exc),
            )
        except RuntimeError as exc:
            return BackendResult(
                status=BackendStatus.FAILED,
                duration_ms=_elapsed(started),
                model=self._model,
                error=str(exc),
            )

        try:
            content, finish_reason = _extract_content(payload)
            if finish_reason == "length":
                return BackendResult(
                    status=BackendStatus.INVALID_OUTPUT,
                    duration_ms=_elapsed(started),
                    model=self._model,
                    tokens_in=usage.get("prompt_tokens"),
                    tokens_out=usage.get("completion_tokens"),
                    error="模型输出被截断（max_tokens 用尽）；请调大 max_tokens 或缩小页面范围",
                )
            data = _parse_json_object(content)
            written = _write_outputs(packet, kind, data, self._model)
        except ValueError as exc:
            return BackendResult(
                status=BackendStatus.INVALID_OUTPUT,
                duration_ms=_elapsed(started),
                model=self._model,
                tokens_in=usage.get("prompt_tokens"),
                tokens_out=usage.get("completion_tokens"),
                error=f"模型输出不符合契约: {exc}",
            )

        return BackendResult(
            status=BackendStatus.SUCCEEDED,
            exit_code=0,
            output_files=written,
            duration_ms=_elapsed(started),
            tokens_in=usage.get("prompt_tokens"),
            tokens_out=usage.get("completion_tokens"),
            model=self._model,
        )

    # ---------------------------------------------------------------- HTTP

    def _read_key(self) -> str:
        if self._api_key_file:
            path = Path(self._api_key_file).expanduser()
            if not path.is_file():
                raise EnvironmentIssue(f"API 密钥文件不存在: {path}")
            key = path.read_text(encoding="utf-8").strip()
        else:
            key = os.environ.get(self._api_key_env, "").strip()
        if not key:
            raise EnvironmentIssue(
                f"未找到 API 密钥：请配置 api_key_file，或设置环境变量 {self._api_key_env}"
            )
        return key

    def _post(self, body: dict[str, Any], api_key: str) -> tuple[dict[str, Any], dict[str, int]]:
        url = self._base_url + self._path
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        last_error = ""
        for attempt in range(1, self._max_retries + 1):
            request = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                    raw = response.read().decode("utf-8")
                    return json.loads(raw), _usage_of(json.loads(raw))
            except urllib.error.HTTPError as exc:
                detail = _short(exc.read().decode("utf-8", errors="replace")) if exc.fp else ""
                if exc.code in (401, 403):
                    raise EnvironmentIssue(
                        f"API 鉴权失败({exc.code})：请检查 api_key_file 或 {self._api_key_env}"
                    ) from exc
                last_error = f"HTTP {exc.code}: {detail}"
                if exc.code not in _RETRY_STATUS:
                    break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < self._max_retries:
                time.sleep(min(2**attempt, 8))
        raise RuntimeError(f"API 调用失败（重试 {self._max_retries} 次）：{last_error}")


# ---------------------------------------------------------------- 任务包 → 请求


def _task_kind(packet: TaskPacket) -> str:
    if (packet.workspace / "context" / "question.txt").is_file():
        return "ask"
    meta = _read_meta(packet.workspace)
    if meta.get("task_type") == "spec_extract":
        return "spec_extract"
    if meta.get("task_type") == "spec_draft":
        return "spec_draft"
    return "generate"


def _build_request(backend: ApiBackend, packet: TaskPacket, kind: str) -> dict[str, Any]:
    task = _read(packet.task_file)
    constraints_path = packet.workspace / "constraints.md"
    constraints = _read(constraints_path) if constraints_path.is_file() else ""
    system = _SYSTEM_PROMPT
    if kind == "ask":
        question = _read(packet.workspace / "context" / "question.txt")
        passages = _read(packet.workspace / "context" / "passages.json")
        schema = (
            '{"answer_markdown": "...", "result": {"question": "...", "answer_type": '
            '"answered|partial|refused", "citations": ["S1"], "confidence": "high|medium|low", '
            '"unsupported": [], "followups": []}}'
        )
        user = (
            f"# 问题\n{question}\n\n# 资料(passages.json)\n{passages}\n\n# 输出 JSON 结构\n{schema}"
        )
    elif kind == "spec_extract":
        system = _SPEC_SYSTEM_PROMPT
        document = _read(packet.workspace / "context" / "document.md")
        sections = _read(packet.workspace / "context" / "sections.json")
        schema = (
            '{"requirements": [{"kind": "functional|non_functional", "text": "...", '
            '"priority": "must|should|may", "source_section": "...", "source_quote": "...", '
            '"acceptance": ["..."]}], "questions": [{"question": "...", "impact": "...", '
            '"owner": "..."}]}'
        )
        user = "\n\n".join(
            [
                f"# 任务\n{task}",
                f"# 需求文档（已规范化）\n{document}",
                f"# 章节骨架（sections.json）\n{sections}",
                f"# 输出 JSON 结构（严格遵守）\n{schema}",
            ]
        )
    elif kind == "spec_draft":
        system = _SPEC_DRAFT_SYSTEM_PROMPT
        document = _read(packet.workspace / "context" / "document.md")
        extract = _read(packet.workspace / "context" / "extract.json")
        evidence = _read(packet.workspace / "context" / "evidence.json")
        errors_path = packet.workspace / "errors.json"
        errors = _read(errors_path) if errors_path.is_file() else ""
        schema = (
            '{"sections": [{"number": 1, "title": "背景与目标", "body": "..."}], '
            '"tasks": [{"goal": "...", "files": ["a.cpp"], "estimated_lines": 120, '
            '"acceptance": ["..."], "depends_on_indexes": []}], '
            '"trace": [{"requirement_id": "R-001", "design_section": "5 方案设计", '
            '"code_location": "src/a.cpp:12", "test": "test_a", "task_index": 1}], '
            '"decisions": [{"title": "...", "context": "...", '
            '"candidates": [{"name": "方案A", "summary": "...", "pros": "...", "cons": "..."}], '
            '"choice": "方案A", "rationale": "...", "unique_justification": ""}]}'
        )
        parts = [
            f"# 任务\n{task}",
            f"# 需求文档（已规范化）\n{document}",
            f"# 已编号需求与开放问题（extract.json）\n{extract}",
            f"# 证据包与影响面（evidence.json）\n{evidence}",
        ]
        if errors:
            parts.append(f"# 上一轮校验失败原因（errors.json，必须逐条修正）\n{errors}")
        parts.append(f"# 输出 JSON 结构（严格遵守）\n{schema}")
        user = "\n\n".join(parts)
    else:
        context_parts = []
        for path in sorted((packet.workspace / "context").glob("*.json")):
            context_parts.append(f"# {path.name}\n{_read(path)}")
        snippets = _read_snippets(packet.workspace)
        if snippets:
            context_parts.append("# 源码片段（按证据编号）\n" + "\n\n".join(snippets))
        schema = (
            '{"page_markdown": "# 标题\\n\\n> 一句话职责…（只写正文，不要写 front matter）", '
            '"result": {"claims": [{"id": "C1", "text": "… [E1]", "evidence": ["E1"], '
            '"confidence": "high"}]}}'
        )
        user = "\n\n".join(
            [
                f"# 任务\n{task}",
                f"# 硬性约束\n{constraints}",
                *context_parts,
                f"# 输出 JSON 结构（严格遵守；evidence 由系统补全，不要输出）\n{schema}",
            ]
        )
    return {
        "model": backend._model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": backend._temperature,
        "max_tokens": backend._max_tokens,
        "response_format": {"type": "json_object"},
    }


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _read_snippets(workspace: Path, limit: int = 80, max_chars: int = 4000) -> list[str]:
    """读取 context/snippets/E*.txt；按证据编号排序，超限截断（控制上下文预算）。"""
    directory = workspace / "context" / "snippets"
    if not directory.is_dir():
        return []

    def order(path: Path) -> tuple[int, str]:
        stem = path.stem
        number = stem[1:] if stem.startswith("E") else ""
        return (int(number) if number.isdigit() else 10**9, stem)

    parts: list[str] = []
    for path in sorted(directory.glob("*.txt"), key=order)[:limit]:
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            text = text[:max_chars] + "\n…（截断）"
        parts.append(f"[{path.stem}]\n{text}")
    return parts


# ---------------------------------------------------------------- 响应解析


def _extract_content(payload: dict[str, Any]) -> tuple[str, str]:
    try:
        choice = payload["choices"][0]
        content = str(choice["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"响应缺少 choices[0].message.content: {_short(str(payload))}") from exc
    return content, str(choice.get("finish_reason") or "")


def _parse_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("未找到 JSON 对象")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("JSON 顶层必须是对象")
    return data


def _usage_of(payload: dict[str, Any]) -> dict[str, int]:
    usage = payload.get("usage") or {}
    result: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            result[key] = value
    return result


def _write_outputs(
    packet: TaskPacket, kind: str, data: dict[str, Any], model_name: str
) -> list[str]:
    if kind == "spec_extract":
        requirements = data.get("requirements")
        questions = data.get("questions", [])
        if not isinstance(requirements, list) or not isinstance(questions, list):
            raise ValueError("spec_extract 输出缺少 requirements 或 questions 数组")
        packet.output_dir.mkdir(parents=True, exist_ok=True)
        (packet.output_dir / "extract.json").write_text(
            json.dumps(
                {"requirements": requirements, "questions": questions},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return ["output/extract.json"]

    if kind == "spec_draft":
        draft: dict[str, Any] = {}
        for key in ("sections", "tasks", "trace", "decisions"):
            value = data.get(key, [])
            if not isinstance(value, list):
                raise ValueError(f"spec_draft 输出的 {key} 必须是数组")
            draft[key] = value
        packet.output_dir.mkdir(parents=True, exist_ok=True)
        (packet.output_dir / "draft.json").write_text(
            json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return ["output/draft.json"]

    if kind == "ask":
        answer = data.get("answer_markdown")
        result = data.get("result")
        if not isinstance(answer, str) or not isinstance(result, dict):
            raise ValueError("ask 输出缺少 answer_markdown 或 result")
        packet.output_dir.mkdir(parents=True, exist_ok=True)
        (packet.output_dir / "answer.md").write_text(answer, encoding="utf-8")
        (packet.output_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return ["output/answer.md", "output/result.json"]

    page = data.get("page_markdown")
    result = data.get("result")
    if not isinstance(page, str) or not isinstance(result, dict):
        raise ValueError("generate 输出缺少 page_markdown 或 result")
    claims = result.get("claims")
    if not isinstance(claims, list) or not claims:
        raise ValueError("result.claims 必须是非空数组")
    meta = _read_meta(packet.workspace)
    # 系统侧始终重写 front matter 与出处段：模型自带的（快照/行号常写错）一律剥离
    body = _strip_evidence_section(_strip_leading_front_matter(page))
    page = _with_front_matter(meta, model_name) + body
    page += "\n" + _evidence_block(meta)
    # page_id / snapshot / evidence 以任务包为权威，模型只补 claims，避免复制篡改
    merged = {
        "page_id": meta.get("page_id", ""),
        "snapshot": meta.get("snapshot_id", ""),
        "claims": claims,
        "evidence": meta.get("evidence", []),
    }
    packet.output_dir.mkdir(parents=True, exist_ok=True)
    (packet.output_dir / "page.md").write_text(page, encoding="utf-8")
    (packet.output_dir / "result.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return ["output/page.md", "output/result.json"]


def _strip_leading_front_matter(text: str) -> str:
    """去掉模型自带的 YAML front matter（若有），保留正文。"""
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return stripped
    end = stripped.find("\n---", 3)
    if end == -1:
        return stripped
    return stripped[end + 4 :].lstrip("\n")


def _strip_evidence_section(body: str) -> str:
    """去掉模型自带的出处段；出处由系统按 meta 重新生成，避免行号/快照写错。"""
    marker = body.rfind("\n## 出处")
    if marker == -1:
        return body
    return body[:marker].rstrip() + "\n"


def _evidence_block(meta: dict[str, Any]) -> str:
    evidence = meta.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return ""
    snapshot = str(meta.get("snapshot_id", ""))
    lines = ["## 出处", ""]
    for item in evidence:
        if not isinstance(item, dict):
            continue
        evidence_id = str(item.get("id", ""))
        file = str(item.get("file", ""))
        line = int(item.get("line_hint", 0) or 0)
        symbol = str(item.get("symbol", ""))
        suffix = f" · `{symbol}`" if symbol else ""
        lines.append(f"[{evidence_id}]: {file}:{line}{suffix} · 快照 {snapshot}")
    return "\n".join(lines) + "\n"


def _with_front_matter(meta: dict[str, Any], model_name: str) -> str:
    """front matter 由系统按任务包元数据生成，模型只负责正文。"""
    try:
        task = TaskMeta.model_validate(meta)
    except ValueError:
        return ""
    page_meta = PageMeta(
        id=task.page_id,
        type=task.page_type,
        title=task.title_hint or task.page_id,
        snapshot=task.snapshot_id,
        module=task.module,
        status=PageStatus.DRAFT,
        coverage_level=task.coverage_level,
        evidence_count=len(task.evidence),
        generated_by=f"api/{model_name}",
        prompt_version=task.prompt_version,
        tags=[task.page_type.value],
    )
    return dump_front_matter(page_meta) + "\n"


def _read_meta(workspace: Path) -> dict[str, Any]:
    path = workspace / "meta.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _short(text: str, limit: int = 200) -> str:
    clean = " ".join(text.split())
    return clean if len(clean) <= limit else clean[:limit] + "…"


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


__all__ = ["ApiBackend", "BACKEND_NAME"]
