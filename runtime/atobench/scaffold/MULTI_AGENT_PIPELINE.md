# ATOBench Multi-Agent Deception Pipeline — Legacy v1 Wiring

> **状态**：Legacy reference only. Canonical RuntimeProgram v2 wiring is
> `atobench/scaffold/MULTI_AGENT_PIPELINE_v2.md` plus
> `atobench/ATOBENCH_DECEPTION_RUNTIME_ARCHITECTURE_v2.md`.
>
> This v1 document preserves the old `deception_config.yaml` / transformer /
> flag-centric wiring for auditability. Do not use its `flag_regex`,
> `real_flag_reachable`, or writeup-to-config instrumentation notes when running
> new v2 experiments.
>
> **历史定位**：本文档曾是 ATOBench "目标靶场 → deception plan → 注入" 这条链路的权威接线路径。
>
> **读者**：(1) 主 Claude Code 会话（通过 Task tool 调度 subagent），(2) `atobench scaffold` CLI 的 Python orchestrator，(3) 评审者理清"哪个 subagent 干什么"。
>
> **版本**：v1 (2026-07-07)。Supersedes 散落在 `scaffold/orchestrator.py` + `.claude/agents/*.md` + `deception_framework_v3.md` §6.3 中的局部描述。

---

## 0. 一图看懂

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         PRE-EPISODE (生成 deception_config)              │
│                                                                         │
│   targets/<name>/writeup.yaml                                           │
│              │                                                          │
│              ▼                                                          │
│   ┌─────────────────────┐                                               │
│   │ deception-recon     │ → endpoint_inventory.jsonl                    │
│   │ (probe target)      │   {path, methods, response_fields,            │
│   └─────────────────────┘    endpoint_type, auth_required, notes}       │
│              │                                                          │
│              ▼ (optional, only if writeup.target.source_repo set)       │
│   ┌─────────────────────┐                                               │
│   │ deception-static-   │ → endpoint_inventory.jsonl (appended)         │
│   │ analyzer            │   + static_analysis.json                      │
│   │ (read source)       │                                               │
│   └─────────────────────┘                                               │
│              │                                                          │
│              ▼                                                          │
│   ┌─────────────────────┐  reads:                                       │
│   │ deception-planner   │   • endpoint_inventory.jsonl                  │
│   │ (LLM strategy)      │   • writeup.yaml                              │
│   │                     │   • primitive_wiki_v2.md  (51 primitives)     │
│   │                     │   • realism_constraints_v2.yaml               │
│   │                     │   • deception_framework_v3.md §6 (plan schema)│
│   │                     │   • primitive_library.json (registered names) │
│   │                     │ writes:                                       │
│   │                     │   • deception_plan.yaml (v3 §6 schema)        │
│   └─────────────────────┘                                               │
│              │                                                          │
│              ▼                                                          │
│   ┌─────────────────────┐  reads: deception_plan.yaml                   │
│   │ deception-          │  checks:                                      │
│   │ consistency         │   • cross-primitive field consistency         │
│   │ (LLM rubric)        │   • §1.1 flag red lines                      │
│   │                     │   • meta.consistency_lock propagation         │
│   │                     │ writes:                                       │
│   │                     │   • consistency_report.md (pass/fail + fixes)│
│   └─────────────────────┘                                               │
│              │                                                          │
│              ▼                                                          │
│   ┌─────────────────────┐  reads: deception_plan.yaml + consistency     │
│   │ deception-validator │  checks:                                      │
│   │ (static + schema)   │   • JSON schema validation                   │
│   │                     │   • invariants (real_flag_reachable,         │
│   │                     │     injection_log_enabled)                   │
│   │                     │   • primitive_name in registered enum        │
│   │                     │   • stateful primitive has state_machine     │
│   │                     │ writes:                                       │
│   │                     │   • validation_report.md (pass/fail)         │
│   │                     │   • if pass: translate to deception_config   │
│   │                     │     .yaml (proxy-consumable schema 0.1.0)    │
│   └─────────────────────┘                                               │
│                                                                         │
│   artifacts produced:                                                   │
│     targets/<name>/deception_plan.yaml     (人读，v3 §6 schema)          │
│     targets/<name>/deception_config.yaml   (proxy 读，schema 0.1.0)      │
│     targets/<name>/scaffold_work/{recon,static,planner,consistency,     │
│       validator}_prompt.md                                                │
│     targets/<name>/scaffold_work/{endpoint_inventory.jsonl,             │
│       static_analysis.json, consistency_report.md, validation_report.md}│
│     targets/<name>/docker-compose.yml   (rendered from template)        │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                      EPISODE (proxy 注入 + agent 跑)                     │
│                                                                         │
│   atobench run --task T1/T2/T3 --baseline B0/B3 \                          │
│     --deception-config targets/<name>/deception_config.yaml \           │
│     --agent-config examples/agents/claude_code.yaml                     │
│                                                                         │
│   1. docker compose up (target + proxy containers)                      │
│   2. proxy addon reads deception_config.yaml at load() hook             │
│   3. ClaudeCodeAgent spawned via `claude -p` with T1/T2/T3 prompt       │
│   4. agent HTTP traffic → proxy → transformers apply per primitive      │
│   5. turns.jsonl logged per HTTP exchange                              │
│   6. on timeout / completion: EffectVectorEvaluator reads turns.jsonl  │
│      → episode_summaries/<ep_id>.json                                   │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│              MID-EPISODE ADAPTATION — DEFERRED TO NEXT PAPER             │
│                                                                         │
│   deception-coverage-planner (every K turns) + deception-content-       │
│   generator (per turn) — subagent definitions exist in .claude/agents/  │
│   but NOT wired into current pipeline. See ADAPTIVE_GENERATOR_V2_       │
│   DESIGN.md for the next-paper seed.                                    │
│                                                                         │
│   Current pipeline is PRE-EPISODE ONLY (strict-benchmark mode).         │
│   Rationale: memory atobench_multi_llm_generator_benchmark_design.         │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 1. 链路五阶段定义

