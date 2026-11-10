# CodingWiki

CodingWiki 是一套“代码仓库百科”工具：把一个大型代码仓库自动整理成**可浏览、可检索、每句话都能追溯到源码**的 Repo Wiki，并在其上继续提供代码问答、开发 Spec 生成与自动编码能力。

它面向的不是一次性文档，而是可持续更新的知识资产：

- **Repo Wiki**：模块 / 流程 / 接口 / 配置 / 构建 / 测试 / 术语表页面，带证据引用、自动校验、版本发布与回滚；
- **代码检索与问答**：BM25 + 向量 + RRF 融合 + cross-encoder 重排，答案带 `[S]` 引用，资料不足直接拒答；
- **开发 Spec**：需求文档 → 原子需求 → 证据包 → 可评审的完整 Spec（含追踪关系、验收标准、任务拆分）；
- **自动编码**：按 approved Spec 建隔离工作区，驱动模型后端改代码，经过补丁检查、构建测试、独立验证后产出交付包；
- **日更闭环**：`sync → facts → wiki → verify → publish → index` 一条命令完成代码更新与知识更新。

## 核心设计

1. **确定性事实层优先**：先用 git 快照、tree-sitter 解析、blob 哈希建立代码事实（文件、符号、调用、测试、配置、构建目标），再让模型基于任务包中的事实写解释。模型不自由阅读整个仓库，避免“看起来合理但没有依据”的内容。
2. **证据可回溯**：Wiki 页面中的结论引用 `[E#]`，逐条对应 `file:line`；问答中的事实引用 `[S#]`，对应检索到的资料段落。
3. **系统注入元数据**：页面 front matter、快照号、证据编号由系统重写/注入，不采信模型自报的元数据。
4. **校验先于发布**：页面必须通过结构、证据、占位语、篇幅与引用密度校验才能进入草稿区；发布门禁会重算内容哈希，防止草稿被篡改后绕过校验。
5. **自动编码只让模型改文件**：配置、编译、测试、补丁检查、结果判定全部由编排器执行，模型进程不负责执行命令，也不决定是否通过。

---

## 一、Repo Wiki

### 页面体系

发布版本包含以下页面类型：

| 类型 | 内容 |
|---|---|
| `home` / `overview` / `architecture` | 项目总览、仓库画像、架构与模块关系 |
| `module` | 模块职责、边界、关键数据结构与入口 |
| `flow` | 跨文件的端到端处理链路 |
| `interface` | 对外接口与调用约定 |
| `config` / `build` / `test` | 配置项、构建目标、测试组织 |
| `glossary` | 术语表 |

模块页固定包含：一句话职责、职责边界、关键数据结构、对外入口、依赖与影响、出处；其他页面类型按类型要求必填结构段。

生成流程由确定性工具规划：

```bash
codingwiki wiki plan --repo <repo>       # 生成覆盖计划（可复用的页面会标记 reuse）
codingwiki wiki generate --repo <repo>   # 生成 + 校验 + 修复循环
codingwiki wiki verify --repo <repo>     # 复验草稿区全部页面
codingwiki wiki publish --repo <repo>    # 发布门禁 + 新版本 + 站点
```

发布后产出：

- **离线站点**：`.codingwiki/wiki/<repo>/versions/<version>/site/index.html`，可直接用浏览器打开；
- **质量看板**：同目录 `dashboard.html`；命令行另有 `codingwiki wiki coverage --repo <repo>`；
- **版本目录**：`versions/<version>/pages/`、`index.json`、`site/`、`wiki/`；
- **当前版本指针**：`.codingwiki/wiki/<repo>/current`，支持回滚：

```bash
codingwiki wiki status --repo <repo>
codingwiki wiki stale --repo <repo>                        # 按最新快照标记受影响页面
codingwiki wiki rollback --repo <repo> --to <version>
```

### 导出为 GitHub Wiki / 通用 Wiki 文档组

```bash
codingwiki wiki export --repo <repo> [--version <version>]
```

导出内容在 `versions/<version>/wiki/`：

- 清洗 front matter、行内 `[E#]` 和机器段「出处」；
- 页尾「参考源码」表转换为源码仓库 blob 的精确行链接；
- 根据事实层调用边生成 Mermaid 模块依赖图、模块调用关系与流程调用链；
- 生成 `Home`、`_Sidebar`、`_Footer`，以及 `Modules`、`Interfaces`、`Flows` 索引页；
- 页面命名扁平化，可直接放入 GitHub Wiki（`<repo>.wiki.git`）或仓库内 `docs/`。

