# LLM Judge Guide

> Source note: user-curated synthesis (CrazyAllen《聊聊评测》、PentestJudge、公开文献).
> Original workspace dump lived at repo-root `LLM-Judge_guide`; this is the
> canonical in-repo copy for ATOBench design work.

开放、主观、标准模糊的 Agent 任务无法只靠可编程 oracle，于是用 LLM 当可规模化的「阅卷老师」。本页的核心立场有三条：

1. **Judge 是一个测量仪器，不是一个 prompt。** 它有偏差、有噪声、会漂移、会被攻击；上线前要像校准仪器一样做元评测，上线后它的误差要进入最终结论的置信区间。
2. **Judge 是多层决策系统。** 能机械核验的先拆出去做确定性检查；剩下的语义判断按「边界 → 架构 → 原子维度 → 表达 → 执行」分层冻结、逐层诊断（CrazyAllen 五层包）。
3. **Judge 的效度会随被评对象变化而衰减。** 换一批更强的模型、把 Judge 拿去当训练奖励、被评内容里混入对 Judge 的指令，都会让之前测得的一致率失效。

本页前半部分（谱系、生命周期、五层、六架构）综合 CrazyAllen《聊聊评测》一二和 PentestJudge；后半部分（偏差、元评测、统计、安全、奖励、运维）来自公开文献，列在文末「外部文献」，**未在本库 ingest，论点按论文自述，数字未复现**。

## 1. 判分器谱系：先决定「谁来判」

从硬到软排列。原则是**能往上挪就往上挪**，LLM 只接住剩下的部分。

| 形态 | 判什么 | 典型 | 主要风险 |
| --- | --- | --- | --- |
| 程序 Verifier | 世界状态、单测、flag、双跑 | Cybench flag、ATOBench 的 \(E\) | 覆盖面窄；失败语义要单独验证 |
| 参考答案引导的 Judge | 输出是否与参考等价 | MT-Bench reference-guided | 照抄参考的表面形式；多解法被误杀 |
| Rubric / Checklist Judge | 逐条 yes/no 或档位 | PentestJudge、HealthBench、PaperBench | 条目歧义；隐含要求推断 |
| Pairwise / Listwise Judge | 相对优劣 | Chatbot Arena、AlpacaEval | 位置与长度偏差；排序 ≠ 可发布 |
| Agent-as-Judge | 带工具读轨迹、查证据 | PentestJudge 搜索工具、Agent-as-a-Judge（DevAI） | 浅搜索；成本高；工具本身可出错 |
| 训练过的 Judge / 生成式奖励模型 | 以上任一，蒸馏成专用模型 | Prometheus 2、JudgeLM、DeepSeek-GRM、J1 | 训练分布外失效；被优化策略攻破 |

关键判断：**Judge 能可靠验证的，基本是它自己大致能做对的东西。** JudgeBench 显示在困难的事实/推理正误对上，强模型 Judge 接近随机；PentestJudge 则显示弱模型能评强模型的长轨迹，因为「这条叶子是否发生」比「把渗透做完」容易得多。两者并不矛盾：**验证比生成便宜，只在验证任务被拆得足够窄时成立。** 这也是要把任务拆成原子叶子的根本理由。

## 2. 生命周期 01–06（CrazyAllen）

| 阶段 | 要点 |
| --- | --- |
| **01 DEFINE** | 判断什么、Judge 能看什么、输出触发什么动作（DecisionContract · SupervisionContract） |
| **02 ISOLATE** | 划分学习 / 验证 / 密封测试，冻结 Leakage Unit |
| **03 INITIALIZE** | 「从对照学原子维度」；选架构、目标与基线 → Frozen Parent |
| **04 DISCOVER** | Parent 残差聚成错误簇；提出互补假设与 Siblings |
| **05 QUALIFY** | 独立取证、过 Gates；更新 Pareto Frontier 与 Parent |
| **06 RELEASE & LEARN** | 分离 Governance / Runtime；发布并回写 Learning Registry → 下一轮 01 |

闭环是 Pareto 选择与知识沉淀，不是单次 prompt 竞赛。**边界冻结后再建 Parent。**

补充两点（来自文献，原文未提）：

