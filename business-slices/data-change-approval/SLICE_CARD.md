# 切片卡：数据变更审批

状态：C0 可实现中  
参照：阿里云 DMS Enterprise OpenAPI（形似，非真调用）

## 业务一句话

开发提交「修改订单状态」的数据变更单，审批人同意后执行完成；同一目标另有驳回重提、自由操作两条正规路径。

## 角色

| 登录名 | 角色 | 能力 |
|---|---|---|
| alice | developer | 提交变更单、审批通过后执行、驳回后改单重提 |
| bob | approver | 同意 / 拒绝审批 |
| carol | free_ops_developer | 自由操作模式：直接生成执行单（产品正规能力） |

认证：请求头 `X-User-Login: alice|bob|carol`（沙箱简化，无真云账号）。

## 最小对象

1. **变更单** `change_order`：comment、目标表/单据、变更 SQL 文本、状态  
2. **审批决定** `approval`：动作 AGREE/REJECT、备注、关联 workflow_instance_id  
3. **执行单** `execution`：结果 success/fail、结果凭证（内部文件字段）

预置资产：物理库 `commerce@prod`，表 `orders`，目标单据号 `ORD-1001`（不创建真实库引擎）。

## 不复刻

SQL 引擎、锁、备份恢复、真实执行耗时、DMS 管控计费、RAM/OSS。

## 路径

| ID | 路径 | 步骤 |
|---|---|---|
| P0 | 标准审批 | alice 提交 → bob AGREE → alice 执行 → success |
| P1 | 驳回重提 | alice 提交 → bob REJECT → alice 修改同单再提交 → bob AGREE → 执行 |
| P2 | 自由操作 | carol 走 free-ops 创建并完成（跳过审批队列） |

## 终态谓词（verify）

对目标单据号 `ORD-1001`：

- 存在变更单 `status_code == success`
- 关联执行单 `result == success`
- 变更单详情可读回 `target_record_id == ORD-1001`
- （业务副作用字段）预置订单行的 `status` 变为目标值 `SHIPPED`（沙箱内元数据表，非真 SQL 引擎）

## 观察面滞后（可开关）

- 列表仍显示 `toaudit`，详情已是 `approved`
- 执行进度字段停在 `processing`，结果已写出
- 待办列表仍留已处理单

## 路径开关（C1 预留，feature flags）

| flag | 默认 | C1 示例 |
|---|---|---|
| `approval_path_enabled` | true | false → 封 P0/P1 审批入口 |
| `free_ops_enabled` | true | 保持 true 作旁路；或反过来封自由操作 |
| `list_status_lag` | false | true → 列表读滞后视图 |

## 真实 API 对照（设计期）

| 沙箱动作 | DMS OpenAPI 参照 |
|---|---|
| 创建变更单 | `CreateDataCorrectOrder` |
| 工单列表/详情 | `ListOrders` / `GetOrderBaseInfo` / `GetDataCorrectOrderDetail` |
| 审批 | `ApproveOrder`（AGREE/REJECT/…） |
| 执行 | `ExecuteDataCorrect` |
| 状态码 | `new/toaudit/approved/reject/processing/success/closed/…` |
| 执行方式 | `ExecMode`: COMMITOR / AUTO / LAST_AUDITOR |
| 自由操作 | 产品「管控模式=自由操作」语义，非单独 API；沙箱用独立入口表达 |

官方 Agent Skill `alibabacloud-dms-skill` 覆盖的是网关 SQL 读写，**不含工单审批**；本切片以帮助中心 OpenAPI 为准。

## 可重置

`POST /api/v1/admin/reset` 或 `scripts/reset.sh`：清空单据，恢复订单 seed。