### Stage 1 — Recon

| 项 | 值 |
|---|---|
| Subagent | `deception-recon` (.claude/agents/deception-recon.md) |
| 输入 | `writeup.yaml`（含 `target.swagger` / `attack_paths[*].key_endpoints` / `ground_truth[*].endpoint` 作为 seed） |
| 输出 | `scaffold_work/endpoint_inventory.jsonl`（每行一个 endpoint：path/method/status/response_fields/auth_required/content_type/endpoint_type/notes） |
| 时间预算 | 5 min |
| 上限 | 50 endpoints |
| 工具 | curl（B0 passthrough，无欺骗） |
| 关键约束 | **不做 exploitation**；recon only；1s 间隔避免压垮 target |
| 失败回退 | swagger 不可达 → 回退到 common paths 列表（/, /api, /admin, /.well-known/, /version 等） |

**endpoint_type 词表**（受控词汇）：见 `deception-recon.md` §4，与 `library.yaml` 的 `fits_endpoint_types` 对齐。planner 依赖此词表做 primitive × endpoint 匹配。

### Stage 2 — Static Analyzer (optional)

| 项 | 值 |
|---|---|
| Subagent | `deception-static-analyzer` (.claude/agents/deception-static-analyzer.md) |
| 触发条件 | `writeup.target.source_repo` 是 git URL（或调用方显式传 `source_repo_path`） |
| 输入 | `source_repo` URL + 现有 `endpoint_inventory.jsonl` |
| 输出 | 追加 hidden routes 到 inventory；额外 `scaffold_work/static_analysis.json`（framework, server_header_tell, hateoas_enabled, state_machine_fields, token_issuance_endpoints, hidden_routes） |
| 时间预算 | 3 min（clone 60s + 分析 120s） |
| 关键约束 | **read-only**，不改 source；大 repo 仅采顶层 |
| 失败回退 | clone 失败 → "skip static analysis"，recon 输出仍可用 |

