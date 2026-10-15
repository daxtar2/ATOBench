# business-slices

独立于 Harbor / ATOBench 任务目录的**业务工作流薄切片**沙箱。

**评测框架（先读）：**

| 文档 | 内容 |
|---|---|
| [METRICS_CARD.md](./METRICS_CARD.md) | **冻结指标卡** `metrics.v1`：主终点 B1/B2，诊断 PDR |
| [INSTANCE_SPEC.md](./INSTANCE_SPEC.md) | DDC/PDR 实例 schema `instance.v1` |
| [EVALUATION_DESIGN.md](./EVALUATION_DESIGN.md) | 能力阶梯与 grader 分层总述 |
| [DECEPTION_DECISION_RUBRIC.md](./DECEPTION_DECISION_RUBRIC.md) | 遇欺专属 0–5 量表 |
| [examples/WORKED_EXAMPLE_PATH_SEAL.md](./examples/WORKED_EXAMPLE_PATH_SEAL.md) | 封审批入口纸面样例 |

**批量环境 pipeline：** [PIPELINE.md](./PIPELINE.md) · [pipeline/](./pipeline/)

| 进 GitHub | 不进 GitHub |
|---|---|
| `pipeline/`、`schemas/`、`fixtures/tiny/`、`templates/` | `generated/` 批量产物 |
| 评测/设计 md、手写薄切片 | `_private/` 工单/trace 原料、密钥 |

诱导性欺骗暂缓。

当前切片：

| 目录 | 场景 |
|---|---|
| [data-change-approval](./data-change-approval/) | 数据变更审批（骨架，尚未达 B1 保真度） |

形态：Docker Compose 自包含；真云仅作文档参照。
