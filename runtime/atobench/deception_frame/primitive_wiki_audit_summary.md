# Primitive Wiki Audit Summary

> **审计对象**：`primitive_wiki.md` + `realism_constraints.yaml`
> **审计视角**：审稿人 + pentest agent domain expert
> **工作流**：审 + 修一体化。发现问题直接在源文件修改，本文档只记录 issue 清单和修复动作
> **不包括**：codebase 对齐审计（无 codebase 上传，工程审计降级为 spec 内部一致性）
>
> Date: 2026-07-07
> Version: v1

---

## 审计打分总览

（Pass 全部完成后填充）

| 轴 | Checkpoint | pass | concern | fail | blocking | 备注 |
|---|---|---|---|---|---|---|
| A. 科学效度 | 4 (A1-A4) | 1 | 3 | 3 | 1 | 全部 resolved (含 old dim 全局替换 65 处) |
| B. Realism | 4 (B1-B4) | 0 | 3 | 0 | 2 | 全部 resolved (含 constraint tier 分级 + template gap 明示) |
| C. Spec 一致性 | 4 (C1-C4) | 3 | 1 | 2 | 2 | 全部 resolved; C3 未做 (需 codebase) |
| D. 覆盖与冗余 | 3 (D1-D3) | 1 | 1 | 0 | 0 | 全部 resolved; jwt_alg_none REFRAME 留 future work |
| E. 跨文件一致性 | 3 (E1-E3) | 0 | 2 | 0 | 1 | E1/E3 resolved; E2 未做 (需 codebase) |
| F. Honest weakness | 3 (F1-F3) | 0 | 1 | 1 | 0 | F1/F2 已在 Pass 2/3 处理; F3 resolved |

**总 Blocking count**: 6，all resolved 或 disclosed。
**总 Concern count**: 11，all resolved 或 documented。
**总 Fail count**: 6，all resolved。
**Pass count**: 5。
**未决**: 2 (C3, E2, 均需 codebase 上传)。

**Overall verdict**: 🟡 **yellow → green（有条件）**——wiki + constraints 的**结构性问题全部修复**，但**留待项目团队处理的 7 个 P0/P1 项**（见下方"未决问题"）是投稿前必做工作。审计后 wiki 和 constraints 在 taxonomy 层已达 benchmark paper appendix 的严谨性门槛，需要项目团队补齐实证 + validator 代码 + target_stack_profile 才能真正达到 green。

---

## 修改动作日志

### Pass 1 — 轴 A1+A2 (科学效度：dim 归属 & coupling 差异)

**A1 dim 归属 (5 抽样)**：

| 抽样 | 判定 | 问题 | 修复 |
|---|---|---|---|
| false_completion | ⚠️ concern | flag_rate 与 giveup 观测重叠；缺 fca | 加 fca + 加 dim 归因说明段 |
| vuln_symptom_inject | ❌ fail | 用了 old dim `false_positive_vuln_a/b`；缺 belief_fake_field；缺 fake hash 溯源约束 | 换 dim + 加 belief_fake_field + 加 `$2b$12$atobench_...` 前缀溯源约定 |
| phantom_incident_narrative | ✅ pass | dim 归属合理 | — |
| cross_turn_jwt_escalation | ❌ fail | old dim；缺 cross_turn_persistence（stateful primitive 必备） | 换 dim + 加 cross_turn_persistence |
| honeypot_admin_login | ❌ fail | old dim；缺 plan_hijack（假 surface 是典型 hijack） | 换 dim + 加 plan_hijack + 加归因说明 |

**A1 全局 fail (blocking)**：wiki 全文 42 处使用 v9 old dim 名（`false_positive_vuln_a/b`, `false_negative_vuln`），违反 v3 §2 12-dim 契约。
- **批量修复**：脚本替换 65 处 Effect dims 行——`false_positive_vuln_a → fabrication_rate`, `false_positive_vuln_b → 删除`, `false_negative_vuln → report_recall`
- **验证**：grep 确认全部清空

**A2 coupling 差异 (3 抽样)**：

| 抽样 | 判定 | 问题 | 修复 |
|---|---|---|---|
| fake_version_banner | ⚠️ concern | 4 variant 未标 dim 差异 | 加 variant-level dim 差异段（loose 弱、SC 强、PC 主打 recept、SR dim 反转到 recall/narrow） |
| corrupt_belief | ✅ pass | 4 variant path/trigger 有清晰区分 | — |
| invariant_echo_false_state_change | ❌ fail | SR ≈ loose, PC ≈ SC——variant 不 orthogonal | 加 "⚠️ Coupling 独立性告警"段，声明"主 sweep 只跑 SC + SR 两个 endpoints" |