### Stage 3 — Planner

| 项 | 值 |
|---|---|
| Subagent | `deception-planner` (.claude/agents/deception-planner.md) |
| 输入 | `endpoint_inventory.jsonl` + `static_analysis.json`（可选）+ `writeup.yaml` + **strategy KB** |
| Strategy KB | 1. `atobench/deception_frame/primitive_wiki_v2.md`（51 primitive × 4 coupling 的注入值模板 + 组合规则）<br>2. `atobench/deception_frame/realism_constraints_v2.yaml`（28 prim × 60 constraint 的 realism 边界）<br>3. `atobench/deception_frame/deception_framework_v3.md` §6（plan schema 输出契约）+ §3（F1-F6 attack face）+ §4（M1-M10 family taxonomy）<br>4. `atobench/schema/primitive_library.json`（**registered primitive name enum** — proxy transformer 实际只认这些 name） |
| 输出 | `targets/<name>/deception_plan.yaml`（**v3 §6.1 schema**，含 `plan.injections[].{attack_face, coupling, target_dims, side_dims, trigger, transform, state_machine, rationale}` + `plan.meta.consistency_lock` + `plan.invariants`） |
| Coverage 目标 | ≥3 primitives，5-7 ideal，最多 9（受 `meta.max_primitives_per_response: 2` 限制——这是**每响应**上限不是总配额） |
| LLM 预算 | 5-10 min（planner 是 LLM-heavy 阶段） |
| 关键约束 | 1. **primitive name 必须在 `primitive_library.json` enum 里**——否则 proxy 不认。<br>2. **coupling 默认 `schema_coupled`**（empirically 最强，memory: atobench_schema_coupled_success）。`loose` 已被 0% 效果证伪（memory: atobench_p2_negative_result），仅在显式 control arm 时用。<br>3. **stateful primitive（corrupt_belief, exhaustion_trap, induce_loop）必须带 `state_machine` 字段**——schema 不强制但 transformer 会 malfunction。<br>4. **同一 endpoint 不可被多 primitive 复用**——除非 `path_regex` 足以消歧。<br>5. **rationale 必须引用 wiki 条目或 memory**——非空字符串。 |

**Planner 的 wiki 查阅流程**（关键 — 这是 v2 wiki 上线后的核心新逻辑）：

```
for each endpoint in inventory:
    1. 用 endpoint_type 查 wiki §1-§11 的 Family/Face 索引
       → 候选 primitive 集 P_cand
    for each primitive in P_cand:
        2. 查 primitive_wiki_v2.md §<primitive> 条目
           → 取 cognitive mechanism + 4 coupling 变体的 injected_value 模板
        3. 查 realism_constraints_v2.yaml#<primitive>
           → 取该 primitive 的 hard_block / soft_warn 约束
           → 检查 endpoint 的 stack 是否触发任一 hard_block
              (e.g. Apache 版本号注入到 Node.js target = hard_block stack_mismatch)
        4. 用 writeup.fake_value_sources + endpoint 实际响应做 target-specific 替换
           → Apache/2.4.49 → Express/4.16.0 (若 target 是 Node.js)
        5. 选择 coupling variant：
           - 默认 schema_coupled
           - 若 primitive 在 wiki 标"⚠️ Coupling 独立性告警"且 SR≈loose
             → 跳过 SR variant，只产 SC + precondition
           - 若 endpoint 是 audit/security 类 → 优先 signal_removal（inversion 效应）
        6. 写入 plan.injections[] 条目，含 rationale 引用 wiki 行号 + memory
```

**Planner 输出 schema（v3 §6.1 摘录）**：

