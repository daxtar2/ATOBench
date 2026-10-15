# 批量环境构建 Pipeline — 边界与骨架

日期：2026-10-15  
状态：开工（先定边界，再填生成器）  
版本：`pipeline.v0`

---

## 1. 什么进 GitHub / 什么不进

| 进仓库（可 PR） | **不进仓库** |
|---|---|
| 通用 pipeline 代码与 CLI | 批量 **生成出的环境目录**（`generated/`） |
| 公开规格：指标卡、实例 schema、rubric、切片卡模板 | 真实工单 / 内部业务脱敏前材料（`_private/`） |
| 极小 **synthetic fixture**（手写、无产权风险） | 录制/合成的 **dense traces 全集** |
| `.gitignore`、文档里的流程说明 | API 密钥、云账号、Terraform state |
| | Agent store 里的调研笔记（已在 `/cursor/stores/self`） |

原则：**仓库只含「怎么造」；「造出来的那一堆」与「原料隐私」留在本地或 store。**

路径约定：

```
business-slices/
  pipeline/           # IN  — 通用代码
  templates/          # IN  — 公开模板（无真实工单）
  fixtures/tiny/      # IN  — 最小可测输入（合成）
  generated/          # OUT — gitignore，批量产物
  _private/           # OUT — gitignore，本地原料
```

本地私有工作副本也可放：`/cursor/stores/self/env-pipeline/`（永不 git add）。

---

## 2. Pipeline 目标（对齐已冻结评测）

输入一份 **Scenario Build Spec**（场景构建说明），输出一个可重置业务切片，并带上：

- 能支撑 `B1` 的终态谓词与最小路径约束  
- ≥2 条注册旁路签名（为 `B2`）  
- 可挂 hook 的关键节点列表（为 `C1(k)`）  
- 可选：DDC anchor 信号（为 PDR）  

**不**在本 pipeline 里：跑 agent、打 PDR 分、Terraform 真云。

阶段（逻辑）：

```
BuildSpec
  → validate schema
  → materialize seed + API stub surface（从 trace/观察面清单）
  → emit docker-compose app skeleton
  → emit path signatures + hook points + outcome predicates
  → smoke: oracle scripts dry-check（可选）
  → write manifest.json 到 generated/<scenario_id>/
```

批量 = 对 `BuildSpec` 列表 map 上述阶段；失败隔离、可重跑单条。

---

## 3. 与「稍后讨论如何批量」的衔接

当前交付：

1. 目录边界 + gitignore（本文件）  
2. `BuildSpec` JSON Schema（公开、无业务机密）  
3. CLI 骨架：`validate` / `build-one` / `build-batch`（默认输出到 `generated/`）  
4. 一个 **tiny synthetic** fixture 跑通 validate（不含阿里云内部语义）

下一讨论再定：原料从哪来（trace 反建、模板库、半自动 LLM 扩面）——那部分若含内部知识，只进 `_private/` 或 store。

---

## 4. 可执行 Plan（v0.2）

阶段化合同见 **[ENV_BUILD_PLAN.md](./ENV_BUILD_PLAN.md)**（P0 Spec → P1 Factory → P2 金牌 → P3 Gate → P4 Expand → P5 消费）。

调查全文（GLM / MiMo / Agent-World / SETA / X 等）仅在 store：

- `/cursor/stores/self/env-pipeline/SURVEY_FRONTIER_ENV_BUILD.md`
- `/cursor/stores/self/env-pipeline/ENV_BUILD_PLAN.md`（含修订对照表）

硬规则（调查强制）：**Verifier Gate（oracle / no-op / unsolved / shortcuts / leak_scan）绿之前禁止默认 expand。**
