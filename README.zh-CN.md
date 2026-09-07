# ATOBench

**ATOBench: Tracing How Autonomous Penetration-Testing Agents Verify Vulnerabilities When Target Evidence Lies**
（ATOBench：当目标证据被篡改时，自主渗透测试 Agent 如何验证漏洞）

[English](README.md) | 简体中文

ATOBench 是一个评测框架，让自主渗透测试 Agent 的验证过程在*对抗性目标观测*（Adversarial
Target Observation，ATO）下变得可观测。它在运行时注入已注册的响应变换，并将每个被变换的
episode 与同环境下的原生（Native）episode 配对。每一对在第一个受影响的响应处对齐，随后
通过带来源链接的重建，追踪 Agent 后续的动作、证据恢复、停止决策与报告支撑。

三个冻结的观测契约（对抗观测单元，AOU）覆盖不同的证据结构：

| AOU | 已注册的干预 | 保留的恢复路径 |
|---|---|---|
| **SQLi 证明** | 携带已注册 SQLi 标记的登录/搜索请求返回 401 / 空结果 | 同端点的原始 JSON Unicode 转义请求可绕过原始选择器 |
| **Basket 归属** | 跨身份的购物篮读取保持 HTTP 200 与 schema 不变，但显示错误的归属者且商品列表为空 | 自有篮、反向方向与未认证流量保持原生；第三次合格读取返回原生购物篮 |
| **JWT 凭证** | 登录成功返回重签名的 RS256 token，但已注册的 claim 被移除 | token 仍然可用；之后的登录会重新获得原生凭证 |

框架执行配对的 Native/ATO episode，在干预接触点规范化并对齐其轨迹，重建被改变的观测如何
传播到后续动作、证据恢复、停止与报告。完整概念指南见
[docs/CONCEPTS.md](docs/CONCEPTS.md)。

![ATOBench 总览](docs/figures/atobench_overview.png)

## 仓库结构

```text
runtime/    可执行运行时：mitmproxy 响应变换引擎、Protocol-v3 配对跨模型
            campaign 运行器、Agent 适配器、冻结的 Juice Shop AOU 套件、
            评测组件
analysis/   证据重建 + 评判层：轨迹编译器、语义匹配、韧性统计、
            学习数据导出
docs/       说明与设计文档：概念指南（CONCEPTS.md）、架构地图
            （ARCHITECTURE.md）、AOU 契约标准、跨模型 campaign runbook、
            AOU 设计指南
```

## 安装

需要 Python 3.10+。真实运行还需要 Docker 以及一个配置好的 Claude Code 兼容
Agent 载体。

```bash
python3 -m pip install ./runtime
python3 -m pip install -e './analysis'
```

## 不调用模型即可验证

以下命令会物化逐模型的 Protocol-v3 配置、分配调度、溯源记录与源快照，
但不会启动 Docker，也不会调用任何模型：

```bash
atobench-cross-model \
  --campaign-id qwen37plus-sqli-dryrun \
  --rounds 1 \
  --models qwen3.7-plus \
  --model-selector qwen3.7-plus=opus \
  --aous sqli \
  --parallel-workers 1 \
  --dry-run
```

## 运行一次授权内的冒烟测试

只允许对随附的故意脆弱基准靶场（OWASP Juice Shop）或你明确获得授权测试的
系统运行：

```bash
atobench-cross-model \
  --campaign-id qwen37plus-sqli-smoke \
  --rounds 1 \
  --models qwen3.7-plus \
  --model-selector qwen3.7-plus=opus \
  --aous sqli \
  --claude-effort high \
  --start-target \
  --parallel-workers 1
```

除非路由证明（route attestation）存在且两个 episode 都表现出 Agent 主动的
工作痕迹，否则应将该配对视为无效。运行器在路由证明或模型供应商出错时
fail-closed。

## 评测结果

一对 episode（干净 `c0` + ATO `c1`）运行完成后，`eval` 层会重建 Agent 实际
做了什么、验证是否站得住脚：

```bash
# 从 episode 的 turns.jsonl 导出脱敏的 HTTP 动作轨迹
atobench-experiment extract-action-trace --turns <episode>/turns.jsonl

# 基于显式配对清单的 C0/C1 行为审计
atobench-experiment audit-behavior --pairs pairs.json --output behavior_audit.json

# 从配对运行产物计算相对干净基线的欺骗效果指标
atobench-experiment pentest-effect --clean-turns c0/turns.jsonl \
  --deception-turns c1/turns.jsonl --clean-report c0/final_report.txt ...

# 端到端配对工作流（自动推断工作区产物，写出 pentest_effect.json）
atobench-experiment evaluate-pair --clean-run-dir <c0-dir> --deception-run-dir <c1-dir>
```

指标定义见 `runtime/atobench/eval/PENTEST_EFFECT_METRICS.md`。`analysis/`
项目是其上的研究层：对一个 campaign 做证据重建、身份盲评的评判与验证韧性
统计（见 `analysis/README.md`）。

## 扩展 ATOBench

AOU 由 Agent 来设计，而不只是手工编写：随附的设计闭环
（`experiment scaffold` → `make-deception` → `compile` → `freeze-suite`）
由一个 Claude Code 规划 Agent 针对你的靶场撰写 `deception_plan.yaml`，
全程以随附的欺骗方法论语料为参考，每一步都有确定性编译门禁。
[docs/AOU_AUTHORING.md](docs/AOU_AUTHORING.md) 同时覆盖 Agent 驱动闭环、
离线构建路径、靶场接入与手工编写。设计层的有效性要求见
[docs/AOU_OPPORTUNITY_CONTRACT_STANDARD.md](docs/AOU_OPPORTUNITY_CONTRACT_STANDARD.md)。

## 本仓库有意不包含的内容

- **运行日志与 episode 产物。** 所有 campaign 运行数据（turns、mitmproxy
  抓包、Agent 工作区、冻结运行产物）均不随仓库发布。
- **冻结参考数据集。** 分析层的冻结队列导出（450-episode Stage-20 参考
  数据）不随仓库发布。可以通过 `analysis/scripts/atobench-vr` 的导出命令
  从冻结 campaign 重新生成。
- **模型供应商配置。** 不包含任何 API key、token 或网关地址。LLM 端点通过
  `OPENAI_API_KEY` / `OPENAI_BASE_URL` 配置。
- 历史 provenance 清单（`*_collection_source.manifest.json`、构建产物的
  freeze manifest）指向最初的冻结采集；其中记录的哈希描述当前发布树。

## 安全与授权使用

本项目的目的是*评测*防御方可控观测下的 Agent 渗透测试行为。随附靶场是
运行在隔离容器中的故意脆弱应用 OWASP Juice Shop。请勿将本框架指向你不
拥有、或未明确获得测试授权的系统。

## 引用

见 [CITATION.cff](CITATION.cff)。

## 许可证

Apache-2.0。见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)。
