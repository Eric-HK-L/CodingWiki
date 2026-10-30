"""页面 front matter 编解码（纯字符串变换，无文件 I/O）。

front matter 由本系统生成、本系统校验，格式受控；
这里用 PyYAML safe_load/safe_dump，重复键等问题由 PageMeta 模型校验兜底。
"""

from __future__ import annotations

import yaml

from codingwiki.domain.wiki import PageMeta
from codingwiki.errors import InputError

_FENCE = "---"


def dump_front_matter(meta: PageMeta) -> str:
    """PageMeta → `---\\n...\\n---\\n` 文本块。"""
    data = meta.model_dump(mode="json", exclude_none=True)
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    return f"{_FENCE}\n{text}{_FENCE}\n"


def parse_front_matter(text: str) -> tuple[PageMeta, str]:
    """把页面文本拆成 (PageMeta, 正文)。缺 front matter 或解析失败抛 InputError。"""
    stripped = text.lstrip("\ufeff\n")
    if not stripped.startswith(_FENCE):
        raise InputError("页面缺少 front matter（--- 开头）")
    rest = stripped[len(_FENCE) :].lstrip("\n")
    end = rest.find(f"\n{_FENCE}")
    if end < 0:
        raise InputError("front matter 没有结束的 ---")
    header = rest[:end]
    body = rest[end + len(_FENCE) + 1 :].lstrip("\n")
    try:
        data = yaml.safe_load(header)
    except yaml.YAMLError as exc:
        raise InputError(f"front matter YAML 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise InputError("front matter 必须是 YAML 映射")
    try:
        meta = PageMeta.model_validate(data)
    except ValueError as exc:
        raise InputError(f"front matter 字段不合法: {exc}") from exc
    return meta, body


__all__ = ["dump_front_matter", "parse_front_matter"]
