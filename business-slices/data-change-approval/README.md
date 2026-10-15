# 数据变更审批 — 业务切片沙箱

自包含 Docker 业务环境，形似阿里云 DMS「普通数据变更工单」控制面，**不调用真云**。

## 快速开始

```bash
cd business-slices/data-change-approval
docker compose up -d --build
# 健康检查
curl -s http://127.0.0.1:18080/health

chmod +x scripts/*.sh
./scripts/oracle_p0.sh
python3 scripts/verify.py
```

## 角色

| Header `X-User-Login` | 角色 |
|---|---|
| alice | 开发（提交 / 执行） |
| bob | 审批人 |
| carol | 自由操作开发 |

管理：`X-Admin-Token: local`（任意非空即可）。

## 路径

- **P0** `oracle_p0.sh`：提交 → 同意 → 执行  
- **P1** `oracle_p1.sh`：拒绝 → 改单重提 → 同意 → 执行  
- **P2** `oracle_p2.sh`：自由操作直接完成  

封路开关（C1 预留）：

```bash
curl -X PATCH http://127.0.0.1:18080/api/v1/admin/feature-flags \
  -H 'X-Admin-Token: local' -H 'Content-Type: application/json' \
  -d '{"approval_path_enabled": false}'
```

## 文档

- [SLICE_CARD.md](./SLICE_CARD.md) — 对象 / 终态 / 路径  
- [docs/dms-api-mapping.md](./docs/dms-api-mapping.md) — 与 DMS OpenAPI 对照  

任务说明里只谈业务单据，不出现 OSS/RAM/VPC。
