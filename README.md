# CodingWiki

CodingWiki 是一套“代码仓库百科”系统：把一个大型代码仓库自动整理成持续更新、可检索、每句话都能追溯到代码的说明文档（Repo Wiki），并在其上提供检索、问答、Spec 生成和自动编码能力。

项目分三个阶段：

1. Repo Wiki + RAG（代码接入、事实提取、Wiki 生成与校验、检索问答、每日更新）；
2. 开发 Spec 生成（Wiki + 需求文档 → 可供 AI 编程助手执行的规格说明）；
3. 自动编码与测试（按 Spec 改代码、编译、测试、修复，并带自动门禁与人审兜底）。

## 文档说明

完整开发计划、开发记录只保存在本机 `docs/` 目录，**不随仓库发布**。本 README 只做仓库概览。

## 开发约定（摘要）

完整规则见开发计划文档 20.1、20.2 节。要点：

- 所有东西都放在工作区内：依赖在 `.venv/`，下载物在 `vendor/`，缓存在 `.cache/`，构建产物在 `build/`，这些都不进版本库；
- 工作区外只允许写 `/tmp/codingwiki-*`，其他改动必须先申请；
- 版本库只收 `src/`、`configs/`、`tests/` 等源码与配置，不含 `docs/`、`.venv/`、`vendor/`、`.cache/`、`build/`；
- 每个小阶段走分支 → 测试 → PR → 合并 `main`，不直接提交 `main`；
- 新增模块必须有正例、反例、边界例测试，跨模块流程补 E2E。

## 本地开发

```bash
# 激活位于工作区内的虚拟环境
source .venv/bin/activate

# 安装依赖（用锁文件，避免版本漂移）
uv pip install -r requirements.lock

# 检查工作区是否干净
git status --porcelain
```