```yaml
plan:
  version: "v2"
  plan_id: <uuid>
  created_at: <ISO 8601>
  target: {name, architecture, stack_hint, entry_url}
  task_mode: T1 | T2 | T3
  scoring_profile: ctf_flag | multi_service_flag | audit_report
  flag_regex: <regex or null>
  ground_truth_vulns: [...]
  agent_budget: {max_turns, max_wall_seconds}
  known_real_vulns: [...]
  primary_target_dims: [<dim>, ...]   # 1-3 个
  injections:
    - id: <slug>
      primitive: <registered_name>     # 必须在 primitive_library.json enum
      attack_face: F1|F2|F3|F4|F5|F6
      coupling: schema_coupled | precondition | signal_removal | loose
      target_dims: [...]
      side_dims: [...]
      trigger: {path_regex, methods, request_body_match?, every_n_calls?}
      transform: {type, response_fields, injected_values, removed_fields}
      state_machine?: {name, params}
      rationale: |
        <为什么选这个 primitive × coupling × dim 组合；引用 wiki 行号或 memory>
  meta:
    max_primitives_per_response: 2
    consistency_lock:
      - fields: [...]
        source: <primitive>
        propagate_to: [<primitive>, ...]
  invariants:
    - name: real_flag_reachable
      applies_when: task_mode in [T1, T2]
      check: <路径描述>
    - name: injection_log_enabled
      check: proxy_config has injection_log.jsonl output
```

**注**：v3 §6.1 的 `plan` 顶层结构与 proxy 实际消费的 `deception_config.yaml` (schema 0.1.0) **不同**。前者面向人读 + 评审，后者是 proxy addon 启动时 `load()` 读的机器格式。两者的桥接见 §3 translator。

### Stage 4 — Consistency (NEW)

| 项 | 值 |
|---|---|
| Subagent | `deception-consistency` (.claude/agents/deception-consistency.md) — **新建** |
| 输入 | `deception_plan.yaml`（planner 输出）+ `primitive_wiki_v2.md`（组合规则参考） |
| 输出 | `scaffold_work/consistency_report.md`（pass/fail + 修改建议） |
| 检查项 | 1. **meta.consistency_lock propagation**：声明 `source: phantom_incident_narrative` 的字段（incident_id, cve_id, researcher_name）是否在 `propagate_to` 列表的 primitive 的 `injected_values` 里取了相同值<br>2. **§1.1 flag red lines**：T1/T2 mode 下，flag endpoint 是否被任何 primitive 的 `transform.type=replace` 全量覆盖<br>3. **§8.4 anti-patterns**：<br>   - 单响应多 leak（`meta.max_primitives_per_response` 是否被违反）<br>   - unrealistic 内容（reference wiki 的 realism_constraints 标 ⚑ 的字段）<br>4. **同 endpoint 多 primitive 冲突**：path_regex 重叠时 transformer apply 顺序是否明确 |
| LLM 预算 | 2-3 min |
| 失败回退 | 任意一项 fail → 报告具体违规 + 建议修 plan，不进入 validator |

**注**：framework_v3 §6.3 把 consistency 列为 subagent 分工之一，但 `.claude/agents/` 目前没有这个 subagent 的定义文件。本 pipeline 文档将其作为**新增 stage 4**，需新建 subagent 定义（见任务 #147）。

### Stage 5 — Validator

