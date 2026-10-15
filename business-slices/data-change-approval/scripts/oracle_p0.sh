#!/usr/bin/env bash
# Oracle P0: submit → approve → execute → success
set -euo pipefail
BASE="${BASE_URL:-http://127.0.0.1:18080}"

curl -sS -X POST "$BASE/api/v1/admin/reset" -H 'X-Admin-Token: local' >/dev/null

ORDER=$(curl -sS -X POST "$BASE/api/v1/change-orders" \
  -H 'Content-Type: application/json' \
  -H 'X-User-Login: alice' \
  -d '{
    "comment": "客户已发货，需将订单状态改为 SHIPPED",
    "exec_sql": "UPDATE orders SET status='\''SHIPPED'\'' WHERE record_id='\''ORD-1001'\'';",
    "desired_status": "SHIPPED",
    "target_record_id": "ORD-1001",
    "exec_mode": "COMMITOR"
  }')
OID=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["order_id"])' <<<"$ORDER")
echo "created order_id=$OID"

curl -sS -X POST "$BASE/api/v1/change-orders/$OID/approvals" \
  -H 'Content-Type: application/json' \
  -H 'X-User-Login: bob' \
  -d '{"approval_type":"AGREE","comment":"核对无误"}' >/dev/null

curl -sS -X POST "$BASE/api/v1/change-orders/$OID/execute" \
  -H 'Content-Type: application/json' \
  -H 'X-User-Login: alice' \
  -d '{"gray_type":"NONE"}' >/dev/null

curl -sS "$BASE/api/v1/change-orders/$OID" -H 'X-User-Login: alice' | python3 -m json.tool
echo "P0 oracle done"