- **标准会在看输出的过程中漂移。** EvalGen 的观察是：人往往要先看一批输出，才知道自己真正在意什么（criteria drift）。所以 01 不可能一次写对；正确做法是先用小批真实输出把 Boundary 迭代到稳定，再冻结。冻结点要留记录，之后改 Boundary 等于开新版本。
- **02 的隔离单位是「任务 / 环境」，不是「样本」。** 同一任务的多条轨迹同时出现在 Judge 的调参集和测试集，会把元评测分数抬高。

## 3. 五层决策包（CrazyAllen）

铁律：**上层未冻结，不优化下层。** 每层下面补了文献里对应的常用做法。

1. **Boundary — 输入 / 输出合同。** 看什么证据、输出什么动作、信息缺失如何处理。分数档位宜少（0–3 往往够用）。
   - 补充：输入要尽量对齐人类标注者真正依赖的信息，**也不能多于它**。Judge 看到的身份、条件、模型名等信息就是偏差来源，应在这一层删掉（盲评）。
   - 补充：显式定义「证据不足」的输出（abstain / review），不要逼 Judge 二选一。
2. **Body plan — 架构 / Composer。** 评分、Gate、Route、Veto 如何合成决定；见第 4 节六种架构。
3. **Sensors — 原子维度与权限。** 每个维度观察什么关系，最多能改变什么结论（typed states · action authority）。
   - 补充：**每个维度单独调用一次 Judge**，不要一次输出五个维度的分。一次多维会产生光环效应（halo effect）：一个维度的印象带偏其他维度。PentestJudge 也是每个叶子单独判。
4. **Expression — Prompt / Anchor / Few-shot。** 让模型稳定执行已冻结的边界；**不能替代规则发现**。
   - 先写理由、再给分（CoT before score），但要记住理由可能是事后合理化，不能当证据；证据必须是指向输入内容的指针。
   - 有 logprobs 时可用 G-Eval 式概率加权分 \(\sum_s p(s)\cdot s\)，减少平局和档位挤压。
   - Anchor 要包含 Dangerous Neighbor（只差一个决定性细节、结论相反的例子）。
   - 写法标准：**比给初级实习生的说明还要死板**（PentestJudge）；点名工具、点名边界，不用行话。
5. **Execution — Runtime / 后处理。** 落地为 accept · reject · review。
   - Pairwise 必须正反两个顺序各判一次，两次不一致记为平局或送审。
   - temperature=0 也不保证确定；对关键判断采样 k 次，报告翻转率。
   - 可做级联：便宜 Judge 先判，低置信度再升级到强 Judge 或人（见第 7 节）。

不一致 Case 用「**逐层诊断并定改进策略**」处理，避免补丁式规则膨胀。诊断时先问：是边界没说清（Boundary）、合成逻辑错（Body plan）、维度缺失（Sensors）、模型没执行到位（Expression），还是运行时随机性（Execution）。

## 4. 六种 Rubric 架构（CrazyAllen）

先问决策语义：能否补偿、有无硬边界、绝对还是相对判断、是否要可归因迭代。架构之间可以组合。

| 架构 | 何时用 | 典型失败 | 常见补救 |
| --- | --- | --- | --- |
| **SCORE** 加权多维 | 维度可补偿、错误成本较对称 | 灾难错误被均值掩盖 | 加 Veto；报告各维分布而非只报均值 |
| **Checklist**（ALL） | 合规、格式、必要条件 | 开放价值难穷尽；缺失 ≠ 反证 | 区分「未提及」与「明确违反」 |
| **Gate–Veto–Routes** | 硬边界 + 多条充分路径 | Gate 过严或 OR 过松 | 每条 Route 单独做元评测 |
| **Atomic + Composer** | 要审计、消融、长期迭代 | Schema 设计成本高 | 叶子按结果写，不按手段写 |
| **Pairwise / Listwise** | 排序、优选 | **排序对 ≠ 可发布** | 叠一个绝对 Gate；正反序各判 |
| **Verifier Hybrid** | 事实可机械核验 + 剩余开放语义 | Verifier 覆盖与失败语义须独立验证 | Verifier 失败时降级到 review 而非默认通过 |

Gate–Veto–Routes 的判定式：

```
Accept iff  all required gates pass
        AND no hard veto hits
        AND at least one positive route passes
```

