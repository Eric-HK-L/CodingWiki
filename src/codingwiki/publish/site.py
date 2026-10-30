"""静态站点生成（架构文档 v0.7 §17.6）。

把发布版本的 Markdown 页面渲染成可离线打开的 HTML：
- index.html：导航树 + 仓库画像 + 搜索入口；
- pages/<page_id>.html：页面正文，内部链接改写为相对路径；
- source/<path>.html：出处回链的源码行号视图；
- style.css / search.js：无外部依赖，file:// 可直接打开。

不引入 Markdown 依赖：只实现 Wiki 用到的块级/行内语法（标题、段落、列表、表格、
代码块、引用、链接、行内代码、证据编号）。
"""

from __future__ import annotations

import html
import json
import posixpath
import re
import shutil
from collections.abc import Mapping
from pathlib import Path

from codingwiki.domain.front_matter import parse_front_matter
from codingwiki.domain.wiki import VersionIndex

_PAGE_TYPES = (
    "module",
    "flow",
    "interface",
    "config",
    "build",
    "test",
    "glossary",
)
_INTERNAL_PREFIX = re.compile(
    r"^(module|interface|flow|config|build|test|overview|home|architecture|glossary|file)/"
)
_EVIDENCE_LINE = re.compile(r"^\[(E\d+)\]:\s+(\S+):(\d+)(.*)$")
_EVIDENCE_REF = re.compile(r"\[(E\d+)\]")
_HEADING = re.compile(r"^(#{1,4})\s+(.+)$")
_LIST = re.compile(r"^(?:-|\*|\d+\.)\s+")
_TABLE_SEP = re.compile(r"^\|(?:\s*:?-+:?\s*\|)+\s*$")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_CODE_SPAN = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")

_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; font: 15px/1.7 -apple-system, "Segoe UI", "PingFang SC", sans-serif;
  color: #1f2328; background: #fbfcfe; }
