#!/usr/bin/env bash
# Oracle P1: reject → resubmit → approve → execute
set -euo pipefail
BASE="${BASE_URL:-http://127.0.0.1:18080}"

curl -sS -X POST "$BASE/api/v1/admin/reset" -H 'X-Admin-Token: local' >/dev/null

ORDER=$(curl -sS -X POST "$BASE/api/v1/change-orders" \
  -H 'Content-Type: application/json' \
  -H 'X-User-Login: alice' \
  -d '{
    "comment": "首次提交文案不完整",
    "exec_sql": "UPDATE orders SET status='\''SHIPPED'\'' WHERE record_id='\''ORD-1001'\'';",
    "desired_status": "SHIPPED",
    "target_record_id": "ORD-1001"
  }')
OID=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["order_id"])' <<<"$ORDER")

curl -sS -X POST "$BASE/api/v1/change-orders/$OID/approvals" \
  -H 'Content-Type: application/json' \
  -H 'X-User-Login: bob' \
  -d '{"approval_type":"REJECT","comment":"请补充业务背景"}' >/dev/null

curl -sS -X POST "$BASE/api/v1/change-orders/$OID/resubmit" \
  -H 'Content-Type: application/json' \
  -H 'X-User-Login: alice' \
  -d '{"comment":"客户仓确认已出库，修正状态为 SHIPPED"}' >/dev/null

curl -sS -X POST "$BASE/api/v1/change-orders/$OID/approvals" \
  -H 'Content-Type: application/json' \
  -H 'X-User-Login: bob' \
  -d '{"approval_type":"AGREE","comment":"可以"}' >/dev/null

curl -sS -X POST "$BASE/api/v1/change-orders/$OID/execute" \
  -H 'Content-Type: application/json' \
  -H 'X-User-Login: alice' \
  -d '{}' >/dev/null

curl -sS "$BASE/api/v1/change-orders/$OID" -H 'X-User-Login: alice' | python3 -m json.tool
echo "P1 oracle done"
