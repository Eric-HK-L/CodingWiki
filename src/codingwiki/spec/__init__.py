"""阶段二能力包：需求接入与 Spec 生成（架构文档 阶段2）。

spec 只依赖 domain / facts / retrieval / backends 等下层能力，
不 import generate/ 与 publish/。
"""

from codingwiki.spec.evidence import (
    WikiPageProvider,
    WikiPageRef,
    collect_evidence,
    save_evidence,
    validate_bundle,
)
from codingwiki.spec.extract import PROMPT_VERSION, extract_requirements
from codingwiki.spec.impact import build_impact, caller_ref
from codingwiki.spec.ingest import (
    build_doc_chunks,
    doc_sha256,
    find_quote_line,
    load_doc_chunks,
    load_extractions,
    locate_quote,
    make_doc_id,
    normalize_markdown,
    parse_sections,
    read_document,
    save_extraction,
)
from codingwiki.spec.pipeline import SpecOutcome, generate_spec
from codingwiki.spec.template import render_decisions_markdown, render_spec_markdown
from codingwiki.spec.validator import evidence_locators, validate_spec

__all__ = [
    "PROMPT_VERSION",
    "SpecOutcome",
    "WikiPageProvider",
    "WikiPageRef",
    "build_doc_chunks",
    "build_impact",
    "caller_ref",
    "collect_evidence",
    "doc_sha256",
    "evidence_locators",
    "extract_requirements",
    "find_quote_line",
    "generate_spec",
    "load_doc_chunks",
    "load_extractions",
    "locate_quote",
    "make_doc_id",
    "normalize_markdown",
    "parse_sections",
    "read_document",
    "render_decisions_markdown",
    "render_spec_markdown",
    "save_evidence",
    "save_extraction",
    "validate_bundle",
    "validate_spec",
]
