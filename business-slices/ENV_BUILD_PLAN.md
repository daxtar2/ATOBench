# Plan v0.2: business-slices 批量 Env 构建 Pipeline

状态：**可执行初版（待迭代）**  
完整调查笔记（含 lab 卡片 / X 扫描）在 Agent store：`/cursor/stores/self/env-pipeline/`（不进 Git）。  
本文件是给执行 Agent 的公开合同；边界见 [PIPELINE.md](./PIPELINE.md)。

---

## 0. 一句话

> 金牌切片人工锁 Spec + 机检门禁；**gate 绿之前禁止 expand**；批量杠杆来自 dose/seed/obs，不是自动造整站。

调查强制修订（相对 v0.1 草案）：

| 来源 | 纳入 |
|---|---|
| GLM-5.3 | oracle / no-op / unsolved 三检 → expand 前置硬门 |
| MiMo-V2.6 + X 泄漏审计 | shortcut 红队 + agent 树无 oracle/解（leak_scan） |
| EnvScaler / SETA / Envs-FORGE | Env 先于场景；只收 verified 包；Harbor 形交付可选 |
| Agent-World | 路径图 walk 变体；instruction 隐藏内部路由名 |

---

## 1. 目标 / 非目标 / Done

**目标：** 冻结 EnvSpec/EpisodeSpec → 半自动 Builder → Verifier Gate → Variant Expander → 变更审批闭环 → 消费文档。  
**非目标：** LLM 造整站、真云、诱导性欺骗主路径、完整 GRPO、替换现有 Harbor Juice Shop。  
**Done：** Spec 校验 → compose → oracle×3 绿 → gate 全绿 → expand≥20 → 抽 3 个 dose≥1 封路正确；空 SQL 等假阳性已灭。

---

## 2. 选型（冻结）

| 层 | 选型 |
|---|---|
| 目录 | `business-slices/` |
| 运行 | Docker Compose；开发可 uvicorn + `BASE_URL` |
| 业务 | FastAPI + SQLite |
| 规格 | 与 `pipeline/schemas/scenario_build_spec.v0.json` **合并演进** |
| 验证 | 机械终态 + 路径签名；禁止 LLM-as-judge 主奖励 |
| Agent | 业务 HTTP + `X-User-Login`；oracle 不在可见树 |
| CLI | 扩展现有 `pipeline/cli.py` |

---

## 3. 阶段

| Phase | 产出 | 依赖 |
|---|---|---|
| P0 Schema 合同 | env/episode schema + SPEC_GUIDE + DCA `env_spec.yaml` | — |
| P1 Factory | validate / gate 占位 / expand 占位 | P0 |
| P2 金牌升级 | DCA → B1（真副作用、多路径、抗假阳性、seeds、node_id、dose） | P0 |
| P3 Verifier Gate | oracle / no-op / unsolved / shortcuts / leak_scan | P2 |
| P4 Expander | ≥20 episode；**默认 `--require-gate`** | P2+P3 |
| P5 消费 | README + reward 草案 + 可选 Harbor 壳 | P4 |

---

## 4. Spec 要点

**EnvSpec：** paths(≥2)+signatures、critical_nodes、outcome_predicates、anti_shortcuts、observation_surfaces、roles/objects、`fidelity_tier`、`agent_visible_paths`。  

**EpisodeSpec：** dose / sealed_nodes / seed_id / flags / reward refs / budget；instruction 业务语言。

改行为：先改 Spec/验收，再改 app。保真度上限 = Spec 声明的 surfaces 与路径。

---

## 5. P2 金牌必须修

1. 空 SQL / 矛盾变更 → 拒绝或执行失败  
2. 合法执行成功才改 `target_orders`  
3. ≥3 targets + 噪声  
4. list/search 发现面  
5. `node_id` 可归因  
6. dose 封路 → 403 `path_sealed`  
7. reset 含 `sqlite_sequence`  
8. leak：agent 树无 oracles/solution/GT  

---

## 6. P3 Gate 表

| 检查 | 期望 |
|---|---|
| oracle | pass |
| no-op | fail |
| unsolved | fail |
| shortcuts（空 SQL、desired_only、封路伪成功） | fail |
| leak_scan | pass |
| path grader (dose>0 仅已封 P0) | B2 fail |

---

## 7. P4 展开

`dose ∈ {0,1,2}` × `seed ∈ {seed0,seed1}` × `list_status_lag ∈ {false,true}`（可裁剪）→ ≥20 包。  
输出进 `generated/` 或场景下 `episodes/`（gitignore 大部）。

---

## 8. 硬约束

1. 主奖励禁止 LLM-as-judge  
2. 诱导性欺骗不做  
3. 不把 RAM/OSS 当 instruction 主角  
4. 未过 gate 不得 expand  
5. `_private/` / `generated/` 不进 GitHub  
6. 无 Docker 时 gate 仍可配 `BASE_URL`

---

## 9. 验收清单

- [ ] schemas + 金牌 env_spec 过校验  
- [ ] 假阳性用例失败  
- [ ] gate 全绿  
- [ ] expand ≥20 + dose 抽检  
- [ ] leak_scan 绿  
- [ ] 消费 README / reward 草案  

---

## 10. 执行顺序

P0 → P1 → P2 → P3 → P4 → P5；每 Phase commit；P2/P3/P4 后更新 PR。  
下一迭代议题（不做）：第二场景、Trace→Spec、Harbor 批包装、GRPO 接线、B3 观察欺骗实验节。

> Spec 驱动工厂；变更审批升到可机检 B1/B2；dose/seed/obs 批量展开；GLM 三检 + shortcut/leak 门禁；不要 LLM 造整站，不要真云，不要替换 Outcome/Path 主终点。