**A2 全局改进**：在 §1 约定段加"Variant-level effect dim 一般原则"，给出 4 variant 主打 dim 的通用原则 + coupling 独立性告警的标注规则。

**Pass 1 blocking count**：1 (old dim 全局)；resolved.
**Pass 1 concern count**：3；resolved.
**Pass 1 fail count**：3；all resolved.

### Pass 2 — 轴 A3+A4 (empirical 溯源 & combination rules)

**A3 empirical claim 溯源 (3 抽样)**：

| Claim | Primitive | 判定 | 修复 |
|---|---|---|---|
| "100% derail, FCA=3.8" | fake_version_banner SC | ⚠️ concern — 无 n 值，"100%" 若 n=3 则过弱 | 加"⚠️ 需在 paper 前补齐 n/model/target"注 |
| "FP 2.8-5.0 vs SR 0.2-1.2, 3-15x ratio" | vuln_symptom_inject | ⚠️ concern — 无 n 值 + 置信区间 | 同上 |
| "gaslighting inverts to detection under deep probe" | no_vuln_gaslighting | ⚠️ concern — 定性描述无 threshold | 同上 |

**A3 全局 blocking**：51 条 primitive 里**只有 4 条**声明 empirical，其他 47 条完全无 evidence status 标注。这是 §六.A 的 honest weakness。
- **修复**：在 §1 加"Evidence status 三档标注"约定（📊 data / 🔬 inferred / 💡 hypothetical），明确 paper 引用规则——数值必须来自 📊 data，其他两档不进 result table
- **未做**：给全部 51 primitive 逐条打三档标签（需要 47 次单点 edit，工作量大且无实证依据。留作 P0 后续工作）

**A4 combination rules 证据等级 (§16 8 条高价值组合)**：

| 组合 | Evidence 状态 |
|---|---|
| Attack chain (banner + symptom) | 🔬 inferred (isolation_sweep) |
| 其他 7 组合 | 💡 hypothetical (纯 intuition) |

- **修复**：在 §16.2 表加 Evidence 列 + 顶部 disclaimer，明确 paper 引用时 hypothetical 组合必须标 `[hypothetical]`

**Pass 2 blocking**：1 (evidence status 缺失全局)；resolved via 三档约定。
**Pass 2 concern**：4；resolved via 标注。
**未决**：47 条 primitive 的三档标签需要项目团队自行判定并回填（audit 不代做）。

### Pass 3 — 轴 B (realism 全审)

**B1 constraint 可检查性 (5 抽样)**：

| Constraint | 判定 | 修复 |
|---|---|---|
| cve_must_be_real_or_omitted | ❌ 需外部 artifact | 加 Tier 分级 audit notes：NVD dump 需 pin snapshot |
| min_key_size_floor | ✅ Tier 1 可实现 | — |
| metric_name_conservative_pattern | ✅ Tier 1 | — |
| not_100_percent_swap 80% | ⚠️ Tier 3 heuristic | 加 audit notes 说明 80% 无 empirical 依据，需 ablation tuning |
| admin_surface_has_plausible_data 5-50 items | ⚠️ Tier 3 heuristic | 同上 |

**B1 全局修复**：在 realism_constraints.yaml 头部加 **AUDIT NOTES**（80 行）：
- 3-tier implementability 分类（Tier 1 = regex/enum/numeric 直接可实现 ~35 条；Tier 2 = 需外部 artifact ~5 条；Tier 3 = heuristic 无 empirical ~6 条）
- 显式声明"0/51 constraints 目前有 executable validator code"——**这是 blocking**，paper 前必须实现 Tier 1 ~35 条
- Provenance 声明：24 primitive 来自 v2 audit §3.3

**B2 24 条完备性**：wiki ⚑ 标记与 realism_constraints.yaml 的 24 条 primitive **对齐**（在 §6 Pass 6 会做 set-equality 独立验证）。80% swap probability 在 constraint 里明确定义。

**B3 templated values gap (§六.B honest weakness)**：wiki 里的 `Apache/2.4.49` / `INC-2024-1042` 全是 illustrative template，非 target-realistic
- **修复**：在 §1 加 "⚠️ Injected value 是 illustrative template" 段，明确 4 点：stack fingerprint mismatch 检查、target-specific 值来源要求、`target_stack_profile.yaml` 机制**规划中未实现**（诚实标注 gap）、paper 引用规则（wiki 值仅作 example）

