# ATOBench @ AAAI 2027 审稿意见分析与修订计划

日期：2026-09-24
输入：Reviewer 1（4 分，rejection）、Reviewer 2（5 分，marginally below）、AI reviewer（详细方法学评审）
论文：arXiv:2608.12996v1

---

## 0. 总评

两份人工评审与 AI 评审的**方向层判断一致且正面**：问题重要（"important limitation of
outcome-only evaluation"）、配对设计用心（"careful"、"attempts to isolate"）、过程分析
有价值（"diagnostic value of measuring evidence recovery separately from continued
activity"）。**所有实质批评都落在执行层**：合约混杂、覆盖度、scaffold 混杂、对照缺失、
oracle 定义、judge 审计报告、反事实语言。

关键事实：多数批评对应的机制在 Harbor 迁移后的框架里已经存在或可以低成本构建
（见 §3-A）。本轮修订的核心是把"框架能力"转化为"审稿人要求的受控实验证据"。

## 1. 批判性消化：接受 vs 反驳

### 接受（真问题，必须修）

| # | 批评 | 来源 | 性质 |
|---|---|---|---|
| A | 三个 AOU 同时变化证据类型/剂量/持久性/恢复成本/预算，差异无法归因于证据结构 | R1-W3, R2-W2, AI-W1 | 实验设计 |
| B | 配对是独立采样，非轨迹级反事实；缺锚点前平衡性检验与不确定性量化 | AI-W2 | 分析+写作 |
| C | 摘要结论"恢复依赖于找到证据并保持到报告"是 G 定义保证的循环论证 | AI-W3 | 写作 |
| D | judge 标签有 96.0%/93.6% 互一致率，但未报 expert-judge 一致率（专家已审全部 450 条） | AI-W5 | 写作（数据已有） |
| E | 覆盖度：单目标、单 scaffold、每类一个手工合约 | R1-W4, R2-W1, AI-W6 | 实验+框架 |
| F | scaffold 混杂：五路由共用 Claude Code 2.1.156 | R1-W5 | 实验 |
| G | 缺对照：中性扰动/剂量匹配安慰剂/随机选择器/时长消融/脚本验证器/验证提示基线 | R1-W6 | 框架+实验 |
| H | oracle 未区分：潜在漏洞 / 反事实原生证据 / 可达利用性 | R1-W2 | 写作（机制已有） |
| I | 小样本条件分析（如 22 对 SQLi）的效力限制 | R2-W5 | 写作 |

### 反驳（误读或尺度问题，rebuttal 中澄清）

| # | 批评 | 反驳要点 |
|---|---|---|
| J | "SQLi 0% 只是发现特定 bypass 难"（R2-W3） | 恢复路径的**可发现性本身就是被测维度**；agent 25+ 次尝试、换编码族仍未找到 escape 路径，正是验证纪律失效的表现。但承认单合约无法分离"bypass 难度"与"证据结构"——用剂量阶梯（§3-A1）把 bypass 难度变成可控变量来回答 |
| K | "新颖性增量"（R1-W1, R2-W4） | 定位问题，见 §2 |

## 2. Why：项目定位与独特性（修订稿的叙事骨架）

近邻工作各问一个问题，欺骗在其中的角色各不相同：