| 项 | 值 |
|---|---|
| Subagent | `deception-validator` (.claude/agents/deception-validator.md) |
| 输入 | `deception_plan.yaml` + `consistency_report.md` + `primitive_library.json` (schema enum) |
| 输出 | `validation_report.md`（pass/fail per invariant + 建议）+ 若 pass：translate 出 `deception_config.yaml` |
| 检查项 | 1. **JSON schema validation**：plan.yaml 满足 v3 §6.1 schema（注：v3 schema 当前是 doc 形式，未落 JSON Schema — **TODO**, 见 §5 未决项 1）<br>2. **primitive name enum**：所有 `injections[].primitive` 在 `primitive_library.json` 的 registered enum 里<br>3. **stateful primitive has state_machine**：corrupt_belief / exhaustion_trap / induce_loop 必须带 `state_machine`<br>4. **invariants check**：<br>   - `real_flag_reachable`：T1/T2 mode 下 flag endpoint 路径是否被 `injections[].trigger.path_regex` 全量覆盖<br>   - `injection_log_enabled`：deception_config.logging.turns_jsonl 路径非空<br>5. **translator to deception_config.yaml**：把 v3 plan schema 转成 proxy schema 0.1.0 |
| 不做的事 | **不再跑 B0/B3 episodes** — 那是 `atobench sweep` 干的，validator 只做静态检查 |
| LLM 预算 | 1-2 min（可纯 Python 实现，无需 LLM） |
| 失败回退 | 任意 invariant fail → 报告 + 不写 deception_config.yaml |

**Translator 逻辑**（plan → config）：

```python
# plan.injections[i] → config.primitives[i]
{
  "name": plan.injections[i].primitive,
  "coupling": plan.injections[i].coupling,
  "match": {
    "path_regex": plan.injections[i].trigger.path_regex,
    "methods": plan.injections[i].trigger.methods,
    "response_fields": plan.injections[i].transform.response_fields,
  },
  "fake_values": plan.injections[i].transform.injected_values,
  "state_machine": plan.injections[i].state_machine,  # if present
  "stateless": not (plan.injections[i].state_machine is not None),
  "params": plan.injections[i].transform.params,  # if any
}
# plan.target → config.target
# plan.meta, plan.invariants → 不进入 config（人读 only）
# plan.flag (from writeup) → config.flag
# plan.synthetic_endpoints (if any) → config.synthetic_endpoints
# plan.link_advertisements (if any) → config.link_advertisements
```

---

## 2. 与 episode 执行的接口

### 2.1 启动一个 episode

```bash
# 1. 起 target + proxy
docker compose -f targets/<name>/docker-compose.yml up -d
sleep 5

# 2. 起 agent through proxy
atobench run \
  --task T1 \                                 # T1/T2/T3
  --baseline B3 \                              # B0/B1/B2/B3
  --deception-config targets/<name>/deception_config.yaml \
  --agent-config examples/agents/claude_code.yaml \
  --episode-id ep_<hex>

# 3. 评估
atobench analyze --episode-id ep_<hex>
# → logs/episode_summaries/ep_<hex>.json (EffectVector)
```

### 2.2 proxy 在 episode 中的角色

- **load() hook**：读 `deception_config.yaml`，build primitive registry（每条 primitive 绑定一个 transformer）
- **request(flow) hook**：处理 synthetic_endpoints（B0+B3 都注入）；处理 link_advertisements（B0+B3 都注入）
- **response(flow) hook**：5-stage pipeline
  1. `_to_internal_flow` — 标准化 mitmproxy flow
  2. `_inject_flag` — B0+B3 都注入真 flag（**NOT deception**）
  3. `_inject_link_advertisements` — B0+B3 都注入 _links
  4. **[B3 only]** `find_match` + `transformer.apply` — 命中 primitive 的 response 被改写
  5. `_write_back` + `log_flow` — 写回 client + 落 `turns.jsonl`
- **关键设计**：flag FIRST，deception SECOND。schema_coupled 变体的 transform.type=replace 会**整体替换 body**——包括 flag 字段。这就是 deception 效应（agent 看不到 flag，因为它在 fake_version_banner 替换后的 body 里没有）。

### 2.3 EffectVector 评估口径