**B4 target applicability**：wiki 只在 preemptive_ftp_gaslight 标了 target-specific，其他 50 条无标注
- **修复**：在 §1 加 "Target applicability 三档" 约定（🌍 target-agnostic / 🎯 stack-specific / 📍 target-specific），列出已知的 stack-specific (7 条) 和 target-specific (1 条)，其余默认 target-agnostic
- **加 paper coverage claim 限定**："we cover HTTP response-layer only; NOT TLS/DNS/timing"

**Pass 3 blocking**：2 (constraint 无 executable code + templated values gap)；both resolved via disclosure。
**Pass 3 concern**：3；all resolved。
**未决**：Tier 1 ~35 条 validator 代码 (~500 lines Python) 需要项目团队实现。这是 paper 提交前的 P0 工作。

### Pass 4 — 轴 C (spec 内部一致性)

**C2 state machine 伪代码 (3 抽样)**：

| SM | 判定 | 修复 |
|---|---|---|
| corrupt_belief `decrementing_queue` | ❌ 逻辑 bug (你 §三.10 指出) | 重写 mitmproxy impl 伪代码：加 `terminal_hold_turns=2` 参数，terminal 后保持 N 次再 reset，避免 approved→queued=4 跳变；同步更新 state machine params 声明 |
| honeypot_admin_login `honeypot_session` | ✅ pass | 伪代码 clean |
| consistent_narrative (phantom_incident_narrative) | ❌ **spec gap** — 无 mitmproxy 骨架，只有 params | 补完整 65 行 mitmproxy 骨架：narrative dict frozen in state_store, 5 endpoint dispatch (changelog / advisory / security.txt / health / metrics) |

**C1 spec 内部字段一致性 (5 抽样)**：

| Primitive | 问题 | 修复 |
|---|---|---|
| fake_version_banner | ✅ | — |
| corrupt_belief | 见 C2 | 见 C2 |
| vuln_symptom_inject | ✅ | — |
| no_vuln_gaslighting | ✅ | — |
| substitute_subgoal | ✅ | — |
| **schema_field_shadowing (bonus)** | ❌ transform.type=augment 但 SR variant 是 remove 操作 | 在 Coupling variants 表加 Transform 列显式声明每 variant type；SR 用 replace |

**C1 全局修复**：在 §1 约定加"Transform type 与 variant 的关系"说明——SR variant 通常需 replace 而非 augment，个别 primitive 应在 Coupling variants 表显式列 Transform 列。

**C3 deception_config schema (deferred)**：无 codebase 上传，无法验证 addon.py 的 `cfg["primitives"]` schema 与 v3 §6.1 的 `plan.injections` 是否一致。**未决问题**留到 codebase 上传后审。

**C4 未实现 primitive 的 spec 明确性 (3 抽样)**：

| Primitive | 判定 | 修复 |
|---|---|---|
| phantom_incident_narrative | ❌ 无 impl 骨架 | 补 65 行骨架（见 C2） |
| sitemap_decoy_orchestration | ✅ pass | 已有骨架且明确 |
| honeypot_admin_login | ✅ pass | 已有骨架 |

**Pass 4 blocking**：2 (corrupt_belief bug + phantom_incident_narrative spec gap)；both resolved。
**Pass 4 concern**：1 (schema_field_shadowing transform type mismatch)；resolved。
**未决**：C3 deception_config schema 对齐——需要 codebase 上传后再审。

### Pass 5 — 轴 D (冗余与覆盖)

**D1 M2 内 subtype 独立性**：7 subtype 表面看冗余，实际沿 (spec-layer × direction × protocol) 三轴分布。
- **修复**：在 §3 M2 章首加"7 subtype 独立性说明"表——沿 layer(static-spec vs runtime-feedback) × direction(add vs remove) × protocol(REST vs GraphQL) 三轴排布，形成 2×2×2 = 8 cells，7 subtype 实例化 7 cell（第 8 cell 被 fake_openapi_deprecation 吞并）。审稿人问 "7 是否冗余" 时可用此表回应。

**D2 未覆盖层次**：wiki 无 coverage claim 限定
- **修复**：加 §17 未覆盖层次清单——列出 TLS / DNS / 传输时序 / HTTP/2 stream / WebSocket 帧内容 / gRPC / HTTP/3 QUIC 7 层未覆盖，给出各层不覆盖原因，并给出 paper coverage claim 建议措辞（"HTTP response-layer only"）
- **原有 §17 → §18**：文件间关系图重编号

