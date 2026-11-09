"""补丁策略：路径允许/禁止与 diff 内容扫描（计划 14.6.2 / 14.6.3）。

只做确定性规则，结论统一成 PatchFinding；不读写文件、不调用模型。
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable

from codingwiki.domain.patch import (
    ChangeKind,
    FindingSeverity,
    PatchFinding,
    PatchPolicy,
    PatchStats,
)
from codingwiki.patch.diff import FileDiff

# 允许做“代码注释/调试输出”检查的扩展名；文档、配置不参与。
_CODE_EXTENSIONS = frozenset(
    {
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".cxx",
        ".cu",
        ".cuh",
        ".hpp",
        ".hh",
        ".py",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".kts",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".m",
        ".mm",
        ".swift",
        ".scala",
        ".sh",
        ".bash",
        ".lua",
        ".rb",
        ".php",
    }
)

# 默认禁止路径：正则 → 原因。允许清单不能覆盖禁止清单（禁止项优先）。
_DENY_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(^|/)(\.github|\.gitlab|\.circleci|\.buildkite|ci)/"),
        "CI 配置目录",
    ),
    (
        re.compile(
            r"(^|/)(\.gitlab-ci\.yml|\.travis\.yml|Jenkinsfile|azure-pipelines\.yml|"
            r"appveyor\.yml|bitbucket-pipelines\.yml)$"
        ),
        "CI 配置",
    ),
    (
        re.compile(
            r"(^|/)(CMakeLists\.txt|Makefile|GNUmakefile|makefile|meson\.build|BUILD|"
            r"BUILD\.bazel|WORKSPACE|WORKSPACE\.bazel|setup\.py|conanfile\.py|"
            r"build\.sh|configure|configure\.ac)$"
        ),
        "构建脚本",
    ),
    (re.compile(r"(^|/)[^/]+\.(mk|cmake)$"), "构建脚本"),
    (
        re.compile(
            r"(^|/)(\.env[^/]*|id_rsa[^/]*|id_ed25519[^/]*|credentials[^/]*|secrets[^/]*|"
            r"[^/]+\.(pem|key|p12|pfx|crt|cer|kdbx))$"
        ),
        "密钥/证书/凭据",
    ),
    (
        re.compile(
            r"(^|/)(third_party|thirdparty|third-party|vendor|external|external_deps|deps|submodules)/"
        ),
        "第三方目录",
    ),
    (re.compile(r"(^|/)generated/"), "自动生成目录"),
    (re.compile(r"(^|/)[^/]*_generated\.(c|h|cc|cpp|py|go|rs|java|ts)$"), "自动生成文件"),
    (re.compile(r"(^|/)[^/]*\.(pb\.h|pb\.cc|pb\.cpp|pb\.c|pb\.go|pb\.py)$"), "自动生成文件"),
    (
        re.compile(
            r"(^|/)[^/]+\.(so|a|dll|dylib|exe|o|obj|bin|class|jar|wasm|pyc|pyo|zip|tar|"
            r"gz|bz2|xz|7z|pdf|png|jpg|jpeg|gif|bmp|ico|webp|woff|woff2|ttf|otf|mp3|mp4|"
            r"wav|avi|mov|pt|pth|onnx|npy|npz|safetensors|h5|hdf5|sqlite|sqlite3|db)$"
        ),
        "二进制/构建产物",
    ),
)

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "私钥"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS Access Key"),
    (re.compile(r"\bASIA[0-9A-Z]{16}\b"), "AWS 临时 Key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), "GitHub Token"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "API Key"),
    (
        re.compile(
            r"\b(?:api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*['\"][^'\"]{8,}['\"]",
            re.IGNORECASE,
        ),
        "凭据字面量",
    ),
)

_DEBUG_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"console\.log\("), "console.log"),
    (re.compile(r"System\.out\.println\("), "System.out.println"),
    (re.compile(r"std::(?:cout|cerr)\s*<<"), "std::cout/cerr"),
    (re.compile(r"\bqDebug\s*\("), "qDebug"),
    (re.compile(r"\bNSLog\s*\("), "NSLog"),
    (re.compile(r"\bbreakpoint\s*\("), "breakpoint"),
    (re.compile(r"\b(?:import\s+pdb|pdb\.set_trace)\b"), "pdb"),
)

_TEST_SKIP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"pytest\.mark\.skip"),
    re.compile(r"unittest\.skip"),
    re.compile(r"\bGTEST_SKIP\s*\("),
    re.compile(r"\bDISABLED_[A-Za-z0-9_]*\s*\("),
    re.compile(r"\.skip\s*\("),
    re.compile(r"@Ignore\b"),
)

_ASSERTION_RE = re.compile(
    r"(?:^|\W)(?:assert|assertEqual|assertTrue|assertFalse|assertThat|EXPECT_[A-Z_]+|"
    r"ASSERT_[A-Z_]+|REQUIRE(?:_[A-Z_]+)?|CHECK(?:_[A-Z_]+)?|BOOST_[A-Z_]+)\b"
)

_TEST_DIR_RE = re.compile(r"(^|/)(tests?|testing|test_data)/")
_TEST_NAME_RE = re.compile(r"(^|/)(test_[^/]*|[^/]*_test(s)?\.[^/]+|[^/]*Tests?\.[^/]+)$")

# 依赖清单与锁文件（计划 14.9：禁止新增依赖，除非 Spec 明确）。
# 这里只识别文件，不做语法解析；新增行一律视为依赖声明。
_DEPENDENCY_MANIFEST_RE = re.compile(
    r"(^|/)(requirements[^/]*\.txt|pyproject\.toml|setup\.cfg|Pipfile|"
    r"package\.json|Cargo\.toml|go\.mod|vcpkg\.json|conanfile\.txt|Gemfile|"
    r"pom\.xml|build\.gradle(?:\.kts)?|environment[^/]*\.ya?ml|conda[^/]*\.ya?ml)$"
)
_LOCKFILE_RE = re.compile(
    r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|Pipfile\.lock|poetry\.lock|"
    r"Cargo\.lock|go\.sum|Gemfile\.lock|conan\.lock|uv\.lock|[^/]+\.lock)$"
)

_COMMENT_PREFIXES = ("//", "#", "/*", "*", "--")
_COMMENT_EXEMPT_PREFIXES = (
    "///",
    "//!",
    "/**",
    "* @",
    "#!",
    "#include",
    "#define",
    "#pragma",
    "#if",
    "#endif",
    "#else",
    "#elif",
)

_GENERATED_MARKER_RE = re.compile(
    r"(?:code generated|do not edit|auto[- ]generated|automatically generated)", re.IGNORECASE
)

_TODO_RE = re.compile(r"(?:TODO|FIXME|XXX)\b")

# 网络调用扫描：新增代码里出现外部 HTTP/socket 访问时降级为 WARNING（
# 计划 14.5.3 要求人工复核；误报代价高于漏报，故不直接拒绝）。
_NETWORK_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\b(?:requests|httpx)\.(?:get|post|put|patch|delete|head|request)\s*\("),
        "HTTP 客户端",
    ),
    (re.compile(r"\burlopen\s*\(|\burllib\.request\b|\bhttp\.client\b"), "urllib/http.client"),
    (
        re.compile(r"\b(?:socket|ssl)\.(?:connect|create_connection)\s*\("),
        "socket 连接",
    ),
    (
        re.compile(r"\baxios\.(?:get|post|put|patch|delete|request)\b"),
        "axios",
    ),
    (re.compile(r"\bfetch\s*\(\s*['\"`]http"), "fetch"),
    (re.compile(r"\b(?:HttpClient|OkHttpClient|URLSession)\b"), "HTTP 客户端"),
    (re.compile(r"\b(?:curl|wget)\s+(?:-|http)"), "curl/wget"),
)

# 危险命令/能力扫描：真正能破坏环境或执行任意代码的写法直接拒绝；
# eval/exec 误报面较大，降级为 WARNING 供复核。
_DANGEROUS_PATTERNS: tuple[tuple[re.Pattern[str], str, FindingSeverity], ...] = (
    (re.compile(r"\bos\.system\s*\("), "os.system", FindingSeverity.BLOCKER),
    (re.compile(r"\bshell\s*=\s*True\b"), "shell=True", FindingSeverity.BLOCKER),
    (re.compile(r"\brm\s+-rf\b"), "rm -rf", FindingSeverity.BLOCKER),
    (re.compile(r"\bsudo\s+"), "sudo", FindingSeverity.BLOCKER),
    (re.compile(r"\bchmod\s+777\b"), "chmod 777", FindingSeverity.BLOCKER),
    (re.compile(r"\|[^\n|]*\b(?:sh|bash)\b"), "管道执行 shell", FindingSeverity.BLOCKER),
    (re.compile(r"\bbase64\s+--?d(?:ecode)?\s*\|"), "base64 解码后执行", FindingSeverity.BLOCKER),
    (re.compile(r"\b(?:eval|exec)\s*\("), "eval/exec", FindingSeverity.WARNING),
)


def deny_reason(path: str) -> str:
    """命中禁止清单时返回原因；未命中返回空串。"""
    for pattern, reason in _DENY_RULES:
        if pattern.search(path):
            return reason
    return ""


def is_test_path(path: str) -> bool:
    return bool(_TEST_DIR_RE.search(path) or _TEST_NAME_RE.search(path))


def is_code_path(path: str) -> bool:
    dot = path.rfind(".")
    return dot >= 0 and path[dot:] in _CODE_EXTENSIONS


def is_lockfile(path: str) -> bool:
    """锁文件：只提示人工确认解析结果，不直接阻断。"""
    return bool(_LOCKFILE_RE.search(path))


def is_dependency_manifest(path: str) -> bool:
    """依赖清单或锁文件：新增行需要 Spec 明确允许（计划 14.9）。"""
    return bool(_DEPENDENCY_MANIFEST_RE.search(path)) or is_lockfile(path)


def is_allowed_path(path: str, policy: PatchPolicy) -> bool:
    """允许清单（Spec 任务文件 / --allow glob）；空清单不放行任何文件。"""
    for pattern in policy.allowed_paths:
        if path == pattern or fnmatch.fnmatchcase(path, pattern):
            return True
        if pattern.endswith("/") and path.startswith(pattern):
            return True
    return False


def _allowed_module_test(path: str, policy: PatchPolicy) -> bool:
    if not policy.allow_module_tests or not policy.allowed_paths:
        return False
    if not is_test_path(path):
        return False
    roots = {item.split("/", 1)[0] for item in policy.allowed_paths if "/" in item}
    if not roots:
        return False
    return path.split("/", 1)[0] in roots


def path_findings(diffs: Iterable[FileDiff], policy: PatchPolicy) -> list[PatchFinding]:
    findings: list[PatchFinding] = []
    for item in diffs:
        reason = deny_reason(item.path)
        if reason:
            findings.append(
                PatchFinding(
                    code="path.forbidden",
                    severity=FindingSeverity.BLOCKER,
                    message=f"{item.path} 命中禁止规则：{reason}",
                    path=item.path,
                )
            )
            continue
        if is_allowed_path(item.path, policy) or _allowed_module_test(item.path, policy):
            continue
        findings.append(
            PatchFinding(
                code="path.not_allowed",
                severity=FindingSeverity.BLOCKER,
                message=f"{item.path} 不在允许清单内（Spec 任务文件或 --allow）",
                path=item.path,
            )
        )
    return findings


def stats_of(diffs: Iterable[FileDiff]) -> PatchStats:
    items = list(diffs)
    return PatchStats(
        files=len(items),
        added_lines=sum(item.added_lines for item in items),
        deleted_lines=sum(item.deleted_lines for item in items),
    )


def limit_findings(stats: PatchStats, policy: PatchPolicy) -> list[PatchFinding]:
    findings: list[PatchFinding] = []
    if stats.files > policy.max_files:
        findings.append(
            PatchFinding(
                code="diff.too_many_files",
                severity=FindingSeverity.BLOCKER,
                message=f"改动 {stats.files} 个文件，超过上限 {policy.max_files}",
            )
        )
    if stats.total_lines > policy.max_lines:
        findings.append(
            PatchFinding(
                code="diff.too_many_lines",
                severity=FindingSeverity.BLOCKER,
                message=f"改动 {stats.total_lines} 行，超过上限 {policy.max_lines}",
            )
        )
    return findings


def content_findings(diffs: Iterable[FileDiff], policy: PatchPolicy) -> list[PatchFinding]:
    items = list(diffs)
    findings: list[PatchFinding] = []
    for item in items:
        if item.binary:
            findings.append(
                PatchFinding(
                    code="diff.binary",
                    severity=FindingSeverity.BLOCKER,
                    message=f"{item.path} 是二进制改动",
                    path=item.path,
                )
            )
            continue
        if item.line_endings_only:
            findings.append(
                PatchFinding(
                    code="diff.line_endings",
                    severity=FindingSeverity.BLOCKER,
                    message=f"{item.path} 仅有换行符整体变化",
                    path=item.path,
                )
            )
        elif item.whitespace_only and not policy.allow_formatting:
            findings.append(
                PatchFinding(
                    code="diff.whitespace_only",
                    severity=FindingSeverity.BLOCKER,
                    message=f"{item.path} 仅有空白/格式化改动",
                    path=item.path,
                )
            )
        if item.kind is ChangeKind.DELETED:
            if is_test_path(item.path):
                findings.append(
                    PatchFinding(
                        code="test.deleted",
                        severity=FindingSeverity.BLOCKER,
                        message=f"删除测试文件 {item.path}",
                        path=item.path,
                    )
                )
            continue
        if _looks_generated(item) and not policy.allow_generated:
            findings.append(
                PatchFinding(
                    code="file.generated",
                    severity=FindingSeverity.BLOCKER,
                    message=f"{item.path} 文件头声明为自动生成，禁止手改",
                    path=item.path,
                )
            )
        findings.extend(_added_line_findings(item, policy))
        findings.extend(_dependency_findings(item, policy))
        if is_test_path(item.path):
            findings.extend(_removed_assertion_findings(item))
        findings.extend(_comment_block_findings(item, policy))
    findings.extend(test_first_findings(items, policy))
    return findings


def test_first_findings(diffs: Iterable[FileDiff], policy: PatchPolicy) -> list[PatchFinding]:
    """改动源码却没有任何测试改动时给出 WARNING（计划 14.5.2）。

    允许“先实现最小接口、再补测试”，但必须在交付说明中注明；
    因此这里只提示，不阻断。
    """
    if not policy.require_test_first:
        return []
    items = [item for item in diffs if item.kind is not ChangeKind.DELETED]
    code = [item for item in items if is_code_path(item.path) and not is_test_path(item.path)]
    if not code or any(is_test_path(item.path) for item in items):
        return []
    return [
        PatchFinding(
            code="test.first_missing",
            severity=FindingSeverity.WARNING,
            message="改动源码但未新增/修改测试；若先实现接口、后续补测试，请在交付说明中注明",
            path=code[0].path,
        )
    ]


def _added_line_findings(item: FileDiff, policy: PatchPolicy) -> list[PatchFinding]:
    findings: list[PatchFinding] = []
    for line in item.added_text:
        for pattern, label in _SECRET_PATTERNS:
            if pattern.search(line):
                findings.append(
                    PatchFinding(
                        code="diff.secret",
                        severity=FindingSeverity.BLOCKER,
                        message=f"{item.path} 新增疑似{label}",
                        path=item.path,
                    )
                )
                break
        if is_code_path(item.path):
            for pattern, label in _DEBUG_PATTERNS:
                if pattern.search(line):
                    findings.append(
                        PatchFinding(
                            code="diff.debug_output",
                            severity=FindingSeverity.BLOCKER,
                            message=f"{item.path} 新增调试输出（{label}）",
                            path=item.path,
                        )
                    )
                    break
            for pattern, label in _NETWORK_PATTERNS:
                if pattern.search(line):
                    findings.append(
                        PatchFinding(
                            code="diff.network_call",
                            severity=FindingSeverity.WARNING,
                            message=f"{item.path} 新增网络调用（{label}），需人工复核",
                            path=item.path,
                        )
                    )
                    break
            for pattern, label, severity in _DANGEROUS_PATTERNS:
                if pattern.search(line):
                    findings.append(
                        PatchFinding(
                            code="diff.dangerous_call",
                            severity=severity,
                            message=f"{item.path} 新增危险调用（{label}）",
                            path=item.path,
                        )
                    )
                    break
        if is_test_path(item.path) and any(pattern.search(line) for pattern in _TEST_SKIP_PATTERNS):
            findings.append(
                PatchFinding(
                    code="test.skipped",
                    severity=FindingSeverity.BLOCKER,
                    message=f"{item.path} 新增跳过测试标记",
                    path=item.path,
                )
            )
        if _TODO_RE.search(line):
            findings.append(
                PatchFinding(
                    code="diff.todo",
                    severity=(
                        FindingSeverity.BLOCKER if policy.todo_blocker else FindingSeverity.WARNING
                    ),
                    message=f"{item.path} 新增 TODO/FIXME，需在 Spec/任务说明中登记",
                    path=item.path,
                )
            )
    return findings


def _dependency_findings(item: FileDiff, policy: PatchPolicy) -> list[PatchFinding]:
    """依赖清单新增行默认阻断；锁文件变更只提示（计划 14.9）。"""
    if not is_dependency_manifest(item.path):
        return []
    if is_lockfile(item.path):
        return [
            PatchFinding(
                code="dep.lockfile_change",
                severity=FindingSeverity.WARNING,
                message=f"{item.path} 锁文件变更，需人工确认依赖解析结果",
                path=item.path,
            )
        ]
    added = [
        line.strip() for line in item.added_text if line.strip() and not _is_comment_line(line)
    ]
    if not added or policy.allow_new_dependencies:
        return []
    return [
        PatchFinding(
            code="dep.new_dependency",
            severity=FindingSeverity.BLOCKER,
            message=(
                f"{item.path} 新增 {len(added)} 行依赖声明（如 {added[0][:60]}），"
                "需 Spec 明确允许（--allow-new-dependencies）"
            ),
            path=item.path,
        )
    ]


def _removed_assertion_findings(item: FileDiff) -> list[PatchFinding]:
    findings: list[PatchFinding] = []
    for line in item.removed_text:
        if _ASSERTION_RE.search(line):
            findings.append(
                PatchFinding(
                    code="test.assertion_removed",
                    severity=FindingSeverity.BLOCKER,
                    message=f"{item.path} 删除/修改既有测试断言，需人工复核",
                    path=item.path,
                )
            )
            break
    return findings


def _comment_block_findings(item: FileDiff, policy: PatchPolicy) -> list[PatchFinding]:
    if not is_code_path(item.path):
        return []
    count = sum(1 for line in item.added_text if _is_comment_line(line))
    if count < policy.max_commented_lines:
        return []
    return [
        PatchFinding(
            code="diff.commented_code",
            severity=FindingSeverity.BLOCKER,
            message=f"{item.path} 新增 {count} 行注释代码，超过上限 {policy.max_commented_lines}",
            path=item.path,
        )
    ]


def _is_comment_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith(_COMMENT_EXEMPT_PREFIXES):
        return False
    return stripped.startswith(_COMMENT_PREFIXES)


def _looks_generated(item: FileDiff) -> bool:
    if item.kind not in {ChangeKind.ADDED, ChangeKind.UNTRACKED}:
        return False
    head = item.added_text[:10]
    return any(_GENERATED_MARKER_RE.search(line) for line in head)


__all__ = [
    "content_findings",
    "deny_reason",
    "is_allowed_path",
    "is_code_path",
    "is_dependency_manifest",
    "is_lockfile",
    "is_test_path",
    "limit_findings",
    "path_findings",
    "stats_of",
    "test_first_findings",
]
