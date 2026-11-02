# CodingWiki

CodingWiki 是一套“代码仓库百科”系统：把一个大型代码仓库自动整理成持续更新、可检索、每句话都能追溯到代码的说明文档（Repo Wiki），并在其上提供检索、问答、Spec 生成和自动编码能力。

项目分三个阶段：

1. Repo Wiki + RAG（代码接入、事实提取、Wiki 生成与校验、检索问答、每日更新）；
2. 开发 Spec 生成（Wiki + 需求文档 → 可供 AI 编程助手执行的规格说明）；
3. 自动编码与测试（按 Spec 改代码、编译、测试、修复，并带自动门禁与人审兜底）。

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
├── 检索：BM25 + 向量 + 融合重排（先 API 模型，Cline 随后）
└── 问答：只基于检索到的资料回答，资料不足直接拒答
```

设计细节见 `docs/架构设计/`（本机保留，不随仓库发布），入口是 `docs/架构设计/README.md`。

## 当前实现进度

| 阶段 | 状态 |
|---|---|
| S1 骨架 / S2 快照 / S3 事实层 | 已合入 main |
| S4 Wiki 生成、校验、发布 | 已合入 main |
| S5 检索与问答 | 已合入 main |
| S6 真实嵌入与重排 | 已合入 main（bge-m3） |
| S6.5 人可读交付（91 页模块级 Wiki + ApiBackend + 离线站点） | 已合入 main |
| S7 接口缝（FactIndex / SearchFilters / CommandRunner / worktree / stale+rollback） | 已合入 main |
| S9 评测基线（金标准集 cuphy-v1 + EvalHarness + `codingwiki eval`） | 已合入 main（拒答准确率 80% 未达门槛，见下） |
| S9-b 金标准集扩容 50+ 题 → 拒答阈值重标定 | 待开工 |
| S10 调度与锁 / S11 站点与覆盖率看板 | 计划 |
| S8 Cline 适配层 | 暂缓（本机不安装 Cline，公司内配置后接入） |

## 已知问题（待回归）

- **拒答判定对 bge-m3 失效**（S9 实测，基线 cuphy-v1：拒答准确率 80%，4 道拒答题全部未拒答）。
  实测 top1：拒答题向量分 0.45–0.59 且 BM25 rank 2–7；可答题 0.66–0.75。现行规则
  「Top1 无 BM25 命中 且 向量分 < 0.35」两个条件在真实模型下同时失效。
  处理：先扩金标准集到 50+ 题（`evals/golden/cuphy-v1.yaml` 只增不改，新集升版本），
  再用 `codingwiki eval` 重标定阈值或改判定条件；扩容与标定 = 任务 S9-b。
  修复后用评测报告 delta 对比前后。

## 文档说明

完整开发计划、架构设计与开发记录只保存在本机 `docs/` 目录，**不随仓库发布**。本 README 只做仓库概览。

## 开发约定（摘要）

完整规则见开发计划文档 20.1、20.2 节。要点：

- 所有东西都放在工作区内：依赖在 `.venv/`，下载物在 `vendor/`，缓存在 `.cache/`，构建产物在 `build/`，这些都不进版本库；
- 工作区外只允许写 `/tmp/codingwiki-*`，其他改动必须先申请；
- 版本库只收 `src/`、`configs/`、`tests/` 等源码与配置，不含 `docs/`、`.venv/`、`vendor/`、`.cache/`、`build/`；
- 每个小阶段走分支 → 测试 → PR → 合并 `main`，不直接提交 `main`；
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

## 许可证

本项目使用 MIT 许可证，详见 [LICENSE](LICENSE)。
