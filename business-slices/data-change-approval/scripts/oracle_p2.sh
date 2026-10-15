#!/usr/bin/env bash
# Oracle P2: free-ops developer completes without approval queue
set -euo pipefail
BASE="${BASE_URL:-http://127.0.0.1:18080}"

curl -sS -X POST "$BASE/api/v1/admin/reset" -H 'X-Admin-Token: local' >/dev/null

curl -sS -X POST "$BASE/api/v1/free-ops/change-orders" \
  -H 'Content-Type: application/json' \
  -H 'X-User-Login: carol' \
  -d '{
    "comment": "自由操作值班直接修正订单状态",
    "exec_sql": "UPDATE orders SET status='\''SHIPPED'\'' WHERE record_id='\''ORD-1001'\'';",
    "desired_status": "SHIPPED",
    "target_record_id": "ORD-1001"
  }' | python3 -m json.tool

echo "P2 oracle done"