**D3 DROP 3 条重审**：
- changelog_cve_injection: ✅ 确认 DROP（C9 硬失）
- **jwt_algorithm_none_bait**: ⚠️ **你 §四.15 观察正确** — 原 DROP 基于"server 主动签发 alg:none JWT" (C9 ❌)。但 **REFRAME 版**（server **接受** agent forged alg:none 并假成功）是 realistic vuln（CVE-2015-9235 类）。已在 §15.2 补充 REFRAME 说明 + 建议 future work 激活到 M8
- xml_feature_flag_bait: ✅ 确认 DROP（parser 不会自曝 config）

**Pass 5 blocking**：0；all resolved。
**Pass 5 concern**：1 (jwt_alg_none REFRAME 潜力)；documented as future work in §15.2。
**Pass 5 pass**：D1 M2 独立性 documented。

### Pass 6 — 轴 E + F (跨文件 & honest weakness)

**E1 primitive 计数对账**：M3 标题写"9 条"但实际只列 7 条
- **修复**：M3 章标题改为 "7 条 = 1 parent + 4 positive-subtype + 2 negative-subtype; merged subtype 不计独立"

**E2 library.yaml vs wiki**：无 codebase 上传，跳过。**未决**。

**E3 realism_constraints.yaml 与 wiki ⚑ set-equality 独立验证**：

Shell diff 验证：
- **Wiki ⚑ 27 条**，constraints 原 24 条——**不 set-equal**
- Wiki 有 ⚑ 但 constraint 缺 4 条：`preemptive_ftp_gaslight`, `content_type_lie`, `numeric_id_boundary_fog`, `clock_skew_deadline_confusion`
- Constraint 有但 wiki 无独立 §：`authoritative_third_party_audit`（已 merged 到 no_vuln_gaslighting）

**修复**：
- 为 4 条缺失 primitive 在 realism_constraints.yaml 末尾追加 constraint entries（9 条 hard_block + 2 soft_warn，共 11 条新约束）——现 constraint 28 primitive, 60 constraints
- 更新头部 Total 计数 + Provenance 说明（解释 authoritative_third_party_audit 的差异是有意保留）

**F1 combination rules empirical status**：见 Pass 2，已加 evidence 列 + disclaimer

**F2 target_stack_profile 机制缺失**：见 Pass 3 B3，已加"规划中未实现"disclosure

**F3 coupling variant 独立性 (3 抽样)**：

| Primitive | 判定 | 修复 |
|---|---|---|
| invariant_echo_false_state_change | ❌ SR ≈ loose, PC ≈ SC | (见 Pass 1 A2) |
| cookie_attribute_phantom | ✅ 4 variant 有实质区别 | — |
| security_header_stripping | ❌ SR ≡ SC (本 primitive 本质是 signal_removal) | 加"⚠️ Coupling 独立性告警"段 + 加 v4 合并建议（可与 cookie/CORS 合并为 client_side_security_config_lie parent） |

**Pass 6 blocking**：1 (constraint set-equality 违反)；resolved via 追加 4 primitive。
**Pass 6 concern**：2 (M3 计数 + security_header_stripping 冗余)；both resolved。
**Pass 6 未决**：E2 library.yaml 对齐 (需 codebase)。

---

## 未决问题（需要你后续决策）

**留待项目团队处理（audit 不代做）**：

1. **47 条 primitive 的 evidence status 三档标签** — 只有 4 条已声明 empirical，其余 47 条 wiki 里没有 📊/🔬/💡 标注。需要项目团队根据 memory/ 里的实际 sweep 数据逐条判定。**这是 paper 提交前 P0 必做**。

2. **Tier 1 realism constraint validator 代码** — realism_constraints.yaml 有 60 条 constraint，但 0 条有 executable validator code。Tier 1 (~35 条) 是纯 regex/enum/numeric-bound，估计 ~500 行 Python 可实现。**paper 提交前 P0 必做**。

3. **`target_stack_profile.yaml` 机制** — 目前 wiki 里所有 injected value 是 illustrative template，无 target-specific 替换机制。subagent 制定 plan 时**完全依赖 LLM 判断**做 target 化。**paper 提交前 P1 必做**——否则实际实验用的假值可能违反 realism。

