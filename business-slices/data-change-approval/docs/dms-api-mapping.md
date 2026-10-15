# DMS OpenAPI 对照（设计期参照）

本沙箱**不访问**阿里云；下列对照用于保持状态机与动作语义接近真实产品。

资料来源：

- [CreateDataCorrectOrder](https://help.aliyun.com/zh/dms/developer-reference/api-dms-enterprise-2018-11-01-createdatacorrectorder)
- [ApproveOrder](https://help.aliyun.com/zh/dms/developer-reference/api-dms-enterprise-2018-11-01-approveorder)
- [ExecuteDataCorrect](https://help.aliyun.com/zh/dms/developer-reference/api-dms-enterprise-2018-11-01-executedatacorrect)
- [ListOrders](https://help.aliyun.com/zh/dms/developer-reference/api-dms-enterprise-2018-11-01-listorders)
- [GetOrderBaseInfo](https://help.aliyun.com/zh/dms/developer-reference/api-dms-enterprise-2018-11-01-getorderbaseinfo)
- [GetDataCorrectOrderDetail](https://help.aliyun.com/zh/dms/developer-reference/api-dms-enterprise-2018-11-01-getdatacorrectorderdetail)
- 管控模式说明（自由操作 / 稳定变更 / 安全协同）

| 沙箱 API | DMS 参照 | 备注 |
|---|---|---|
| `POST /api/v1/change-orders` | CreateDataCorrectOrder | 入参含 Comment、ExecSQL、EstimateAffectRows、ExecMode、Db 目标 |
| `GET /api/v1/change-orders` | ListOrders | `view=` ≈ OrderResultType（AS_COMMITTER/AS_HANDLER/AS_ADMIN） |
| `GET /api/v1/change-orders/{id}` | GetOrderBaseInfo + GetDataCorrectOrderDetail | 详情返回 true 状态；列表可滞后 |
| `POST .../approvals` | ApproveOrder | AGREE / REJECT / CANCEL；需 WorkflowInstanceId 语义 |
| `POST .../execute` | ExecuteDataCorrect | COMMITOR / AUTO / LAST_AUDITOR |
| `POST /api/v1/free-ops/change-orders` | 自由操作管控模式 | 产品能力，非单一 OpenAPI；沙箱独立入口 |
| 状态码 | StatusCode | new/toaudit/approved/reject/processing/success/closed/… |

## Agent Skill 检索结论（alibabacloud-find-skills）

检索「DMS / 数据变更工单审批」时，目录内相关 skill 主要是：

- `alibabacloud-dms-skill`：数据网关资产盘点 / SQL 读写  
- `alibabacloud-dms-data-agent-platform-setup`：托管 agent platform 实例  

**均不覆盖工单审批控制面**。因此本切片以 OpenAPI 帮助文档建模，而不是安装上述 skill 当运行时依赖。