a { color: #0b62c4; text-decoration: none; }
a:hover { text-decoration: underline; }
header { position: sticky; top: 0; z-index: 2; display: flex; gap: 16px;
  align-items: baseline; padding: 10px 24px; background: #ffffffdd;
  border-bottom: 1px solid #d8dee4; backdrop-filter: blur(8px); }
header strong { font-size: 16px; }
header span { color: #59636e; font-size: 12px; }
main { max-width: 980px; margin: 28px auto 64px; padding: 0 24px; }
h1 { font-size: 28px; line-height: 1.25; margin: 8px 0 18px; }
h2 { font-size: 19px; margin: 32px 0 10px; padding-top: 10px; border-top: 1px solid #e6eaef; }
h3 { font-size: 16px; margin: 20px 0 8px; }
p { margin: 8px 0; }
code { font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
  background: #eef1f4; padding: 1px 5px; border-radius: 4px; }
pre { overflow: auto; padding: 14px 16px; background: #f3f5f7; border-radius: 8px; }
pre code { background: transparent; padding: 0; }
table { border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 14px; }
th, td { border: 1px solid #d8dee4; padding: 7px 9px; text-align: left; vertical-align: top; }
th { background: #f3f6f9; }
blockquote { margin: 12px 0; padding: 8px 14px; border-left: 3px solid #0b62c4;
  background: #f3f7fc; color: #32383f; }
.ev { font-size: 12px; background: #e8f1ff; border-radius: 4px; padding: 0 4px; }
.evidence { padding-left: 0; list-style: none; }
.evidence li { margin: 4px 0; font-size: 13px; color: #4b5560; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px; margin: 16px 0; }
.card { border: 1px solid #d8dee4; border-radius: 8px; padding: 10px 12px; background: #fff; }
.card b { display: block; font-size: 20px; }
.nav { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px; }
.nav section h3 { margin: 0 0 8px; }
.nav ul { margin: 0; padding-left: 18px; }
.search { display: flex; gap: 8px; margin: 18px 0; }
.search input { flex: 1; padding: 9px 12px; border: 1px solid #d8dee4; border-radius: 8px; }
#results { margin: 0; padding-left: 18px; }
.src { font: 12px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre; }
.src .ln { color: #8b949e; display: inline-block; width: 4.5em; user-select: none; }
.src .line { display: block; }
.src .line:target { background: #fff3bf; }
@media (prefers-color-scheme: dark) {
  body { color: #e6edf3; background: #0d1117; }
  header { background: #161b22dd; border-color: #30363d; }
  code, pre { background: #161b22; }
  th { background: #161b22; } th, td { border-color: #30363d; }
  .card { background: #161b22; border-color: #30363d; }
  blockquote { background: #161b22; color: #c9d1d9; }
  .ev { background: #1f3a5f; }
}
"""

_SEARCH_JS = """
(function () {
  var data = window.CW_SEARCH || { pages: [] };
  var input = document.getElementById('q');
  var out = document.getElementById('results');
  if (!input || !out) return;
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) {
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];
  }); }
  input.addEventListener('input', function () {
    var q = input.value.trim().toLowerCase();
    if (!q) { out.innerHTML = ''; return; }
    var hits = data.pages.filter(function (p) {
      return (p.title + ' ' + p.text + ' ' + p.id).toLowerCase().indexOf(q) !== -1;
    }).slice(0, 20);
    out.innerHTML = hits.map(function (p) {
      return '<li><a href="' + esc(p.url) + '">' + esc(p.title) +
        '</a> <code>' + esc(p.id) + '</code></li>';
    }).join('') || '<li>没有匹配页面</li>';
  });
})();
"""


def build_site(
    version_dir: Path,
    index: VersionIndex,
    payload: list[tuple[str, str, str]],
    workdir: Path,
    profile: Mapping[str, object],
) -> Path:
    """生成 site/；先写临时目录再原子替换，失败不会破坏旧站点。"""
    tmp = version_dir / "site.tmp"
    site = version_dir / "site"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    (tmp / "pages").mkdir()
    (tmp / "source").mkdir()

    page_text = {page_id: text for page_id, text, _result in payload}
    result_text = {page_id: result for page_id, _text, result in payload}
    titles = {entry.page_id: entry.title for entry in index.pages}
    known = set(page_text)
    evidence = _evidence_lines(result_text)

    (tmp / "style.css").write_text(_CSS, encoding="utf-8")
    _write_source_pages(tmp, workdir, evidence)

    for page_id, text in page_text.items():
        body = _page_body(text)
        content = _render_body(
            body,
            page_id=page_id,
            from_dir=posixpath.dirname(f"pages/{page_id}.html"),
            known=known,
            evidence=evidence,
        )
        page_titles = titles.get(page_id, page_id)
        page_type = page_id.split("/", 1)[0]
        page_html = _layout(
            title=page_titles,
            repo=index.repo_id,
            version=index.version_id,
            root=posixpath.relpath("index.html", posixpath.dirname(f"pages/{page_id}.html")),
            css=posixpath.relpath("style.css", posixpath.dirname(f"pages/{page_id}.html")),
            body=content,
        )
        target = tmp / "pages" / f"{page_id}.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page_html, encoding="utf-8")
        _ = page_type

    home_body = _page_body(page_text.get("home", ""))
    home_content = _render_body(
        home_body,
        page_id="home",
        from_dir="",
        known=known,
        evidence=evidence,
    )
    home_content += _nav_html(index, titles)
    home_html = _layout(
        title=f"{index.repo_id} Repo Wiki",
        repo=index.repo_id,
        version=index.version_id,
        root="index.html",
        css="style.css",
        body=_profile_html(index, profile) + _search_html() + home_content,
    )
    (tmp / "index.html").write_text(home_html, encoding="utf-8")

    search_pages = [
        {
            "id": page_id,
            "title": titles.get(page_id, page_id),
            "type": page_id.split("/", 1)[0],
            "url": f"pages/{page_id}.html",
            "text": _plain_text(_page_body(text))[:600],
        }
        for page_id, text in page_text.items()
    ]
    search_data = json.dumps(
        {"repo": index.repo_id, "version": index.version_id, "pages": search_pages},
        ensure_ascii=False,
    )
    (tmp / "search.js").write_text(
        f"window.CW_SEARCH = {search_data};\n{_SEARCH_JS}", encoding="utf-8"
    )

    if site.exists():
        shutil.rmtree(site)
    tmp.rename(site)
    return site


# ---------------------------------------------------------------- 页面结构


def _page_body(text: str) -> str:
    if not text.strip():
        return ""
    try:
        _meta, body = parse_front_matter(text)
        return body
    except Exception:  # noqa: BLE001 - 站点渲染不能因单页 front matter 失败而中断
        return text


def _layout(*, title: str, repo: str, version: str, root: str, css: str, body: str) -> str:
    return (
        '<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_esc(title)} · {_esc(repo)}</title>"
        f'<link rel="stylesheet" href="{_esc(css)}">'
        "</head><body>"
        f'<header><strong><a href="{_esc(root)}">{_esc(repo)} Repo Wiki</a></strong>'
        f"<span>{_esc(version)}</span></header>"
        f"<main>{body}</main></body></html>\n"
    )


def _profile_html(index: VersionIndex, profile: Mapping[str, object]) -> str:
    items = [
        ("快照", index.snapshot_id.split("@", 1)[-1][:8] or index.snapshot_id),
        ("页面", len(index.pages)),
        ("文件", profile.get("files", "-")),
        ("符号", profile.get("symbols", "-")),
        ("调用", profile.get("calls", "-")),
        ("模块", profile.get("modules", "-")),
    ]
    cards = "".join(
        f'<div class="card"><b>{_esc(value)}</b>{_esc(label)}</div>' for label, value in items
    )
    return f'<section class="cards">{cards}</section>'


def _search_html() -> str:
    return (
        '<section><h2>搜索</h2><div class="search">'
        '<input id="q" type="search" placeholder="输入模块名、符号或配置键">'
        '</div><ul id="results"></ul></section>'
        '<script src="search.js"></script>'
    )


def _nav_html(index: VersionIndex, titles: dict[str, str]) -> str:
    groups: dict[str, list[str]] = {}
    for entry in index.pages:
        page_id = entry.page_id
        if page_id in {"home", "overview", "architecture"}:
            group = "开始"
        elif page_id.startswith("module/"):
            group = "模块"
        elif page_id.startswith("flow/"):
            group = "流程"
        elif page_id.startswith("interface/"):
            group = "接口"
        else:
            group = "配置 / 构建 / 测试 / 术语"
        groups.setdefault(group, []).append(page_id)
    order = ["开始", "模块", "流程", "接口", "配置 / 构建 / 测试 / 术语"]
    sections: list[str] = []
    for group in order:
        ids = groups.get(group, [])
        if not ids:
            continue
        links = "".join(
            f'<li><a href="pages/{_esc(page_id)}.html">'
            f"{_esc(titles.get(page_id, page_id))}</a></li>"
            for page_id in sorted(ids)
        )
        sections.append(f"<section><h3>{_esc(group)}</h3><ul>{links}</ul></section>")
    return f'<h2>目录</h2><div class="nav">{"".join(sections)}</div>'


# ---------------------------------------------------------------- Markdown


def _render_body(
    body: str,
    *,
    page_id: str,
    from_dir: str,
    known: set[str],
    evidence: dict[str, list[tuple[str, int, str]]],
) -> str:
    narrative, evidence_lines = _split_evidence(body)
    rendered = _render_blocks(narrative, page_id=page_id, from_dir=from_dir, known=known)
    rendered += _render_evidence(evidence_lines, from_dir=from_dir)
    _ = evidence
    return rendered


def _split_evidence(body: str) -> tuple[str, list[str]]:
    marker = "\n## 出处"
    index = body.find(marker)
    if index == -1:
        return body, []
    narrative = body[:index]
    section = body[index + len(marker) :]
    lines = [line.strip() for line in section.splitlines() if line.strip()]
    return narrative, lines


def _render_blocks(text: str, *, page_id: str, from_dir: str, known: set[str]) -> str:
    lines = text.splitlines()
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue
        if stripped.startswith("```"):
            language = stripped[3:].strip()
            index += 1
            code: list[str] = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            index += 1
            cls = f' class="language-{_esc(language)}"' if language else ""
            out.append(f"<pre><code{cls}>{html.escape(chr(10).join(code))}</code></pre>")
            continue
        heading = _HEADING.match(stripped)
        if heading:
            level = len(heading.group(1))
            out.append(
                f"<h{level}>{_inline(heading.group(2), page_id, from_dir, known)}</h{level}>"
            )
            index += 1
            continue
        if stripped.startswith("|"):
            table, index = _render_table(lines, index, page_id, from_dir, known)
            out.append(table)
            continue
        if stripped.startswith(">"):
            quote: list[str] = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quote.append(lines[index].strip().lstrip(">").strip())
                index += 1
            out.append(
                "<blockquote>"
                + _inline(" ".join(quote), page_id, from_dir, known)
                + "</blockquote>"
            )
            continue
        if _LIST.match(stripped):
            ordered = stripped[0].isdigit()
            tag = "ol" if ordered else "ul"
            items: list[str] = []
            while index < len(lines) and _LIST.match(lines[index].strip()):
                item = _LIST.sub("", lines[index].strip(), count=1)
                items.append(f"<li>{_inline(item, page_id, from_dir, known)}</li>")
                index += 1
            out.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue
        paragraph: list[str] = [stripped]
        index += 1
        while index < len(lines):
            candidate = lines[index].strip()
            if (
                not candidate
                or candidate.startswith(("```", "|", ">"))
                or _HEADING.match(candidate)
                or _LIST.match(candidate)
            ):
                break
            paragraph.append(candidate)
            index += 1
        out.append(f"<p>{_inline(' '.join(paragraph), page_id, from_dir, known)}</p>")
    return "\n".join(out)


def _render_table(
    lines: list[str], start: int, page_id: str, from_dir: str, known: set[str]
) -> tuple[str, int]:
    rows: list[list[str]] = []
    index = start
    while index < len(lines) and lines[index].strip().startswith("|"):
        raw = lines[index].strip().strip("|")
        if not _TABLE_SEP.match(lines[index].strip()):
            rows.append([cell.strip() for cell in raw.split("|")])
        index += 1
    if not rows:
        return "", index
    header = "".join(f"<th>{_inline(cell, page_id, from_dir, known)}</th>" for cell in rows[0])
    body = "".join(
        "<tr>"
        + "".join(f"<td>{_inline(cell, page_id, from_dir, known)}</td>" for cell in row)
        + "</tr>"
        for row in rows[1:]
    )
    return f"<table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table>", index


def _render_evidence(lines: list[str], *, from_dir: str) -> str:
    if not lines:
        return ""
    items: list[str] = []
    for line in lines:
        match = _EVIDENCE_LINE.match(line)
        if not match:
            continue
        evidence_id, path, line_no, rest = match.groups()
        url = posixpath.relpath(f"source/{path}.html", from_dir or ".")
        target = f"{url}#L{line_no}" if _source_exists_hint(path) else url
        items.append(
            f'<li id="{_esc(evidence_id)}">[{_esc(evidence_id)}]: '
            f'<a href="{_esc(target)}">{_esc(path)}:{_esc(line_no)}</a>'
            f"{_esc(rest)}</li>"
        )
    if not items:
        return ""
    return '<h2>出处</h2><ul class="evidence">' + "".join(items) + "</ul>"


def _source_exists_hint(_path: str) -> bool:
    # 源码页是否真的生成由 _write_source_pages 决定；链接先带上 #L，缺失时浏览器停在页首
    return True


def _inline(text: str, page_id: str, from_dir: str, known: set[str]) -> str:
    codes: list[str] = []

    def protect(match: re.Match[str]) -> str:
        codes.append(html.escape(match.group(1)))
        return f"\x00CODE{len(codes) - 1}\x00"

    protected = _CODE_SPAN.sub(protect, text)
    escaped = html.escape(protected)

    def link(match: re.Match[str]) -> str:
        label = match.group(1)
        target = html.unescape(match.group(2))
        href = _href(target, page_id, from_dir, known)
        return f'<a href="{_esc(href)}">{label}</a>'

    escaped = _LINK.sub(link, escaped)
    escaped = _BOLD.sub(r"<strong>\1</strong>", escaped)

    def evidence(match: re.Match[str]) -> str:
        ref = match.group(1)
        return f'<a class="ev" href="#{_esc(ref)}">[{_esc(ref)}]</a>'

    escaped = _EVIDENCE_REF.sub(evidence, escaped)
    for idx, code in enumerate(codes):
        escaped = escaped.replace(f"\x00CODE{idx}\x00", f"<code>{code}</code>")
    _ = page_id
    return escaped


def _href(target: str, page_id: str, from_dir: str, known: set[str]) -> str:
    if target.startswith(("http://", "https://", "mailto:", "#")):
        return target
    if _INTERNAL_PREFIX.match(target):
        clean, _sep, anchor = target.partition("#")
        if clean in known:
            rel = posixpath.relpath(f"pages/{clean}.html", from_dir or ".")
            return rel + (f"#{anchor}" if anchor else "")
    return target


def _plain_text(markdown: str) -> str:
    text = re.sub(r"```[\s\S]*?```", " ", markdown)
    text = re.sub(r"^## 出处[\s\S]*$", " ", text, flags=re.MULTILINE)
    text = re.sub(r"[#>*_`|]", " ", text)
    return " ".join(text.split())


# ---------------------------------------------------------------- 源码页


def _evidence_lines(result_text: dict[str, str]) -> dict[str, list[tuple[str, int, str]]]:
    files: dict[str, list[tuple[str, int, str]]] = {}
    for text in result_text.values():
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            continue
        for item in data.get("evidence", []) or []:
            if not isinstance(item, dict):
                continue
            path = str(item.get("file", ""))
            line = int(item.get("line_hint", 0) or 0)
            symbol = str(item.get("symbol", ""))
            if path:
                files.setdefault(path, []).append((symbol, line, str(item.get("id", ""))))
    return files


def _write_source_pages(
    site: Path, workdir: Path, evidence: dict[str, list[tuple[str, int, str]]]
) -> None:
    for path, rows in evidence.items():
        source = (workdir / path).resolve()
        if not str(source).startswith(str(workdir.resolve())) or not source.is_file():
            continue
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        rendered = []
        for number, line in enumerate(lines, start=1):
            rendered.append(
                f'<span class="line" id="L{number}"><span class="ln">{number}</span>'
                f"{html.escape(line)}</span>"
            )
        target = site / "source" / f"{path}.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        # 源码页落在 source/<path>.html，相对路径必须从 source/ 这一层起算
        page_dir = posixpath.dirname(f"source/{path}.html")
        back = posixpath.relpath("index.html", page_dir)
        body = (
            f"<h1>{_esc(path)}</h1>"
            f'<p><a href="{back}">返回首页</a></p>'
            f'<pre class="src">{"".join(rendered)}</pre>'
        )
        target.write_text(
            _layout(
                title=path,
                repo="source",
                version=f"{len(rows)} 条证据",
                root=posixpath.relpath("index.html", page_dir),
                css=posixpath.relpath("style.css", page_dir),
                body=body,
            ),
            encoding="utf-8",
        )


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


__all__ = ["build_site"]