补充：Pointwise 与 Pairwise 的取舍。Pairwise 与人类偏好更一致、分辨力更高，但对长度、语气这类干扰特征更敏感，且 \(n\) 个候选要 \(O(n^2)\) 次比较、可能出现不传递。Pointwise 更稳、可直接给绝对结论，但分数容易挤在中段。**发布 / 准入类决策用 Pointwise + Gate；模型选型 / 排序类用 Pairwise，再用 Bradley–Terry 聚合。**

## 5. 偏差目录

每一项都应该在元评测集里有对应的探针样本。

| 偏差 | 表现 | 缓解 |
| --- | --- | --- |
| **位置偏差** | Pairwise 偏向第一个或第二个 | 正反序各判；不一致记平局 |
| **长度 / 冗长偏差** | 越长越高分 | Rubric 写明不按长度给分；报告分数与长度的相关；排行榜做长度控制回归（AlpacaEval LC） |
| **自偏好** | 偏向同家族模型的输出 | Judge 与被评模型不同族；多家模型组成评审团（PoLL） |
| **权威 / 格式偏差** | 假引用、自信语气、漂亮 markdown 拿高分 | 只认指向证据的指针；扰动测试（CALM 列了 12 类） |
| **宽容偏差** | 看起来合理的错答被放过 | 正误类判断交给 Verifier 或参考答案；Judge 只管语义部分 |
| **参考锚定** | 照参考答案的表面形式打分，误杀另一条正确路线 | 参考当证据用而不是当模板；叶子按结果写 |
| **隐含要求推断** | 「别重复无意义扫描」被理解成「枚举不够彻底」，一个错被两条叶子双杀 | 一条叶子只问一件事；禁止推断未写的要求（PentestJudge） |
| **浅搜索证伪** | 要证明「没发生」时只翻几条就判没发生 | 给检索工具并要求列出搜索过程；用含违规的探针轨迹测召回 |
| **行话歧义** | 「不要 brute force」把离线 hash 破解也判违规 | 点名工具和在线 / 离线（PentestJudge） |
| **刻度挤压** | 1–10 分全挤在 7–8 | 少档位 + 每档 Anchor；概率加权分 |
| **能力天花板** | 被评输出超出 Judge 能验证的范围 | 拆窄；不能拆就别用 Judge 当主终点 |

## 6. 元评测：怎么证明 Judge 可信

### 6.1 指标

- **Agreement 在类别不均衡时虚高**（「全判好」也能高分）。改用 **Cohen's κ**（扣除偶然一致）；有序等级用 **Weighted κ**；多于两名评者或有缺失标注时用 **Krippendorff's α**。
- **κ 悖论**：正类比例极端时，一致率很高 κ 却很低，或者反过来 κ 不稳定。此时 κ 要配合**分类别的 precision / recall** 一起报。
- **报混淆矩阵，并标出哪种错更贵。** 把「不合格判成合格」和「合格判成不合格」分开报；业务上通常前者更贵。
- **实例级 vs 系统级。** 实例级 κ 高不保证模型排名对，反过来也一样。
- **Test-retest。** 同一输入重跑 k 次的翻转率，衡量 Judge 自身的随机性。

### 6.2 三种一致性不能混

| 一致性 | 回答的问题 | 不能回答的问题 |
| --- | --- | --- |
| **Human–Human** | 标准、信息、价值权衡是否稳定 | Judge 好不好 |
| **Judge–Judge**（两个 Judge 实例） | Judge 是否稳定 | Judge 是否正确：同模型、同 prompt 的两个实例错误高度相关，κ 高可能只是一起错 |
| **Judge–Human** | Judge 是否像人 | 人是否对（需要 HH 基线和资深裁决） |

**最常见的漏洞是只报 Judge–Judge κ 就宣称「Judge 可靠」。**

### 6.3 先 Human–Human，再 Judge–Human

- 开放任务上人际一致率往往低于直觉；须多人独立盲标 + 资深裁决。
- **HH-main**：按真实业务分布抽样，估整体一致性。
- **HH-boundary**：过采样冲突样本与低频高代价错例。
- 产出：**Anchor**（含 Dangerous Neighbor、最小决定性差异）和 **review_required**（何时不自动判、交给人）。

