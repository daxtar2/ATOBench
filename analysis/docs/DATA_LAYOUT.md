# Data layout

## 三类数据

### 1. Source data（外部、只读）

- 冻结 campaign 目录；
- agent main/subagent JSONL；
- target-side HTTP/runtime events；
- final reports。

这些数据通常体积最大且可能包含敏感信息，不打包进独立项目。通过
`config/campaign_registry.yaml` 和 `config/project.example.json` 显式挂载。

### 2. Derived workspace（本地、可重建）

每次实验使用一个新的 `workspace/<experiment-id>/`：

```text
01_census/
02_sessions/
03_redacted/
04_alignment/
05_facts/
06_claims/
07_judge_packets/
08_judgments/
09_semantic_matches/
10_episode_states/
11_pair_profiles/
12_statistics_freeze/
13_statistics/
14_result_facts/
15_qa/
17_trajectory_dynamics/
```

完成的 stage 默认不可覆盖。修复应创建新 experiment/version，并记录替换关系。
Guarded execution 使用新的 `workspace/guarded-runs/<unique-campaign>/`，其中的
handoff 和 receipt 只保存 lock/command hashes、状态和退出码，不保存 agent 文本或
runner stdout。

### 3. Reference data（随项目、小而冻结）

`reference/latest/` 是当前 Stage 20 基准快照：

- cohort 中包含 pair profiles、episode states 和 fact registry；
- results 中包含统计、facts 和 trajectory 输出；
- audits 中包含契约修复与统计哈希；
- `learning_data_v1` 包含三种 LearningRecord view、manifest、dataset card 与 audit；
- `learning_transitions_v1` 包含 23,377 条字段名/值均移除的 structured transition、
  manifest、dataset card 与 audit；
- `transition_audit_packets_v1` 包含 60 条确定性分层抽样 packet 和闭集审计协议；
  它只准备人工审计，尚未收集人工结论；
- `counterfactual_trajectory_v1` 包含六类 canonical trajectory object、Replay / 
  Diagnostic / Counterfactual 三个 view、manifest、quality summary 与 export audit；
- 192MB `graph_nodes.jsonl` 未打包，因此 reference 可以复现统计，但不能从零重画
  trajectory dynamics；可使用原 graph 重新生成 transition projection。

### 4. Platform manifests（随项目、冻结声明）

`config/platform/` 不复制外部靶场或 AOU runtime；它只保存稳定 ID、兼容关系、
适配器元数据和外部 artifact hash。`reference/latest/platform/` 保存经验证的
plan-only freeze lock。它能在外部 `atobench/` 邻接目录存在时验证完整链路，但不能
替代 target 或 runtime 本身。

## 安全门

独立版本不再依赖某个用户名或某条 ATOBench 绝对路径识别真实数据。除操作系统临时
目录外，所有输入默认视为 real data，必须显式传 `--allow-real-data`。模型调用
还需要冻结 lock/allowlist 和对应的 call authorization；两种授权互不替代。