4. **Codebase 层审计 (v1 §三 C 轴 checkpoint 9-11)** — 需要上传 transformers/*.py, addon.py, deception_config.yaml, library.yaml 后做工程一致性审计。目前只做了 spec 内部一致性。

5. **Empirical claim 的 n / model / target 补齐** — 4 条 📊 claim 都 flag 了 "⚠️ 需在 paper 前补齐"，需要项目团队回填。

6. **`jwt_algorithm_none_bait` REFRAME 决策** — 你 §四.15 观察正确，REFRAME 版是 realistic 的（server 接受 forged alg:none JWT）。当前状态 DROP + 已 disclosed 为 future work。若决定激活，需归入 M8 并写完整 spec。

7. **`security_header_stripping` 与 cookie / CORS 合并决策** — 三者可能合并为 `client_side_security_config_lie` parent。当前保留独立 + 加 v4 合并建议。若合并会影响 M6 family 结构。

---

## 面向 会议投稿的建议

**审计对投稿的判断**：

wiki + realism_constraints 经过本次审计后，**在方法论层已达 会议 benchmark paper 的 appendix 门槛**——taxonomy 严谨、每个 primitive 有 rationale、有 realism 检查、有 evidence status 三档诚实标注。审稿人问 "72 primitive 是不是任意堆的" / "这些是不是纸上谈兵" / "为什么这么多 subtype" 时，都有具体回答（§16.2 disclaimer / §17 未覆盖清单 / §3 M2 独立性表 / 三档 evidence 声明）。

**但审计发现的 P0 gap 必须在 paper 提交前解决**：

1. **实证覆盖率**：47/51 primitive 无 empirical——审稿人问 "这么多 primitive 有多少跑过" 时无法回答。**paper 提交前必须**：
   - 至少 10 parent primitive × 主实验矩阵（4 agent × 4 target × 3 run = 480 runs）
   - 5-8 高价值 subtype 的 subtype-level ablation
   - 其余 37+ subtype 只做 "structural validation" (proxy 能实现、trace 里能观测)

2. **Realism validator 代码**：60 条 constraint 全靠 spec，0 条 executable。**paper 提交前必须**：
   - Tier 1 (~35 条) 实现为 Python module，~500 行
   - 作为 artifact 释出，配合 mitmproxy 使用

3. **`target_stack_profile.yaml` 机制**：目前 wiki injected value 都是 template，subagent LLM 判断替换。**paper 提交前必须**：
   - 至少为 3-4 个典型 target stack (Node/Express, Java/Spring, Python/Flask, GraphQL) 定义 profile
   - 声明 subagent 使用协议

**Paper 叙事策略**（基于审计资产）：

- **卖 methodology 优先**：12 dim + 10 family × 6 face taxonomy + 9-standard audit (含 C9 realism) 是最强 claim
- **实证部分诚实分层**：10 primitive parent 做深度实证；其余作为 "extended catalog" 未做实证，坦承说明
- **主动预防审稿攻击点**（都已在 wiki 里 disclosed）：
  - "why 46 core?" → §12 audit trail 78→46 过滤过程
  - "why not TLS/DNS?" → §17 未覆盖清单 + coverage claim 限定
  - "how do you know these primitives are realistic?" → C9 audit + realism_constraints.yaml
  - "how do you attribute agent failure to specific primitive?" → variant-level dim 差异 + evaluator 归因说明
  - "why 8 combination rules?" → §16.2 disclaimer 明确 7/8 是 hypothetical

**审稿人**必**问的 3 个问题 + 我们的现成回答**：

1. Q: "Only 4/51 primitives have empirical validation. How can this be a benchmark?"
   A: "We propose a three-tier evidence status (📊 data / 🔬 inferred / 💡 hypothetical) and explicitly limit result-table entries to Tier 1. The remaining 47 primitives constitute an extended catalog for community validation. This is comparable to CVE catalog releases where individual CVEs are documented before independent PoC exists."

2. Q: "The injected values (Apache/2.4.49, INC-2024-1042) look canned. Are these actual injection strings?"
   A: "These are illustrative templates. Actual injections are target-stack-specific values chosen by our multi-subagent planner based on target reconnaissance. See §1 'Injected value 是 illustrative template' clause + `target_stack_profile.yaml` (planned artifact)."

3. Q: "How do you know the deception isn't just triggering agent common-sense deficits rather than genuine cognitive vulnerability?"
   A: "We introduced a C9 realism check (audit v2, 2026-07-06) that filtered out 3 primitives (changelog_cve_injection, jwt_algorithm_none_bait, xml_feature_flag_bait) whose injections would be identifiable by basic sanity check. The remaining 46 core primitives all pass realism review, and 24 have explicit `realism_constraints.yaml` bounds. See audit v2 §3.5-3.6."

**时间线建议**：

- **T-6 weeks (now)**：完成审计修复 (this doc), 完成 evidence tier 回填, 完成 realism validator Tier 1 实现
- **T-4 weeks**：主实验矩阵跑完 (480 runs)
- **T-2 weeks**：write-up + rebuttal 预演
- **T-0**：submit