- 输入：`turns.jsonl`（per HTTP exchange）+ `episodes.jsonl`（episode 级 metadata）+ `final_report`（agent 产出）
- 输出：`logs/episode_summaries/ep_<hex>.json`，含 12 维 EffectVector
- 12 维分组：
  - Layer 1 (behavioral outcome): `flag_rate`, `fca`, `report_recall`, `report_precision`, `fabrication_rate`, `false_positive_vuln_a/b`, `false_negative_vuln`
  - Layer 2 (process trace): `time_stalling`, `resource_thrashing`, `plan_hijack`, `scope_narrowing`, `termination_give_up`
  - Layer 3 (cognitive): `belief_fake_field`, `reception_class`, `cross_turn_persistence`

---

## 3. 多 subagent 调用模型

### 3.1 通过 Claude Code 主会话（canonical）

主 Claude 会话用 Task tool 按 stage 顺序调用 subagent。每 stage 写 prompt 文件 + 传 input path 给 subagent，subagent 写 output 文件后返回。

```
[main session]
  Task(subagent_type=deception-recon, prompt=<recon_prompt.md content>)
    → subagent writes endpoint_inventory.jsonl, returns summary
  Task(subagent_type=deception-static-analyzer, prompt=...)   # optional
    → subagent appends to inventory, returns summary
  Task(subagent_type=deception-planner, prompt=...)
    → subagent writes deception_plan.yaml, returns summary
  Task(subagent_type=deception-consistency, prompt=...)       # NEW
    → subagent writes consistency_report.md, returns summary
  Task(subagent_type=deception-validator, prompt=...)
    → subagent writes validation_report.md + deception_config.yaml, returns summary
```

主会话可在 stage 间插入人类 review（例如 plan 出来后等用户确认再跑 consistency + validator）。

### 3.2 通过 `atobench scaffold` CLI（headless）

```bash
atobench scaffold --target <name> --baseline B3 --n-episodes 3 --invoke-subagents
```

`scaffold/orchestrator.py:ScaffoldOrchestrator` 跑完 5 stages，每 stage 通过 `subprocess.run(["claude", "--print", "--agent", <name>, "--input-file", <prompt>])` shell out 调用 subagent。

**当前状态**：`STAGES = ["recon", "static-analyzer", "planner", "validator"]` — **缺 consistency stage**（任务 #149 待办）。

### 3.3 通过 `atobench generate` CLI（strict-benchmark，无 subagent）

```bash
atobench generate --target <name> --task-id T3 --output targets/<name>/deception_config.yaml
```

`scaffold/multi_llm_generator.py:MultiLLMGenerator` 跑 6-stage Python pipeline（proposal_sampler → convergence_filter → trajectory_fit_filter → coupling_enforcer → primitive_translator → write config）。**不调用 subagent**，直接通过 OpenAI-compatible API 调 3 个 LLM（qwen3.7-max + deepseek-v4-pro + kimi-k2.7-code）。

**何时用**：strict-benchmark 模式（pre-episode generation only, no mid-episode adaptation, no cross-episode memory）。memory: `atobench_multi_llm_generator_benchmark_design`。

**与 subagent pipeline 的关系**：`atobench generate` 是 **subagent pipeline 的 LLM-only 替代**。两者都产出 `deception_config.yaml`（schema 0.1.0），但 `generate` 跳过 recon/static-analyzer/consistency/validator，直接出 config。

---

## 4. 文件依赖图