---

## 二、检索与问答

### 建索引

```bash
codingwiki index build --repo <repo> --embedding bge-m3
codingwiki index build --repo <repo> --embedding hash      # 离线/CI 占位
```

索引语料来自最新事实、已发布 Wiki 页面和需求文档切块，落盘为：

```text
.codingwiki/indices/<repo>/<index_id>/{chunks.jsonl,vectors.bin,index.json}
```

检索链路为 BM25 + 向量召回 → RRF 融合 → 可选 cross-encoder 重排。真实语义模型使用 `bge-m3`（1024 维）与 `bge-reranker-v2-m3`。

### 检索与问答

```bash
codingwiki search --repo <repo> --query "PUSCH 信道估计在哪实现" \
  --module <module> --file-prefix src/ --top-k 20 --no-rerank
codingwiki ask --repo <repo> --question "LDPC 译码的入口函数是什么"
```

问答特点：

- 只使用检索到的资料，不调用外部知识；
- 答案中的事实带 `[S#]` 引用，引用编号必须能闭合到资料段落；
- 资料不足时直接拒答，不生成猜测内容；
- 检索结果可按模块、文件前缀过滤。

### 评测

金标准集是 YAML（示例见 [evals/golden/](evals/golden/)）：

```yaml
set_id: myrepo-v1
repo_id: myrepo
questions:
  - id: Q01
    question: "认证逻辑在哪个模块？"
    expect_any: ["module/src/auth", "src/auth/"]
```

```bash
codingwiki eval --repo <repo> --golden evals/golden/myrepo-v1.yaml --top-k 10
```

输出 Recall@k、MRR、nDCG@k、拒答准确率、可答题误拒数与出处有效率，并与上一次报告对比。

---

## 三、开发 Spec

Spec 生成是六步确定性流水线，模型只负责草拟，编号、快照和来源由系统统一注入：

```text
normalize → extract → evidence → draft → validate → persist
```

```bash
# 1. 一条命令跑完六步：需求文档 → 原子需求 → 证据包 → 草稿 → 校验修复 → 落盘
codingwiki spec generate --repo <repo> --doc requirements/feature.md --id F-001 \
  --backend api --app-config configs/api.local.yaml

# 2. 校验
codingwiki spec validate .codingwiki/specs/<repo>/F-001

# 3. 评审
codingwiki spec review .codingwiki/specs/<repo>/F-001 \
  --approve --role architect --ack-warnings
```

需要分步检查或重跑某个环节时：

```bash
codingwiki spec extract --repo <repo> --doc requirements/feature.md \
  --backend api --app-config configs/api.local.yaml
codingwiki spec evidence --repo <repo> --spec <doc_id 或 F-001>
codingwiki spec list --repo <repo>
```

Spec 固定覆盖 11 章结构、需求覆盖、验收标准、任务规模、追溯关系、决策与状态等维度。评审状态机为：

```text
needs-info → draft → approved / rejected
```

批准要求现场重算 `blockers=0`、开放问题全部关闭、warning 已逐条确认、快照一致；门禁失败退出码为 `3`。评审结果同源写回 `spec.json`、`spec.md` 与 `review.json`。

常用评审参数：

- `--role requester|module_owner|test_owner|security|architect`
- `--reject --comment "原因"`
- `--close-question Q-001`（可重复）
- `--ack-warnings`

---

## 四、自动编码

自动编码按 approved Spec 的任务拆分执行，单个任务默认限定 **3 个文件、300 行改动**。

```bash
# 查看任务
codingwiki coding task list --spec-dir .codingwiki/specs/<repo>/F-001

# 可选：查看某任务的任务包
codingwiki coding packet build --repo <repo> \
  --spec-dir .codingwiki/specs/<repo>/F-001 --task T-001

# 执行编码
codingwiki coding run --repo <repo> \
  --spec-dir .codingwiki/specs/<repo>/F-001 \
  --task T-001 \
  --backend-config configs/backend.local.yaml \
  --verifier-config configs/verifier.local.yaml
```

模型后端通过 argv 模板启动，不经过 shell：

```yaml
# configs/backend.local.yaml（本机私有配置，已被 .gitignore 忽略）
kind: command
command:
  - my-agent
  - --packet
  - "{packet_dir}"
  - --workspace
  - "{workspace}"
  - --result
  - "{result_path}"
timeout_seconds: 1800
```

