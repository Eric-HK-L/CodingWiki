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

__all__ = [
    "PROMPT_VERSION",
    "WikiPageProvider",
    "WikiPageRef",
    "build_doc_chunks",
    "build_impact",
    "caller_ref",
    "collect_evidence",
    "doc_sha256",
    "extract_requirements",
    "find_quote_line",
    "load_doc_chunks",
    "load_extractions",
    "locate_quote",
    "make_doc_id",
    "normalize_markdown",
    "parse_sections",
    "read_document",
    "save_evidence",
    "save_extraction",
    "validate_bundle",
]