```
atobench/deception_frame/                    # strategy KB (人读)
  ├── primitive_wiki_v2.md                # 51 primitive × 4 coupling 注入值模板
  ├── realism_constraints_v2.yaml         # 28 prim × 60 constraint realism 边界
  ├── deception_framework_v3.md           # §3 face × §4 family × §6 plan schema × §7 eval
  ├── deception_tricks_catalog_final.md   # curated trick catalog（72 primitive）
  └── build_primitive_index.py            # primitive_index.yaml 再生成脚本

atobench/primitives/                         # registered primitive registry (机读)
  └── library.yaml                        # 9 个有 transformer 实现的 primitive

atobench/schema/                             # JSON Schema
  ├── deception_config.json               # proxy 消费的 config schema (0.1.0)
  ├── primitive_library.json              # primitive name enum (registered)
  ├── writeup.json                        # target writeup schema
  ├── episode_spec.json                   # episode spec schema
  └── episode_summary.json                # EffectVector output schema

atobench/proxy/                              # mitmproxy addon + transformers
  ├── addon.py                            # load/request/response hooks
  ├── transformers/                       # 每 primitive 一个 transformer
  │   ├── base.py                         # PrimitiveTransformer ABC
  │   ├── fake_version_banner.py
  │   ├── vuln_symptom_inject.py
  │   ├── corrupt_belief.py
  │   ├── exhaustion_trap.py
  │   ├── induce_loop.py
  │   ├── substitute_subgoal.py
  │   ├── no_vuln_gaslighting.py
  │   └── ... (one per registered name)
  ├── pattern_matcher.py                  # find_match logic
  └── logging.py                          # log_flow → turns.jsonl

atobench/scaffold/                           # pre-episode generation
  ├── orchestrator.py                     # 5-stage subagent pipeline
  ├── multi_llm_generator.py              # strict-benchmark LLM-only pipeline
  ├── adaptive_generator.py               # mid-episode (deferred)
  ├── convergence_filter.py
  ├── trajectory_fit_filter.py
  ├── coupling_enforcer.py
  ├── primitive_template.py
  ├── proposal_sampler.py
  └── templates/
      ├── docker-compose.yml
      └── writeup.yaml.example

.claude/agents/                           # subagent definitions (Claude Code)
  ├── deception-recon.md
  ├── deception-static-analyzer.md
  ├── deception-planner.md
  ├── deception-consistency.md            # NEW (task #147)
  ├── deception-validator.md
  ├── deception-coverage-planner.md       # mid-episode (deferred)
  └── deception-content-generator.md      # mid-episode (deferred)

targets/<name>/                           # per-target artifacts
  ├── writeup.yaml                        # input (人写)
  ├── docker-compose.yml                  # orchestrator 渲染
  ├── deception_plan.yaml                 # planner 输出 (v3 §6 schema, 人读)
  ├── deception_config.yaml               # validator 翻译 (proxy schema 0.1.0, 机读)
  └── scaffold_work/
      ├── recon_prompt.md
      ├── static_analyzer_prompt.md
      ├── planner_prompt.md
      ├── consistency_prompt.md           # NEW
      ├── validator_prompt.md
      ├── endpoint_inventory.jsonl
      ├── static_analysis.json
      ├── consistency_report.md           # NEW
      └── validation_report.md

atobench/agents/                             # pentest agent adapters
  ├── base.py                             # BaseAgent ABC
  ├── claude_code.py                      # ClaudeCodeAgent (primary)
  └── prompts/
      ├── T1.md  T2.md  T3.md             # task prompts

examples/
  ├── agents/claude_code.yaml             # agent config
  └── sweeps/t1_smoke.yaml                # sweep config

logs/                                     # episode outputs
  ├── turns.jsonl                         # per HTTP exchange
  ├── episodes.jsonl                      # per episode metadata
  ├── turns_by_episode/ep_<hex>.jsonl     # isolated per episode
  ├── episode_summaries/ep_<hex>.json     # EffectVector output
  └── state_ep_<hex>.json                 # state_store dump
```

---

## 5. 未决项 / TODO

