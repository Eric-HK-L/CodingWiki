"""页面校验器（计划文档 9.6.3、9.8；架构文档 v0.4 13.3）。

不调用大模型，只做确定性检查。规则分五组：
格式 / 结构 / 证据 / 语义 / 内容。任一 error 即 failed。
证据匹配用空白规范化精确匹配（4.5.2：行号只作提示，片段才是依据）。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from codingwiki.domain.facts import RepoFacts
from codingwiki.domain.front_matter import parse_front_matter
from codingwiki.domain.wiki import (
    PageMeta,
    PageResult,
    TaskMeta,
    VerifyIssue,
    VerifyReport,
)
from codingwiki.errors import InputError

VERIFIER_VERSION = "verify-v1"

_FUZZY_WORDS = ("大概", "应该是", "也许", "据说")
_EVIDENCE_REF = re.compile(r"\[(E\d+)\]")
_INTERNAL_LINK = re.compile(
    r"\[[^\]]*\]\(((?:module|interface|flow|config|build|test|overview|home|glossary|howto|adr|file)/[^)\s]+)\)"
)
_BODY_MIN_CHARS = 200
_BODY_MAX_CHARS = 8000


class PageVerifier:
    """对单个页面做机械校验。需要事实库与工作树文本。"""

    def __init__(
        self,
        facts: RepoFacts,
        workdir: Path,
        known_pages: set[str] | None = None,
    ) -> None:
        self._facts = facts
        self._workdir = workdir.resolve()
        self._known_pages = known_pages or set()
        self._symbol_names = {s.name for f in facts.files for s in f.symbols}
        self._symbol_names |= {s.qualified_name for f in facts.files for s in f.symbols}
        self._file_texts: dict[str, str] = {}

    def verify(self, page_text: str, result_text: str, meta: TaskMeta) -> VerifyReport:
        issues: list[VerifyIssue] = []

        result = self._parse_result(result_text, issues)
        page_meta, body = self._parse_page(page_text, issues)

        if result is not None:
            issues += self._check_result_consistency(result, meta)
            issues += self._check_evidence_closure(page_text, result, meta)
            issues += self._check_evidence_against_snapshot(result)
            issues += self._check_symbols_exist(result)
        if page_meta is not None:
            issues += self._check_meta_consistency(page_meta, meta)
        if body is not None:
            issues += self._check_content(body)
        return self._report(meta.page_id, issues, page_text, result_text)

    # ---------------------------------------------------------------- 格式与结构

    def _parse_result(self, result_text: str, issues: list[VerifyIssue]) -> PageResult | None:
        try:
            return PageResult.model_validate_json(result_text)
        except ValueError as exc:
            issues.append(
                VerifyIssue(
                    rule="result_json_invalid",
                    message=f"result.json 不符合契约: {exc}",
                    suggestion="按 schema 重写 result.json，只含 page_id/snapshot/claims/"
                    "evidence/unknowns/questions",
                )
            )
            return None

    def _parse_page(
        self, page_text: str, issues: list[VerifyIssue]
    ) -> tuple[PageMeta | None, str | None]:
        if not page_text.strip():
            issues.append(VerifyIssue(rule="page_empty", message="page.md 不存在或为空"))
            return None, None
        try:
            meta_obj, body = parse_front_matter(page_text)
        except InputError as exc:
            issues.append(
                VerifyIssue(
                    rule="front_matter_invalid",
                    message=str(exc),
                    suggestion="补齐 front matter 必填字段：id/type/title/snapshot/status/"
                    "coverage_level/evidence_count/generated_by/prompt_version/format_version",
                )
            )
            return None, None
        if not body.strip():
            issues.append(VerifyIssue(rule="page_body_empty", message="页面正文为空"))
            return meta_obj, None
        return meta_obj, body

    def _check_result_consistency(self, result: PageResult, meta: TaskMeta) -> list[VerifyIssue]:
        issues: list[VerifyIssue] = []
        if result.page_id != meta.page_id:
            issues.append(
                VerifyIssue(
                    rule="page_id_mismatch",
                    message=f"result.page_id={result.page_id} 与任务 {meta.page_id} 不一致",
                    suggestion=f"把 page_id 改为 {meta.page_id}",
                )
            )
        if result.snapshot != meta.snapshot_id:
            issues.append(
                VerifyIssue(
                    rule="snapshot_mismatch",
                    message=f"result.snapshot={result.snapshot} 与任务 {meta.snapshot_id} 不一致",
                    suggestion=f"把 snapshot 改为 {meta.snapshot_id}",
                )
            )
        return issues

    def _check_meta_consistency(self, page_meta: PageMeta, meta: TaskMeta) -> list[VerifyIssue]:
        issues: list[VerifyIssue] = []
        if page_meta.id != meta.page_id:
            issues.append(
                VerifyIssue(
                    rule="front_matter_id_mismatch",
                    message=f"front matter id={page_meta.id} 与任务 {meta.page_id} 不一致",
                    suggestion=f"把 id 改为 {meta.page_id}",
                )
            )
        if page_meta.type is not meta.page_type:
            issues.append(
                VerifyIssue(
                    rule="front_matter_type_mismatch",
                    message=f"front matter type={page_meta.type} 与任务 {meta.page_type} 不一致",
                    suggestion=f"把 type 改为 {meta.page_type.value}",
                )
            )
        if page_meta.snapshot != meta.snapshot_id:
            issues.append(
                VerifyIssue(
                    rule="front_matter_snapshot_mismatch",
                    message=(
                        f"front matter snapshot={page_meta.snapshot} "
                        f"与任务 {meta.snapshot_id} 不一致"
                    ),
                    suggestion=f"把 snapshot 改为 {meta.snapshot_id}",
                )
            )
        return issues

    # ---------------------------------------------------------------- 证据

    def _check_evidence_closure(
        self, page_text: str, result: PageResult, meta: TaskMeta
    ) -> list[VerifyIssue]:
        issues: list[VerifyIssue] = []
        result_ids = {e.id for e in result.evidence}

        # 页面 [En] ⊆ result.evidence
        for ref in sorted(set(_EVIDENCE_REF.findall(page_text))):
            if ref not in result_ids:
                issues.append(
                    VerifyIssue(
                        rule="page_evidence_missing_in_result",
                        message=f"页面引用了 [{ref}]，但 result.json 的 evidence 中没有",
                        evidence_id=ref,
                        suggestion=f"在 result.json 的 evidence 中补 {ref}，或从页面删除该引用",
                    )
                )
        # claims.evidence ⊆ result.evidence
        for claim in result.claims:
            for ref in claim.evidence:
                if ref not in result_ids:
                    issues.append(
                        VerifyIssue(
                            rule="claim_evidence_missing",
                            message=f"结论 {claim.id} 引用了 result.json 中不存在的证据 {ref}",
                            claim_id=claim.id,
                            evidence_id=ref,
                            suggestion=f"把 {claim.id} 的 evidence 改为已声明的编号",
                        )
                    )
        # result.evidence ⊆ meta.evidence，且 quote 未被篡改
        meta_by_id = {e.id: e for e in meta.evidence}
        for ev in result.evidence:
            expected = meta_by_id.get(ev.id)
            if expected is None:
                issues.append(
                    VerifyIssue(
                        rule="evidence_not_in_task",
                        message=f"证据 {ev.id} 不在任务包 meta.json 中",
                        evidence_id=ev.id,
                        suggestion="只能引用任务包提供的证据编号",
                    )
                )
                continue
            if _normalize_ws(ev.quote) != _normalize_ws(expected.quote):
                issues.append(
                    VerifyIssue(
                        rule="evidence_quote_tampered",
                        message=f"证据 {ev.id} 的 quote 与任务包不一致",
                        evidence_id=ev.id,
                        suggestion="quote 必须逐字复制任务包 snippets/ 中的片段",
                    )
                )
            if ev.file != expected.file:
                issues.append(
                    VerifyIssue(
                        rule="evidence_file_tampered",
                        message=f"证据 {ev.id} 的 file={ev.file} 与任务包 {expected.file} 不一致",
                        evidence_id=ev.id,
                        suggestion=f"把 file 改回 {expected.file}",
                    )
                )
        return issues

    def _check_evidence_against_snapshot(self, result: PageResult) -> list[VerifyIssue]:
        issues: list[VerifyIssue] = []
        for ev in result.evidence:
            text = self._load_file(ev.file)
            if text is None:
                issues.append(
                    VerifyIssue(
                        rule="evidence_file_missing",
                        message=f"证据 {ev.id} 的文件 {ev.file} 不在快照工作树中",
                        evidence_id=ev.id,
                        suggestion="确认文件路径与快照一致",
                    )
                )
                continue
            if _normalize_ws(ev.quote) not in _normalize_ws(text):
                issues.append(
                    VerifyIssue(
                        rule="evidence_quote_not_found",
                        message=f"证据 {ev.id} 的片段在 {ev.file} 中找不到（允许空白差异）",
                        evidence_id=ev.id,
                        suggestion=f"从 {ev.file} 第 {ev.line_hint} 行附近重新逐字摘录片段",
                    )
                )
        return issues

    def _check_symbols_exist(self, result: PageResult) -> list[VerifyIssue]:
        issues: list[VerifyIssue] = []
        for ev in result.evidence:
            if ev.symbol and ev.symbol not in self._symbol_names:
                issues.append(
                    VerifyIssue(
                        rule="symbol_not_in_facts",
                        message=f"证据 {ev.id} 引用的符号 {ev.symbol} 不在事实库中",
                        evidence_id=ev.id,
                        suggestion="删除该结论或标为未确认",
                    )
                )
        return issues

    # ---------------------------------------------------------------- 内容

    def _check_content(self, body: str) -> list[VerifyIssue]:
        issues: list[VerifyIssue] = []
        plain = _strip_code_and_evidence(body)
        for word in _FUZZY_WORDS:
            if word in plain:
                issues.append(
                    VerifyIssue(
                        rule="fuzzy_word",
                        message=f"正文出现无依据模糊词「{word}」",
                        suggestion="改为有证据支撑的确定表述，或写入未确认事项",
                    )
                )
        for link in sorted(set(_INTERNAL_LINK.findall(body))):
            if link not in self._known_pages:
                issues.append(
                    VerifyIssue(
                        rule="broken_internal_link",
                        message=f"内部链接 {link} 的目标页面不存在",
                        severity="warning",
                        suggestion="修正链接或先移除",
                    )
                )
        length = len(plain)
        if length < _BODY_MIN_CHARS or length > _BODY_MAX_CHARS:
            issues.append(
                VerifyIssue(
                    rule="body_length",
                    message=f"正文长度 {length} 不在 {_BODY_MIN_CHARS}~{_BODY_MAX_CHARS} 区间",
                    severity="warning",
                    suggestion="太长则拆分，太短则补证据",
                )
            )
        return issues

    # ---------------------------------------------------------------- 内部

    def _load_file(self, path: str) -> str | None:
        if path in self._file_texts:
            return self._file_texts[path]
        file_path = (self._workdir / path).resolve()
        if not str(file_path).startswith(str(self._workdir)):
            return None
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        self._file_texts[path] = text
        return text

    def _report(
        self, page_id: str, issues: list[VerifyIssue], page_text: str, result_text: str
    ) -> VerifyReport:
        failed = any(issue.severity == "error" for issue in issues)
        return VerifyReport(
            page_id=page_id,
            status="failed" if failed else "passed",
            issues=issues,
            verifier_version=VERIFIER_VERSION,
            page_sha256=_sha256(page_text),
            result_sha256=_sha256(result_text),
        )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _strip_code_and_evidence(body: str) -> str:
    """去掉代码块与出处块，只留叙述文字做模糊词/字数检查。"""
    text = re.sub(r"```[\s\S]*?```", " ", body)
    text = re.sub(r"^## 出处[\s\S]*$", " ", text, flags=re.MULTILINE)
    return text


__all__ = ["VERIFIER_VERSION", "PageVerifier"]