| 工作类 | 代表 | 问的问题 | 欺骗的角色 |
|---|---|---|---|
| 网络欺骗防御 | CHeaT (USENIX'25) | 欺骗能否**防御** agent 攻击 | 武器 |
| 环境注入攻击 | ASB (ICLR'25)、AgentLAB 等 | 被污染环境能否**攻击** agent | 攻击向量 |
| 证据落地缺陷 | overtrust 类 (arXiv'26) | agent 是否**盲信**环境证据 | 故障源 |
| 渗透能力评测 | Cybench、ExploitGym、Huang et al. (EMNLP'25) | agent 能否**攻破**目标 | 不存在 |

**ATOBench 问的是第四个问题：当证据通道本身不可信时，agent 的结论还可信吗？**
欺骗在此不是武器、攻击向量或故障源，而是**校准过的探针**——冻结、注册、可回放、
永远保留恢复路径。目标不是骗住 agent，而是测量验证链条在谎言下的断裂位置与方式。

一句话定位（可用于摘要/引言）：

> For autonomous security agents, the epistemic pipeline (observation → hypothesis →
> verification → claim) is the safety-critical component. ATOBench is the instrument
> that measures its integrity under adversarial observation — in both directions:
> concealed evidence (false negatives) and fabricated evidence (false positives).

双向欺骗（隐藏 + 伪造）是修订后最完整的独特性声明，直接回应"我们就是不同"。

## 3. 行动清单

### A. 框架调整（代码/基建）

| # | 项目 | 对应批评 | 现状/工作量 |
|---|---|---|---|
| A1 | **剂量匹配合约矩阵**：3 AOU × dose∈{1, 2, persistent}，同预算 | A, J | SQLi 已有 D0/D1/D2（oracle 验证过）；需新增 basket dose∈{1, persistent}、jwt dose∈{2, persistent} 变体任务。小工作量（任务模板参数化） |
| A2 | **伪造证据 AOU**（deceptive positive）：对错误凭证返回伪造 200+token（SQLi 面）、伪造 basket 归属证据、伪造含 claim 的 token——测 agent 是否接受无效证明 | 双向欺骗叙事, AI 建议1 | 新建 3 个合约（RuntimeProgram 的 synthetic_response 原语直接支持）。中工作量，概念增量最大 |
| A3 | **对照程序包**：中性扰动（只改非证据字段，同剂量）、剂量匹配安慰剂（变换非证据端点）、随机选择器 | G | 新 RuntimeProgram ×3，小工作量 |
| A4 | **scaffold 矩阵**：codex / gemini-cli / scripted-oracle 跑通同一批任务 | F | Harbor 原生支持（`--agent` 参数）；需验证安装链路与 ATIF 落地。小工作量 |
| A5 | **锚点前平衡性检验**：stage-chain 分析器增加 pre-anchor 行为分布对比（签名计数、端点分布）+ paired bootstrap 明确重采样单元与次数 | B | 扩展 `analysis/src/atobench_vr/stage_chain.py`。小工作量 |
| A6 | **统一验证链 schema**：contract-agnostic 的 action–evidence–stop–report 骨架 + 合约特化实现 | AI 建议3 | verifier 已按此结构实现（reward_core + per-AOU analyze），需在文档/论文中显式化 |
| A7 | **验证提示基线**：强调验证纪律的 instruction 变体 | G | 任务复制 + instruction.md 修改。极小工作量 |
| A8 | **第二目标**（crAPI）：rule engine 已有 `crapi_vehicle_relation` 匹配器钩子 | E | 真工作量：compose 目标 + 至少 2 个合约。中期 |
| A9 | **每类第二个独立构建合约**（不同作者/不同构造路径） | E, R2-W2 | 用 authoring pipeline（agent 驱动 deception 设计循环）生成 + 人工评审。中工作量 |

### B. 后续实验（需要跑的新数据）

| # | 实验 | 依赖 | 回答 |
|---|---|---|---|
| B1 | 剂量-响应曲线：3 AOU × 3 剂量 × n 采样 | A1 | A, J——"差异归因于证据结构还是剂量" |
| B2 | 双向欺骗实验：隐藏组 vs 伪造组 | A2 | 双向错误率；agent 是否接受伪造证明 |
| B3 | scaffold × model 矩阵（2-3 scaffold × 2 model × SQLi 合约） | A4 | F——模型效应与 scaffold 效应分离 |
| B4 | 对照实验：中性扰动 / 安慰剂 / 随机选择器 / 验证提示基线 | A3, A7 | G——失败是否源于欺骗性证据本身 |
| B5 | outcome-only vs 阶段链的诊断差异分析（复用现有 450 episodes + 新数据） | 无 | R1-W1 的实证回答：过程视角能看到什么结果视角看不到的 |
| B6 | 第二目标迁移性证据 | A8 | E |

### C. 论文调整（写作/分析）

| # | 修改 | 对应批评 |
|---|---|---|
| C1 | **Oracle 三层定义**正式化：latent vulnerability（后端仍可注入）/ counterfactual native evidence（C0 展示）/ accessible registered evidence（C1 经注册恢复路径可达，由准入控制证明存在）。评测判定第三层 | H |
| C2 | 反事实语言降级："isolates" → "matched cohorts"；新增锚点前平衡性检验结果与配对 bootstrap 细节（重采样单元 = pair，次数明确） | B |
| C3 | 摘要/结论去循环论证：改为报告**哪个分量在哪个合约失败**（实证），删除分量间逻辑依赖的"发现"（定义保证） | C |
| C4 | 补报 expert-judge 一致率、纠正数、分歧 taxonomy；说明哪些标签进入主终点 | D |
| C5 | Related work 补 Huang et al. (EMNLP'25, 渗透 agent 功能属性/过程分析) 与 ASB (ICLR'25, 工具响应中的间接注入)；新颖性收窄并明确为"受控语义证据变换 + 渗透特化的证据-报告重建" | K, AI-W4 |
| C6 | 小样本条件分析全部带 worst–best bounds 与效力说明（已有机制，需突出） | I |
| C7 | 术语一致化：proxy-emulated deception 与 target-side mechanism 的边界表述（§3.1/§4.2） | AI 建议4 |
| C8 | 形式化补丁：四分类 outcome ↔ 分量谓词组合的显式映射；"observed verification state"/"unavailable outcome"操作化定义；§5.3 敏感性分析对齐主终点 GV | AI minor |
| C9 | 新增 source-linked 定性失败 taxonomy（SQLi 策略切换、Basket 适应失败、JWT 矛盾停止、unsupported report 的代表性案例） | AI 建议2 |
| C10 | "Substantial grounded verification" 等措辞与 Table 2 绝对值（40.0%/58.7%）对齐 | AI minor |

## 4. 优先级与排期建议

**第一批（框架，直接回答最强批评）**：A1（剂量矩阵）→ A2（伪造证据）→ A3（对照包）→ A5（平衡性检验）
**第二批（实验）**：B1 → B4 → B2 → B3
**第三批（覆盖度与写作）**：A8/A9 → B6 → C 系列随实验完成同步修订

A1–A5 全部落在现有 Harbor 框架内，不需要新基建；A8 是唯一的中期工程项。