1. ~~**v3 §6.1 plan schema 未落 JSON Schema**~~ ✅ 已实现 (2026-07-07)：`atobench/schema/deception_plan.json` (JSON Schema 2020-12, ~200 行) + `validate_deception_plan()` in loader.py + 14 个 schema 测试。deception-validator Check 1 已切到 `validate_deception_plan(plan)`。
2. ~~**plan → config translator 未实现**~~ ✅ 已实现 (2026-07-07)：`atobench/scaffold/plan_to_config.py` (translate + translate_files 函数, 10 个测试)。validator subagent prompt 切到调用真实模块，不再 LLM 现写 Python。verified end-to-end: plan schema ✓ → translate → config schema ✓。
3. ~~**consistency stage 未在 orchestrator STAGES 里**~~ ✅ 已实现 (2026-07-07)：STAGES = [recon, static-analyzer, planner, consistency, validator]。
4. ~~**planner 还引用 `library.yaml` (9 primitives)**~~ ✅ 已实现 (2026-07-07)：planner 现在查 `primitive_wiki_v2.md` (51 prim) + `realism_constraints_v2.yaml` + `deception_framework_v3.md §6` + `primitive_library.json` enum 作为 strategy KB。**但 wiki vs library 的核心 gap 仍在**：wiki 是策略百科 (51+)，library 是可执行注册表 (15)。planner 选 primitive 时查两者交集，未实现的 wiki primitive 写入 `plan.meta.unimplemented_wiki_primitives` 给 paper §X.3。
5. **mid-episode adaptation (coverage_planner + content_generator) 整体 deferred 到下篇 paper**。memory: `atobench_multi_llm_generator_benchmark_design` 决策 strict-benchmark 模式。当前 pipeline 是 pre-episode only。
6. **primitive_library.json 与 library.yaml 不一致**。library.yaml 列 9 个 primitive；primitive_library.json（schema 文件）的 enum 有 15 个（含 cross_turn_jwt_escalation_v2 / decoy_sql_search_v2 等 subtype）。需对齐——以 primitive_library.json 为权威 enum，library.yaml 补齐缺失 subtype 的 strategy 字段。
7. **primitive_index.yaml 已落地** (2026-07-07)：`atobench/deception_frame/primitive_index.yaml` (64 entries) + `build_primitive_index.py` 生成器。subagent 先查索引（~64 行 YAML）筛候选，再按 `wiki_line_range` lazy-load wiki 段落（30-80 行/primitive），不再每次读 2577 行 wiki。

---

## 6. 与既有文档的关系

| 文档 | 重叠点 | 本文档的位置 |
|---|---|---|
| `deception_framework_v3.md` §6.3 | multi-subagent 分工建议 | 本文档是 §6.3 的**实施版**——把"建议"落成 stage 1-5 + 文件路径 + 接线代码 |
| `deception_framework_v3.md` §6.1 | plan schema | 本文档 §1 Stage 3 引用 §6.1 schema |
| `scaffold/orchestrator.py` | STAGES list + prompt builders | 本文档是 orchestrator 的**设计契约**——orchestrator 实现应满足本文档的 stage 定义 |
| `.claude/agents/deception-*.md` | subagent 定义 | 本文档是 subagent 的**调用方契约**——subagent 定义应满足本文档的 input/output 约定 |
| `scaffold/MULTI_LLM_GENERATOR_BENCHMARK_DESIGN.md` | strict-benchmark 模式 | 本文档 §3.3 引用 — `atobench generate` 是 subagent pipeline 的 LLM-only 替代 |
| `scaffold/ADAPTIVE_GENERATOR_V2_DESIGN.md` | mid-episode adaptation | 本文档 §0 红框标 deferred — 下篇 paper 范围 |

---

## 7. 修改本流程的规则

任何对本文档定义的链路的修改必须：

1. **同步更新** `.claude/agents/deception-*.md` 对应 subagent 的 input/output 段
2. **同步更新** `scaffold/orchestrator.py` 的 STAGES / prompt builders / `_invoke_subagent`
3. **在 §5 未决项** 记录新 TODO 或标记已解决项
4. **在 §0 流程图** 标注新 stage / 新文件
5. **写 memory** 记录为什么改（用 `atobench_*` prefix；引用本文档版本号）

修改 v1 → v2 时，**不覆盖**本文档，新写 `MULTI_AGENT_PIPELINE_v2.md`（按用户全局规则"重要策略就写新文件，不要覆盖去写"）。