约定：后端进程只允许修改 `{workspace}` 中的文件，可选把结果写入 `{result_path}`；是否通过以退出码与 PatchGuard 检查为准，不采信模型自述。独立验证器为可选项，使用同样的配置结构，通过 `--verifier-config` 指定。

每轮执行：

```text
任务包 → 模型后端 → PatchGuard 补丁检查 → configure/lint/compile/test → 独立验证
```

PatchGuard 会检查允许/禁止路径、文件数、行数、二进制、换行符、纯空白改动、大段注释、调试输出、密钥、生成文件、删除或跳过测试、删除断言、高危命令调用、网络调用、`eval/exec`、新增依赖等；高危命令调用直接阻断，其余项生成警告进入报告供复核。另有测试先行红门禁、破坏性操作拦截、进程组隔离、CPU 时间上限、独立 `TMPDIR` 与增量构建。

运行产物：

```text
.codingwiki/coding/<repo>/<spec_id>/<task_id>/runs/<run_id>/
├── baseline/                     # 基线构建与测试结果
├── round-<n>/                    # 每轮后端、补丁检查、构建测试、验证结果
├── audit/commands.jsonl          # 所有执行命令的审计记录
├── run.json / report.json        # 运行记录与指标
├── delivery/                     # 成功：patch.diff / delivery.json / delivery.md / pr.md
└── escalation.json / escalation.md  # 失败：升级报告，不自动回滚
```

配套命令：

```bash
codingwiki patch workspace open --repo <repo> --task T-001
codingwiki patch check --repo <repo> --task T-001 --spec-dir .codingwiki/specs/<repo>/F-001
codingwiki patch workspace rollback --repo <repo> --task T-001
codingwiki patch workspace close --repo <repo> --task T-001 [--keep-branch]
codingwiki coding replay --run <run.json> --repo <repo> --spec-dir .codingwiki/specs/<repo>/F-001
codingwiki coding report --repo <repo> --spec-dir .codingwiki/specs/<repo>/F-001
```

`coding replay` 从原运行工作区捕获 `patch.diff`，在独立工作树中重放并逐阶段对比，输出 `replay.json`；`coding report` 汇总成功率、一次通过率、轮次、耗时与词元。

---

## 五、日更闭环

```bash
codingwiki run --repo <repo>
codingwiki run --repo all --backend api --app-config configs/api.local.yaml
```

六个阶段串行执行，全程持有仓库锁（同一仓库并发触发会立即失败并报告持锁者）：

```text
sync → facts → wiki → verify → publish → index
```

支持：

- `--from wiki`：从指定阶段开始，更早阶段不执行也不记录；
- `--skip index`：跳过指定阶段（可重复）；
- `--revision <rev>`：覆盖仓库配置的版本；
- `--backend mock|api`：Wiki 生成后端。

每次运行写入 `.codingwiki/jobs/<repo>/<时间戳>-<job_id>.json`。cron 示例：

```cron
0 2 * * * cd /path/to/CodingWiki && .venv/bin/codingwiki run --repo all --backend api --app-config configs/api.local.yaml
```

---

## 环境要求与安装

- Python 3.11+
- git
- 真实语义检索需要 `torch` 与 `transformers`（`hash` 后端不需要）

```bash
# 在工作区内创建虚拟环境（uv 会读取 uv.lock）
uv sync --extra dev
source .venv/bin/activate

# 可选：启用 bge-m3 与 bge-reranker-v2-m3
uv pip install torch transformers

# 命令行入口
codingwiki version
```

模型目录按以下顺序解析：

1. 命令行显式路径（`--model-path` / 配置里的 `model_path`）；
2. 环境变量 `CODINGWIKI_MODEL_DIR`；
3. `~/models/BAAI/<模型名>`；
4. `~/.cache/models/<模型名>`；
5. `.codingwiki/models/<模型名>`；
6. `.cache/models/<模型名>`。

模型目录名分别为 `bge-m3` 与 `bge-reranker-v2-m3`。

---

## 快速开始

### 1. 配置模型接入

仓库内的 [configs/default.yaml](configs/default.yaml) 是不含密钥的示例配置。需要真实模型时，复制一份本机私有配置（已被 `.gitignore` 忽略）：

