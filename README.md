# CodingWiki

CodingWiki 是一套“代码仓库百科”系统：把一个大型代码仓库自动整理成持续更新、可检索、每句话都能追溯到代码的说明文档（Repo Wiki），并在其上提供检索问答、开发 Spec 生成和自动编码能力。

项目分三个阶段，**三个阶段的流水线已全部交付**：

1. Repo Wiki + RAG（代码接入、事实提取、Wiki 生成与校验、检索问答、每日更新）——已验收；
2. 开发 Spec 生成（Wiki + 需求文档 → 可供 AI 编程助手执行的规格说明）——已交付；
3. 自动编码与测试（按 Spec 改代码、编译、测试、修复，并带自动门禁与人审兜底）——已交付。

## 一阶段的交付形态

一阶段交付的不是一堆 Markdown 文件，而是**一个能读的站点 + 一套可校验的知识资产**：

```
站点（可分版本浏览、可离线打开）
├── 项目总览 / 架构
├── 模块页（每个模块一页：职责、关键结构、入口、依赖、常见改动）
├── 核心流程页（跨文件的端到端链路）
├── 接口 / 配置 / 构建 / 测试 / 术语表
└── 索引与统计（页面清单、覆盖率、快照、证据有效率）

配套能力
├── 每句话带 [E] 出处，可点击回到 file:line；校验器逐条核对
├── 检索：BM25 + 向量（bge-m3）+ RRF 融合 + cross-encoder 重排
├── 问答：只基于检索到的资料回答，资料不足直接拒答
├── 导出：一键导出 GitHub Wiki / 通用 Wiki 文档组（Mermaid 依赖图、索引页）
└── 日更：codingwiki run 六阶段回流闭环（sync → facts → wiki → verify → publish → index）
```

## 当前实现进度

| 阶段 | 状态 |
|---|---|
| 一阶段 S1–S7（骨架/快照/事实层/Wiki 生成校验发布/检索问答/bge-m3 语义检索/接口缝） | 已合入 main |
| 一阶段 S9/S9-b 评测基线（cuphy-v2 金标准 56 问：Recall@10=100%、出处有效率 100%、拒答准确率 94.6%） | 已合入 main |
| 一阶段 S10/S11（JobRunner 日更闭环 + 并发锁 / 站点覆盖率看板 C0–C3） | 已合入 main |
| 一阶段验收（真实仓库 cuphy 端到端：191 页 Wiki，C1 99.8% / C2 95.4% / C3 100%） | 已通过 |
| 二阶段 S12–S16（需求接入 / 检索与影响分析 / Spec 生成流水线 / 自动校验 / 评审与 3 试点） | 已合入 main |
| 二阶段 S17 覆盖补强 | 已合入 main |
| 三阶段 S18–S29（补丁协议与工作区 / 编码任务包 / 构建测试编排 / 独立验证 / 失败升级 / 命令审计 / 补丁重放 / 运行指标 / JUnit / 预算熔断 / flaky 隔离 / 交付包） | 已合入 main |
| 三阶段 S30–S35（资源限制 / 增量构建 / 补丁内容安全 / 测试先行红门禁 / 依赖变更护栏 / 破坏性操作护栏） | 已合入 main |
| S36 Repo Wiki 导出（GitHub Wiki / 通用 Wiki 文档组） | 已合入 main |
| S8 Cline 适配层 | 暂缓（等公司内 Cline 环境，不阻塞其余能力；模型后端已有 mock / api 两条路径） |
| P4 代码源接入 | 暂缓（等公司内环境；当前支持 git / GitHub） |

全量测试 670 条全绿（ruff + mypy 严格模式 + pytest 质量门）。

## 已知限制

- 拒答阈值按 cuphy 单仓库实测标定（`retrieval.min_vector_score=0.595`），迁移到差异较大的仓库需用 `codingwiki eval` 重标定；
- Wiki 页面为模块/流程/组件级聚合页（一阶段验收决策），逐文件/逐接口级说明的进一步细化在后续优化中进行；
- 三阶段已在真实仓库（cuphy）完成全链路 dry-run 与门禁验证，真实模型编码后端（Cline headless）待公司内环境接入。

## 文档说明

- 架构设计（三阶段总体 + 各阶段实施基线）随仓库发布，见 [docs/架构设计/](docs/架构设计/README.md)；
- 完整开发计划随仓库发布，见 [docs/CodingWiki-项目开发计划.md](docs/CodingWiki-项目开发计划.md)；
- 开发记录、任务卡、验收报告等过程文档只保存在本机 `docs/`，不随仓库发布。

## 开发约定（摘要）

完整规则见开发计划文档 20.1、20.2 节。要点：

- 所有东西都放在工作区内：依赖在 `.venv/`，下载物在 `vendor/`，缓存在 `.cache/`，构建产物在 `build/`，这些都不进版本库；
- 工作区外只允许写 `/tmp/codingwiki-*`，其他改动必须先申请；
- 版本库只收 `src/`、`configs/`、`tests/`、`evals/` 与随仓库发布的架构文档，不含 `.venv/`、`vendor/`、`.cache/`、`build/` 及本机过程文档；
- 每个小阶段走分支 → 测试 → 合并 `main`，不直接提交 `main`；
- 新增模块必须有正例、反例、边界例测试，跨模块流程补 E2E。

## 本地开发

```bash
# 激活位于工作区内的虚拟环境（由 uv 创建，Python 3.12）
source .venv/bin/activate

# 安装运行与开发依赖（用 uv.lock 保证版本一致）
uv sync --extra dev

# 本地检查
ruff check .
ruff format --check .
mypy
pytest

# 校验配置
codingwiki config validate --file configs/default.yaml
```

常用命令：

```bash
codingwiki repo sync --repo cuphy          # 拉取代码并建快照
codingwiki facts extract --repo cuphy      # 提取代码事实
codingwiki wiki plan|generate|publish      # Wiki 生成与发布
codingwiki wiki export --repo cuphy        # 导出 GitHub Wiki 文档组
codingwiki index build --repo cuphy --embedding bge-m3
codingwiki search --repo cuphy "query"
codingwiki ask --repo cuphy "question"
codingwiki run --repo cuphy                # 日更回流闭环
codingwiki spec generate|check|review      # 二阶段 Spec
codingwiki code plan|run|status            # 三阶段自动编码
```

## 许可证

本项目使用 MIT 许可证，详见 [LICENSE](LICENSE)。