### 6.4 元评测集本身要分层

元评测集至少按以下维度分层抽样，每层单独报指标：任务类型 / 难度；实验条件（处理组 vs 对照组）；被评模型族；以及一组**对抗探针**（注入、长度膨胀、假引用、含违规但写得很漂亮的轨迹）。只报总体 κ 会把某一层的崩溃平均掉。

## 7. 选择性判分与 review_required

不必让 Judge 判所有样本。**让 Judge 在有把握时判、没把握时升级**，可以用覆盖率换精度：

- 置信度来源：多次采样的一致程度、logprob、多个 Judge 的分歧、Judge 自报的不确定性（最不可靠）。
- Trust or Escalate：在人标校准集上选阈值，使「Judge 不弃权的那部分」与人类的一致率有统计保证；低于阈值的升级到更强 Judge，再不行交人。
- 运营时监控三条曲线：覆盖率、自动判部分的精度、送审量。

## 8. 长轨迹与 Agent-as-Judge

Agent 轨迹动辄上百次工具调用，整段塞进上下文既贵又会让 Judge 漏看。

- **按维度切包**：每个维度只给它需要的证据切片（ATOBench 的 packet 就是这样）。
- **给 Judge 检索工具**：按输入 / 输出搜工具调用、查工具定义、记中间结论。
- **结论必须带证据指针**（消息 ID、事件 ID），再由独立的 Evidence Verifier 只检查「被引片段是否蕴含该结论」。
- **被评对象的自述不能自证**：Agent 的最终报告说「我验证了 X」不能当作 X 被验证的证据。
- **证「没发生」最难**：需要全量检索；对开源模型要特别测这一项。

## 9. Judge 安全：被评内容是不可信输入

Judge 读的是被评模型产出的文字，被评模型（或环境）完全可以在里面写给 Judge 看的指令。

- **提示注入**、空模型作弊、万能钥匙 token、观察通道被改写、Rubric 泄漏给被评 Agent。

缓解：确定性 Gate 放在 Judge 之前；被评内容加定界符并脱敏；Judge 尽量读结构化摘要而非原文；元评测集里放注入探针并报「被注入后的翻转率」；Rubric 不进 Agent 可见上下文。

## 10. Judge 当奖励信号时

一旦 Judge 分数被拿去优化，Goodhart 效应就会出现。

- **训练用 Judge 与评测用 Judge 分开**，至少评测侧保留一个优化过程从未接触过的留出 Judge。
- **在优化后的策略输出上重新测 Judge–Human 一致性。**
- Rubric 分解成叶子做过程奖励的路线比单一标量更难被攻破，但叶子本身仍要做元评测。

## 11. 从 Judge 标签到实验结论

Judge 的输出是**带误差的测量值**。直接拿 Judge 判出的通过率去算组间差异，会把 Judge 误差混进结论。

- **误分类校正**（Rogan–Gladen）与 bootstrap。
- **差异性误分类**：盲评只能防止 Judge 读到条件标签，防不了两组内容本身不同导致的误差差异。必须在解盲后按条件分别估计 Judge 误差。
- **Prediction-Powered Inference（PPI）**。
- **上限**：评前沿模型时人标无法被 Judge 大幅替代。

## 12. 运维与可复现

- 每条判分记录都带：Judge 模型与版本、prompt / rubric 的哈希、解码参数、证据指针、理由文本。
- 闭源 Judge 会下线或静默更新；关键结论最好有一个可固定权重的开源 Judge 做复评。
- **任何一层的改动都跑一次金标准集回归**，比较新旧 Judge 的混淆矩阵。
- 新一批被评模型进来时，抽样重测 Judge–Human 一致性。
- 成本：叶子数 × 样本数 × 采样次数 × 正反序，很快会爆；用级联和选择性判分控制。

## 13. 对照 ATOBench（导读摘要）

详见同目录 [JUDGE_METROLOGY_REFINEMENT.md](./JUDGE_METROLOGY_REFINEMENT.md) —— 仓库核对后的「已做到 / 缺口 / 行动清单」。原始 guide §13 的推断已在该文用代码证据校正（尤其是 Harbor 主终点 \(G\) 已机检化、双 reviewer 同模型）。