```bash
cp configs/default.yaml configs/api.local.yaml
```

在 `configs/api.local.yaml` 中填写 `model.api`：

```yaml
model:
  backend: api
  api:
    enabled: true
    base_url: "https://你的-openai-兼容端点/v1"
    model: "你的模型名"
    api_key_env: CODINGWIKI_API_KEY
    # 或 api_key_file: "/绝对路径/到/密钥文件"
    max_tokens: 16384
```

`max_tokens` 不得低于 `16384`：更低值会导致页面 JSON 被截断。密钥只写入环境变量或本机密钥文件，不写入仓库文件。

### 2. 接入一个代码仓库

在 `configs/repos/<repo_id>.yaml` 新建仓库配置：

```yaml
repo_id: myrepo
provider: github                 # 也支持 git
url: https://github.com/org/myrepo.git
branch: main
root: ""                         # 可选：仓库内子目录作为分析根
revision: latest
enabled: true
excludes: []                     # 额外排除规则
max_text_file_size_kb: 2048

# 以下命令仅在自动编码阶段使用，可先留空
commands:
  build:
    configure: ""
    compile: "make -j8"
    incremental: ""              # 可选：修复轮次使用的增量编译命令
    timeout_s: 1800
  test:
    fast: "ctest -R smoke"
    full: "ctest"
    timeout_s: 3600
    report_format: junit
    report_paths: ["build/test-results/*.xml"]
    baseline_runs: 3
    retries: 2
    critical_tests: []
  lint:
    command: ""
    timeout_s: 600
resources:
  cpu_seconds: 3600              # 0 表示不限制
```

### 3. 首次生成 Wiki

```bash
codingwiki config validate --file configs/default.yaml
codingwiki repo sync --repo myrepo
codingwiki facts extract --repo myrepo
codingwiki wiki plan --repo myrepo
codingwiki wiki generate --repo myrepo --backend api --app-config configs/api.local.yaml --workers 4
codingwiki wiki publish --repo myrepo
```

生成过程中可查看事实层：

```bash
codingwiki facts query --repo myrepo --kind symbol --module <module> --limit 20
codingwiki facts query --repo myrepo --kind call --file src/
```

### 4. 建索引并检索

```bash
codingwiki index build --repo myrepo --embedding bge-m3 --app-config configs/default.yaml
codingwiki search --repo myrepo --query "认证入口在哪"
codingwiki ask --repo myrepo --question "认证失败时走哪条错误处理链路"
```

首次没有真实模型时，可用 `--embedding hash` 先跑通链路；评测指标只在真实语义模型下有意义。

### 5. 导出 Wiki

```bash
codingwiki wiki export --repo myrepo
```

把 `.codingwiki/wiki/myrepo/versions/<version>/wiki/` 下的 Markdown 放入 GitHub Wiki 或仓库 `docs/` 即可。

### 6. 生成 Spec 并自动编码

```bash
codingwiki spec generate --repo myrepo --doc requirements/feature.md --id F-001 \
  --backend api --app-config configs/api.local.yaml
codingwiki spec validate .codingwiki/specs/myrepo/F-001
codingwiki spec review .codingwiki/specs/myrepo/F-001 --approve --role architect --ack-warnings
codingwiki coding run --repo myrepo --spec-dir .codingwiki/specs/myrepo/F-001 \
  --task T-001 --backend-config configs/backend.local.yaml
```

---

## 命令参考

| 命令 | 作用 |
|---|---|
| `codingwiki version` | 显示版本 |
| `codingwiki config validate --file <yaml>` | 校验配置文件 |
| `codingwiki repo sync --repo <id>` | 拉取代码并生成快照 |
| `codingwiki facts extract --repo <id>` | 从快照提取代码事实 |
| `codingwiki facts query --repo <id> [--kind ...]` | 查询已落盘事实 |
| `codingwiki wiki plan\|generate\|verify\|publish` | Wiki 规划、生成、复验、发布 |
| `codingwiki wiki export --repo <id>` | 导出 GitHub Wiki / 通用 Wiki 文档组 |
| `codingwiki wiki status\|coverage\|stale\|rollback` | 质量看板、覆盖率、过期标记、版本回滚 |
| `codingwiki index build --repo <id>` | 构建检索索引 |
| `codingwiki search --repo <id> --query <text>` | 混合检索 |
| `codingwiki ask --repo <id> --question <text>` | 带引用问答 |
| `codingwiki eval --repo <id> --golden <yaml>` | 金标准集评测 |
| `codingwiki run --repo <id>` | 日更六阶段闭环 |
| `codingwiki spec extract\|evidence\|generate\|validate\|review` | 开发 Spec 流水线 |
| `codingwiki patch workspace open\|list\|rollback\|close` | 隔离工作区生命周期 |
| `codingwiki patch check` | 补丁路径、规模、安全规则检查 |
| `codingwiki coding task list` | 列出 approved Spec 的任务 |
| `codingwiki coding packet build` | 生成任务包 |
| `codingwiki coding run` | 多轮编码、构建测试与独立验证 |
| `codingwiki coding replay` | 用补丁重放并对比阶段结果 |
| `codingwiki coding report` | 汇总运行指标 |

