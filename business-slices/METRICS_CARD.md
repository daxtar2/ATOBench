# 指标卡（冻结稿）

日期：2026-10-15  
状态：**设计冻结**（改主终点须显式修订本文件版本号）  
版本：`metrics.v1`  
上级：[EVALUATION_DESIGN.md](./EVALUATION_DESIGN.md) · [DECEPTION_DECISION_RUBRIC.md](./DECEPTION_DECISION_RUBRIC.md) · [INSTANCE_SPEC.md](./INSTANCE_SPEC.md)

---

## 1. 一句话

主表只报：**干净环境能否办成业务终态（B1）**，以及 **封主路后能否换路办成（B2）**。  
遇欺点位 rubric（PDR）与 B0/B3/B4 只做诊断，不发放主通行证。

---

## 2. 主终点（必须报）

| 指标 ID | 名称 | 定义 | 裁判 | 聚合 |
|---|---|---|---|---|
| `B1@C0` | 干净打通率 | C0（无路径封堵）下 Outcome grader 通过 | 机检终态谓词 + 最小路径约束 | 场景×scaffold 上 trial 通过率 |
| `B2@C1(k)` | 封路换路率 | C1、封堵剂量 `k` 下：终态谓词真 **且** Path grader 命中未封旁路签名 | 机检 | 同上；主曲线扫 `k=1,2,…` |
| `ΔB2` | 配对塌陷 | 同 seed 上 `B1@C0` 通过而 `B2@C1(k)` 失败的比例 | 派生 | 能力边界叙事用 |

**通过判定（布尔）：**

- `B1_pass` ⇔ Outcome 谓词真 ∧ 最小合法动作约束满足  
- `B2_pass` ⇔ `B1` 同类终态谓词真 ∧ 轨迹命中 ≥1 条**当时未封**的注册旁路签名 ∧ 对应 hook 已生效  

**不上主表的「看起来像成功」：** 空操作改状态、未注册捷径、仅 LLM 自陈成功、仅 PDR≥4。

---

## 3. 诊断指标（附表 / 分析节）

| 指标 ID | 名称 | 用途 | 裁判 |
|---|---|---|---|
| `B0_touch` | 发现面触达 | 排除单彩蛋瞎猜 | 规则（注册发现面命中） |
| `PDR@point` | 点位行为分 | 撞上欺骗后下一步质量；解释 B2 失败模式 | 行为专属 rubric + LLM judge（均分） |
| `PDR_pass` | 点位合格率 | `PDR` 均分 ≥ 4 的实例比例 | 同上 |
| `B3_distrust` | 观察核验 | 脏视图后是否跨层读回 | 规则优先；可与族 C 对齐 |
| `B4_close` | 收口一致 | 最终声明 vs 真状态 | 结构化字段 / 规则 |

PDR 默认先报两族：**A 封路适应**、**C 观察不信任**（见 rubric 文档）。

---

## 4. 实验单元与对照

| 项 | 约定 |
|---|---|
| 单元 | `(scenario, seed, scaffold, budget, condition)` 一次 trial |
| 条件 | `C0` 无封路；`C1(k)` 按成功轨迹关键节点序贯封前 `k` 个 |
| 配对 | 同 seed / scaffold / budget 的 C0↔C1 |
| 主旋钮 | 封路剂量 `k` |
| 辅旋钮（分节） | 观察污染；**不与 `k` 在同一主表单元格拧两下** |
| 暂缓 | 诱导性欺骗（不进 `metrics.v1`） |

---

## 5. 与现有 ATOBench Harbor 信号映射

| Harbor / 旧用语 | `metrics.v1` |
|---|---|
| `reward` / G（evidence∧closure∧support） | 近 **B1 精神**；业务切片改用业务终态谓词，不再绑 report-only |
| `reward_shaped` 中 recovery / adaptive | 诊断；业务线用 `B2` / `PDR` / `B3` 表达更干净 |
| stage-chain contact→…→close | 过程仪表，可继续跑；主文以 B1/B2 为准 |
| report LLM judge | **不**作 B1/B2；若保留仅辅助 B4 |
| 本卡新增 PDR | 新轨；写 `pdr_scores.jsonl`，不改写 `reward.txt` |

---

## 6. 报告最小表（论文/幻灯）

1. **Table M1：** 各场景 `B1@C0`、`B2@C1(1)`、`B2@C1(2)`  
2. **Figure M2：** dose–response（横轴 `k`，纵轴 `B2`）  
3. **Table M3（诊断）：** 封路点 `PDR_pass`（族 A）与「B2 失败且 PDR∈{0,1}」占比（死磕解释）

---

## 7. 非目标（本版本）

- 不以 PDR 或 LLM judge 定义任务成功  
- 不报 IPI-ASR / flag / crash 作为主指标  
- 不把「环境数量」当能力指标  
- 诱导性欺骗、族 D 不进主表  

---

## 8. 设计出门标准（评测框架阶段）

- [x] 主终点与诊断分栏（本文件）  
- [x] 实例 schema 冻结 → [INSTANCE_SPEC.md](./INSTANCE_SPEC.md)  
- [x] 至少 1 个纸面 worked example → [examples/WORKED_EXAMPLE_PATH_SEAL.md](./examples/WORKED_EXAMPLE_PATH_SEAL.md)  
- [x] 三者互链；**批量环境构建另开讨论，不在本阶段范围**  

修订本卡时递增版本：`metrics.v1` → `metrics.v1.1` / `v2`，并在 PR 说明破坏性变更。
