# ATOBench Judge 元评测反思与完善计划

日期：2026-10-17  
状态：设计定案（框架 + 小步工程；大人标 / PPI 实验另开）  
依据：[LLM_JUDGE_GUIDE.md](./LLM_JUDGE_GUIDE.md)（用户整理的 LLM-Judge 要点）  
范围：Harbor 主终点 + `analysis/` 离线轨迹 Judge；旁及 business-slices PDR  
非目标：本轮不把 LLM Judge 拉回 `reward` 主路径；不重跑全量 450 条人标

---

## 0. 一句话

> ATOBench 的 **架构方向是对的**（Verifier Hybrid + 盲评切包 + 证据指针），但 **元评测与误差传播仍偏粗糙**：主要报了同模型双实例的一致率，缺少 κ / Judge–Human / 按条件拆误差 / 注入探针 / 结论校正。本文件把 guide 的先进做法落成可执行清单。

---

## 1. 仓库核对：guide §13 的校正

| Guide 原述 | 仓库现状（2026-10-17） | 判定 |
|---|---|---|
| \(E\) 确定性、\(C\)/\(S\) 交给 Judge | Harbor `reward_core`：**`reward = G` 全链路机检**（含 `report_closure` / `trace_support`）；LLM Judge **不进 scored path**（见 `harbor/docs/EVALUATION_DESIGN.md`、`REWARD_SPEC.md`） | **已比 guide 更硬**——主终点正确上移到 Verifier |
| 盲评 + forbidden keys + 泄漏扫描 | `analysis/` packet 构建与 validate 阶段有 identity blinding | **已做到** |
| 按维度切包 + Evidence Verifier | `atobench-trajectory-judge` 单维；`atobench-evidence-verifier`；adjudicator | **已做到** |
| 双 Judge + Adjudicator | `reviewer_ids` 二实例 + adjudicator | **半对**：配置明文 `"reviewer_semantics": "isolated_replicate_reviewers_same_model_revision"`（`judge_runner_config.json`）→ **同模型同 prompt 的复本**，非异模型评审团 |
| 主终点与诊断三维分开 | G 机检；诊断维 `verification_control` / `stop_decision` / `report_grounding` | **已做到** |
| 报 κ | `judge_pilot._agreement_summary` 只报 `exact_score_agreement_rate` / `band_agreement_rate` | **缺口确认** |
| Expert–Judge 一致 | AAAI 审稿分析（`docs/AAAI2027_REVIEW_ANALYSIS.md` C4）承认专家已审 450 条但论文未报 | **写作 + 分析缺口**（数据侧可能已有） |
| Test-retest / prompt 哈希 | receipt 有 cost/duration；rubric/prompt 哈希与翻转率未进标准报告 | **缺口确认** |
| 注入探针 | ATO 改写观测；packet 仍含 messages / final_report 转述 | **缺口确认** |

**结论：** 粗糙点不在「该不该用 LLM」，而在 **把 Judge 当已校准仪器使用时的计量学**——guide 的核心立场 #1。

---

## 2. 五层对照（CrazyAllen）→ ATOBench 现状

| 层 | ATOBench 现状 | 缺口 |
|---|---|---|
| **Boundary** | 盲评、单维、`insufficient_evidence`、报告不自证轨迹 | 档位仍偏宽（1–10）；Stop 四态与 1–10 混用；Dangerous Neighbor anchors 不足 |
| **Body plan** | Verifier Hybrid（主终点）+ Atomic 三维（诊断）+ Adjudicator composer | 诊断维 SCORE 均值可掩盖灾难错误；缺显式 Veto（如「假阳性 grounding」） |
| **Sensors** | 三维分开调用；Evidence Verifier 权限清晰 | Judge 仅 `Read` 工具——长轨迹「证伪没发生」易浅搜索 |
| **Expression** | subagent specs + per-dimension rubric.md | rubric 缺 Dangerous Neighbor；行话边界（ready-supported 等）需更死板的例子 |
| **Execution** | 双实例 + 分歧触发 adjudicator；`temperature=not_exposed` | 无 test-retest 翻转率；无级联升级；无异模型第二审 |

---

## 3. 三种一致性：当前证据落在哪一格

```
Human–Human     → 未作为标准产物发布（HH-main / HH-boundary 缺失）
Judge–Judge     → 有（但同模型复本；报 agreement rate 非 κ）
Judge–Human     → 专家审计存在（审稿 C4），未进主分析管线 / 论文主表
```

Guide 警告「**只报 Judge–Judge 就宣称可靠**」——这正是我们最危险的舒适区。同模型双实例 κ 高，最多证明 **稳定**，不证明 **正确**。

---

## 4. 优先缺口（P0–P3）

### P0 — 计量学最小闭环（本 PR / 下一小步可做）

1. **Agreement → κ**  
   在 pilot / validation 汇总中增加：
   - Cohen's κ（精确分档或 band）
   - Quadratic Weighted κ（1–10 有序）
   - 仍保留 raw agreement（对照）