所有子命令都支持 `--help`，例如 `codingwiki wiki generate --help`。

## 运行时目录

所有运行产物默认落在工作区的 `.codingwiki/` 下（已加入 `.gitignore`），可用 `CODINGWIKI_HOME` 改根目录：

```text
.codingwiki/
├── blobs/                       # 快照内容寻址存储
├── repos/<repo>/work/           # 代码工作树
├── snapshots/<repo>/            # 快照与 manifest
├── facts/<repo>/                # 代码事实层
├── wiki/<repo>/
│   ├── drafts/                  # 草稿区
│   ├── plans/                   # 覆盖计划
│   ├── versions/<version>/      # 页面、站点、导出 Wiki、索引
│   └── current                  # 当前版本指针
├── indices/<repo>/<index_id>/   # 检索索引
├── jobs/<repo>/                 # 日更任务审计记录
├── locks/<repo>.lock            # 仓库并发锁
├── specs/<repo>/                # 需求抽取与 Spec
├── coding/<repo>/               # 编码运行记录
├── worktrees/<repo>/            # 隔离工作树
└── patches/<repo>/              # 补丁记录
```

## 退出码

| 退出码 | 含义 |
|---|---|
| `0` | 成功 |
| `1` | 内部错误 |
| `2` | 输入或配置错误（不重试） |
| `3` | 环境问题或门禁拒绝 |
| `4` | 可重试错误（超时、锁冲突、网络抖动） |

## 配置约定

- 配置文件只接受 YAML；机器交换数据统一使用 JSON。
- 配置模型拒绝未知字段，写错字段会直接报错而不是静默忽略。
- 主配置 `configs/default.yaml` 包含模型、嵌入、重排、检索参数；真实端点与模型名写入 `configs/*.local.yaml`。
- 仓库配置放在 `configs/repos/<repo_id>.yaml`，`root` 用于指定仓库内分析根目录。
- 密钥只通过环境变量名或本机密钥文件路径引用，配置文件本身不保存密钥内容。

## 适用边界与注意事项

- Wiki 页面是模块 / 流程 / 组件级的聚合页，不是逐文件、逐接口的完整 API 手册。
- 问答拒答门槛按具体仓库标定；迁移到差异较大的仓库后，应使用 `codingwiki eval` 重新标定 `retrieval.min_vector_score`。
- 评测指标只在真实语义模型下有意义，`hash` 嵌入仅用于离线跑通链路与 CI。
- 代码源当前支持 git / GitHub；`p4` 在配置模型中保留为预留类型。
- 自动编码的真实模型能力由外部命令后端提供，CodingWiki 负责隔离、约束、检查和验证，不内嵌具体编码 Agent。
- 自动编码失败不会自动回滚工作区，失败证据与升级报告保留在运行目录中，由人工决定下一步。

## 文档

- 架构设计（总体架构 + 各能力实施基线）：[docs/架构设计/](docs/架构设计/README.md)
- 项目开发计划与验收基线：[docs/CodingWiki-项目开发计划.md](docs/CodingWiki-项目开发计划.md)
- 评测金标准集：[evals/golden/](evals/golden/)
- 试点需求文档：[evals/specs/](evals/specs/)

## 开发

```bash
uv sync --extra dev

ruff format .
ruff check .
mypy
pytest
```

版本库只收 `src/`、`configs/`、`tests/`、`evals/` 与随仓库发布的架构文档；`.venv/`、`vendor/`、`.cache/`、`.codingwiki/`、构建产物和本机过程文档均不入库。

## 许可证

本项目使用 MIT 许可证，详见 [LICENSE](LICENSE)。