2. **显式标注 reviewer 语义**  
   报告字段写明 `same_model_replicate` vs `cross_model_panel`；禁止把前者写成「独立评审团」。
3. **分层汇总骨架**  
   agreement / κ 按 `dimension × (解盲后) condition × AOU` 分组输出空表结构（解盲后填）。
4. **Guide 入库 + 本反思文档**（本 PR）。

### P1 — 可信度证据（需人标 / 已有专家标签入库）

5. **Expert–Judge 表**  
   把已有 450 条专家审计接进 `validate-judges` 或独立 `judge-metrology` 阶段：JH κ、混淆矩阵、**假阳性格加粗**（unsupported→supported 最贵）。
6. **按 C0/C1 拆 Judge 误差**  
   解盲后分别估 se/sp；检查差异性误分类是否扭曲 Retention / ΔGV。
7. **诊断三维 κ**  
   尤其 `stop_decision` 四态：Weighted κ / Krippendorff α；与 1–10 分数脱钩报告。

### P2 — 鲁棒性与运维

8. **注入 / 长度 / 假引用探针集**（元评测用，不计主终点）→ 报翻转率。  
9. **Test-retest**：同 packet 重跑 k=3，报翻转率；receipt 强制写 model id + rubric sha256 + spec sha256。  
10. **异模型第二审（PoLL 轻量版）**：至少一维用不同族模型复评子集，与主 Judge 对照。  
11. **检索工具（可选）**：对「证伪没发生」类叶子，给 Judge 受限搜索（仍禁 web）；或预计算 inverted index 进 packet。

### P3 — 结论层（论文 / 统计）

12. **Rogan–Gladen / PPI**：当诊断维或任何仍依赖 Judge 的标签进入组间差异时，把 Judge 误差传入 CI。  
13. **主终点坚持机检**：Harbor `reward` 永不接 LLM；若 Arena/RL 用过程 Judge，Bench 侧保留 **holdout Judge**（guide §10）。  
14. **business-slices PDR**：沿用同一 Boundary（盲、单维、指针、abstain）；**禁止** PDR 发 B1/B2 通行证（已有 METRICS_CARD）；上线前走 P0–P1 元评测清单。

---

## 5. 与主终点 / 诊断的边界（冻结）

| 信号 | 判分器 | 能否定义任务成功 |
|---|---|---|
| \(E, C, S\) → \(G\) | 程序 Verifier（Harbor） | **是** |
| Adaptive verification | 合约特化确定性规则 | 是（阶段分量） |
| Verification Control / Stop / Grounding | LLM Judge（离线） | **否**——只解释断裂位置 |
| PDR（business-slices） | LLM Judge（离线） | **否**——B3 诊断 |
| Retention / 配对韧性 | 统计层（条件在机检能力上） | 是；若分母混入 Judge 标签则必须做误差校正 |

---

## 6. 执行切片（给实现 Agent）

### 6.1 本轮（工程）

- [x] 入库 `docs/LLM_JUDGE_GUIDE.md` + 本文  
- [x] `analysis/src/atobench_vr/judge_agreement.py`：κ / weighted κ  
- [x] `judge_pilot._agreement_summary` 接入 `metrology` 块  
- [x] 单元测试（不均衡类别下 agreement 高、κ 低的夹具）  
- [x] `judge_runner_config.json` 增加 `metrology.reviewer_panel_kind`  
- [x] CONCEPTS / EVALUATION_WORKFLOW 链到本文

### 6.2 下一轮（数据 / 论文）

- [ ] 导出 expert labels ↔ final judgment 对齐表  
- [ ] 按 C0/C1×AOU 混淆矩阵 + 假阳性成本表  
- [ ] 论文补 Expert–Judge（回应 AAAI C4）  
- [ ] 探针包设计（注入 / 冗长 / 假引用）规格，不计主终点

### 6.3 明确不做（本阶段）

- 把 LLM Judge 接回 Harbor `reward`  
- 全量重标所有历史 episode  
- 训练专用 Judge 模型  
- 用 Pairwise 替代 Pointwise 做发布决策

---

## 7. 验收（P0 完成时）

1. Pilot / validation JSON 同时含 `exact_score_agreement_rate`、`cohen_kappa`、`quadratic_weighted_kappa`。  
2. 配置与报告标明 `same_model_replicate`，文档禁止称其为独立多模型评审。  
3. 本文与 Guide 在 `docs/` 可发现；`docs/CONCEPTS.md` / `EVALUATION_WORKFLOW.md` 有链接。  
4. 现有 judge 单测全绿。

---

## 8. 给后续实验的「昂贵错误」备忘

对诊断维与任何报告类标签：

- **最贵错误：** 把 unsupported / not-ready 判成 supported / ready-supported（假阳性抬高「看起来恢复了」）。  
- 送审阈值优先保假阳性上限，而不是最大化覆盖率。  
- 解盲后若 C1 的 se/sp 明显差于 C0，Retention 必须带校正或敏感性分析——盲评防不住内容诱发的差异性误分类。
