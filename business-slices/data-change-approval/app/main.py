"""Data Change Approval business slice — DMS-inspired control plane.

Business language APIs. Status codes and action names mirror DMS Enterprise
OpenAPI (CreateDataCorrectOrder / ApproveOrder / ExecuteDataCorrect / ListOrders)
but no real Alibaba Cloud calls are made.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from db import db
from seed import get_flag, now, reset_database, set_flag, user_by_id, user_by_login

app = FastAPI(title="Data Change Approval Slice", version="0.1.0")


# ---------- auth ----------

class Actor(BaseModel):
    id: int
    login: str
    display_name: str
    role: str


def current_user(x_user_login: str = Header(..., alias="X-User-Login")) -> Actor:
    with db() as conn:
        row = user_by_login(conn, x_user_login.strip().lower())
        if not row:
            raise HTTPException(401, f"unknown user login: {x_user_login}")
        return Actor(
            id=row["id"],
            login=row["login"],
            display_name=row["display_name"],
            role=row["role"],
        )


def optional_admin(x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token")) -> None:
    # Sandbox admin; any token or missing is ok for local slice. Require presence for write.
    if x_admin_token is None:
        raise HTTPException(401, "X-Admin-Token required for admin endpoints")


# ---------- models ----------

class CreateChangeOrderRequest(BaseModel):
    comment: str = Field(..., description="业务背景")
    classify: str = "订单状态修正"
    target_db: str = "commerce@prod"
    target_table: str = "orders"
    target_record_id: str = "ORD-1001"
    exec_sql: str
    desired_status: str = "SHIPPED"
    estimate_affect_rows: int = 1
    exec_mode: Literal["COMMITOR", "AUTO", "LAST_AUDITOR"] = "COMMITOR"


class ResubmitRequest(BaseModel):
    comment: Optional[str] = None
    exec_sql: Optional[str] = None
    desired_status: Optional[str] = None
    classify: Optional[str] = None


class ApproveRequest(BaseModel):
    approval_type: Literal["AGREE", "REJECT", "CANCEL"]
    comment: str = ""


class ExecuteRequest(BaseModel):
    gray_type: Literal["NONE", "FIRST", "EACH"] = "NONE"


class FreeOpsCreateRequest(CreateChangeOrderRequest):
    """自由操作：具备职责的开发直接完成变更（跳过审批队列）。"""
    pass


class FeatureFlagsPatch(BaseModel):
    approval_path_enabled: Optional[bool] = None
    free_ops_enabled: Optional[bool] = None
    list_status_lag: Optional[bool] = None
    todo_lag: Optional[bool] = None


# ---------- helpers ----------

STATUS_DESC = {
    "new": "新建",
    "toaudit": "审批中",
    "approved": "审批通过",
    "reject": "审批拒绝",
    "processing": "执行中",
    "success": "执行成功",
    "fail": "执行失败",
    "closed": "已关闭",
    "cancel": "已撤销",
}


def _next_workflow_id(conn) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(workflow_instance_id), 1000) + 1 AS n FROM change_orders"
    ).fetchone()
    return int(row["n"])


def _order_dict(conn, row, *, for_list: bool = False) -> dict[str, Any]:
    committer = user_by_id(conn, row["committer_id"])
    status = row["status_code"]
    if for_list and row["list_status_code"]:
        status = row["list_status_code"]
    exec_row = conn.execute(
        "SELECT * FROM executions WHERE order_id = ?", (row["order_id"],)
    ).fetchone()
    approvals = conn.execute(
        "SELECT * FROM approvals WHERE order_id = ? ORDER BY id",
        (row["order_id"],),
    ).fetchall()
    out: dict[str, Any] = {
        "order_id": row["order_id"],
        "workflow_instance_id": row["workflow_instance_id"],
        "comment": row["comment"],
        "classify": row["classify"],
        "plugin_type": row["plugin_type"],
        "status_code": status,
        "status_desc": STATUS_DESC.get(status, status),
        "true_status_code": row["status_code"],
        "committer": committer["login"] if committer else None,
        "committer_id": row["committer_id"],
        "target": {
            "db": row["target_db"],
            "table": row["target_table"],
            "record_id": row["target_record_id"],
        },
        "exec_sql": row["exec_sql"],
        "desired_status": row["desired_status"],
        "estimate_affect_rows": row["estimate_affect_rows"],
        "exec_mode": row["exec_mode"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "approvals": [
            {
                "id": a["id"],
                "approval_type": a["approval_type"],
                "comment": a["comment"],
                "approver_id": a["approver_id"],
                "approver": (user_by_id(conn, a["approver_id"]) or {"login": None})["login"],
                "created_at": a["created_at"],
            }
            for a in approvals
        ],
        "execution": None,
    }
    if exec_row:
        progress = exec_row["progress"]
        # observation lag: progress can lag behind result
        out["execution"] = {
            "id": exec_row["id"],
            "result": exec_row["result"],
            "progress": progress,
            "result_artifact": exec_row["result_artifact"],
            "detail": exec_row["detail"],
            "created_at": exec_row["created_at"],
            "updated_at": exec_row["updated_at"],
        }
    return out


def _sync_list_status(conn, order_id: int, true_status: str) -> None:
    if get_flag(conn, "list_status_lag"):
        # keep previous list view if already set; else freeze prior
        row = conn.execute(
            "SELECT status_code, list_status_code FROM change_orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        lag_value = row["list_status_code"] or row["status_code"]
        conn.execute(
            "UPDATE change_orders SET status_code = ?, status_desc = ?, "
            "list_status_code = ?, updated_at = ? WHERE order_id = ?",
            (true_status, STATUS_DESC.get(true_status, true_status), lag_value, now(), order_id),
        )
    else:
        conn.execute(
            "UPDATE change_orders SET status_code = ?, status_desc = ?, "
            "list_status_code = NULL, updated_at = ? WHERE order_id = ?",
            (true_status, STATUS_DESC.get(true_status, true_status), now(), order_id),
        )


# ---------- lifecycle ----------

@app.on_event("startup")
def _startup() -> None:
    reset_database()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------- business APIs ----------

@app.get("/api/v1/me")
def me(user: Actor = Depends(current_user)) -> Actor:
    return user


@app.get("/api/v1/target-orders/{record_id}")
def get_target_order(record_id: str, user: Actor = Depends(current_user)) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM target_orders WHERE record_id = ?", (record_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "target order not found")
        return dict(row)


@app.post("/api/v1/change-orders", status_code=201)
def create_change_order(
    body: CreateChangeOrderRequest, user: Actor = Depends(current_user)
) -> dict[str, Any]:
    if user.role not in {"developer", "free_ops_developer"}:
        raise HTTPException(403, "only developers can create change orders")
    with db() as conn:
        target = conn.execute(
            "SELECT * FROM target_orders WHERE record_id = ?",
            (body.target_record_id,),
        ).fetchone()
        if not target:
            raise HTTPException(400, f"unknown target_record_id: {body.target_record_id}")
        wf = _next_workflow_id(conn)
        ts = now()
        cur = conn.execute(
            """
            INSERT INTO change_orders (
              workflow_instance_id, comment, classify, target_db, target_table,
              target_record_id, exec_sql, desired_status, estimate_affect_rows,
              exec_mode, committer_id, status_code, status_desc, list_status_code,
              created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'toaudit', ?, NULL, ?, ?)
            """,
            (
                wf,
                body.comment,
                body.classify,
                body.target_db,
                body.target_table,
                body.target_record_id,
                body.exec_sql,
                body.desired_status,
                body.estimate_affect_rows,
                body.exec_mode,
                user.id,
                STATUS_DESC["toaudit"],
                ts,
                ts,
            ),
        )
        order_id = int(cur.lastrowid)
        # pending todo for approvers
        for approver in conn.execute(
            "SELECT id FROM users WHERE role = 'approver'"
        ).fetchall():
            conn.execute(
                "INSERT INTO todo_inbox (user_id, order_id, kind, active) VALUES (?, ?, 'approve', 1)",
                (approver["id"], order_id),
            )
        row = conn.execute(
            "SELECT * FROM change_orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        return _order_dict(conn, row)


@app.get("/api/v1/change-orders")
def list_change_orders(
    view: Literal["AS_COMMITTER", "AS_HANDLER", "AS_ADMIN", "AS_Related"] = Query("AS_ADMIN"),
    user: Actor = Depends(current_user),
) -> dict[str, Any]:
    """Mirrors DMS ListOrders OrderResultType semantics (simplified)."""
    with db() as conn:
        if view == "AS_COMMITTER":
            rows = conn.execute(
                "SELECT * FROM change_orders WHERE committer_id = ? ORDER BY order_id DESC",
                (user.id,),
            ).fetchall()
        elif view == "AS_HANDLER":
            rows = conn.execute(
                """
                SELECT c.* FROM change_orders c
                JOIN todo_inbox t ON t.order_id = c.order_id
                WHERE t.user_id = ? AND t.active = 1
                ORDER BY c.order_id DESC
                """,
                (user.id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM change_orders ORDER BY order_id DESC"
            ).fetchall()
        return {
            "total_count": len(rows),
            "orders": [_order_dict(conn, r, for_list=True) for r in rows],
        }


@app.get("/api/v1/change-orders/{order_id}")
def get_change_order(order_id: int, user: Actor = Depends(current_user)) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM change_orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "order not found")
        return _order_dict(conn, row, for_list=False)


@app.post("/api/v1/change-orders/{order_id}/resubmit")
def resubmit_change_order(
    order_id: int, body: ResubmitRequest, user: Actor = Depends(current_user)
) -> dict[str, Any]:
    """P1: after REJECT, committer edits same order and resubmits to audit."""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM change_orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "order not found")
        if row["committer_id"] != user.id:
            raise HTTPException(403, "only committer can resubmit")
        if row["status_code"] != "reject":
            raise HTTPException(409, "only rejected orders can be resubmitted")
        comment = body.comment or row["comment"]
        exec_sql = body.exec_sql or row["exec_sql"]
        desired = body.desired_status or row["desired_status"]
        classify = body.classify or row["classify"]
        conn.execute(
            """
            UPDATE change_orders SET comment=?, exec_sql=?, desired_status=?, classify=?,
              status_code='toaudit', status_desc=?, list_status_code=NULL, updated_at=?
            WHERE order_id=?
            """,
            (comment, exec_sql, desired, classify, STATUS_DESC["toaudit"], now(), order_id),
        )
        for approver in conn.execute(
            "SELECT id FROM users WHERE role = 'approver'"
        ).fetchall():
            conn.execute(
                "INSERT INTO todo_inbox (user_id, order_id, kind, active) VALUES (?, ?, 'approve', 1)",
                (approver["id"], order_id),
            )
        row = conn.execute(
            "SELECT * FROM change_orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        return _order_dict(conn, row)


@app.post("/api/v1/change-orders/{order_id}/approvals")
def approve_order(
    order_id: int, body: ApproveRequest, user: Actor = Depends(current_user)
) -> dict[str, Any]:
    with db() as conn:
        if not get_flag(conn, "approval_path_enabled"):
            raise HTTPException(
                403,
                "approval path disabled by feature flag (path sealed)",
            )
        if user.role != "approver":
            raise HTTPException(403, "only approvers can approve")
        row = conn.execute(
            "SELECT * FROM change_orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "order not found")
        if row["status_code"] != "toaudit":
            raise HTTPException(409, f"order status is {row['status_code']}, expected toaudit")
        conn.execute(
            """
            INSERT INTO approvals (order_id, workflow_instance_id, approval_type, comment, approver_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                row["workflow_instance_id"],
                body.approval_type,
                body.comment,
                user.id,
                now(),
            ),
        )
        if body.approval_type == "AGREE":
            next_status = "approved"
        elif body.approval_type == "REJECT":
            next_status = "reject"
        else:
            next_status = "cancel"
        _sync_list_status(conn, order_id, next_status)
        # clear active approve todos (or lag)
        if get_flag(conn, "todo_lag") and body.approval_type == "AGREE":
            pass  # leave stale todos
        else:
            conn.execute(
                "UPDATE todo_inbox SET active = 0 WHERE order_id = ? AND kind = 'approve'",
                (order_id,),
            )
        # AUTO exec mode
        row = conn.execute(
            "SELECT * FROM change_orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        if next_status == "approved" and row["exec_mode"] == "AUTO":
            _do_execute(conn, row, executor_id=row["committer_id"])
            row = conn.execute(
                "SELECT * FROM change_orders WHERE order_id = ?", (order_id,)
            ).fetchone()
        elif next_status == "approved" and row["exec_mode"] == "LAST_AUDITOR":
            _do_execute(conn, row, executor_id=user.id)
            row = conn.execute(
                "SELECT * FROM change_orders WHERE order_id = ?", (order_id,)
            ).fetchone()
        return _order_dict(conn, row)


def _do_execute(conn, row, executor_id: int) -> None:
    order_id = row["order_id"]
    if row["status_code"] != "approved":
        raise HTTPException(409, f"cannot execute from status {row['status_code']}")
    ts = now()
    # apply business side effect on metadata table
    conn.execute(
        "UPDATE target_orders SET status = ? WHERE record_id = ?",
        (row["desired_status"], row["target_record_id"]),
    )
    artifact = f"exec-{order_id}-ORD-1001.txt"
    detail = (
        f"updated {row['target_table']}.{row['target_record_id']} "
        f"-> {row['desired_status']}"
    )
    progress = "processing" if get_flag(conn, "list_status_lag") else "success"
    conn.execute(
        """
        INSERT INTO executions (order_id, executor_id, result, progress, result_artifact, detail, created_at, updated_at)
        VALUES (?, ?, 'success', ?, ?, ?, ?, ?)
        ON CONFLICT(order_id) DO UPDATE SET
          result='success', progress=excluded.progress, result_artifact=excluded.result_artifact,
          detail=excluded.detail, updated_at=excluded.updated_at, executor_id=excluded.executor_id
        """,
        (order_id, executor_id, progress, artifact, detail, ts, ts),
    )
    _sync_list_status(conn, order_id, "success")


@app.post("/api/v1/change-orders/{order_id}/execute")
def execute_order(
    order_id: int, body: ExecuteRequest, user: Actor = Depends(current_user)
) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM change_orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "order not found")
        if row["status_code"] != "approved":
            raise HTTPException(409, f"order status is {row['status_code']}, expected approved")
        if row["exec_mode"] == "COMMITOR" and user.id != row["committer_id"]:
            raise HTTPException(403, "COMMITOR mode: only committer may execute")
        if row["exec_mode"] == "LAST_AUDITOR" and user.role != "approver":
            raise HTTPException(403, "LAST_AUDITOR mode: only last auditor may execute")
        _do_execute(conn, row, executor_id=user.id)
        row = conn.execute(
            "SELECT * FROM change_orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        return _order_dict(conn, row)


@app.post("/api/v1/free-ops/change-orders", status_code=201)
def free_ops_create(
    body: FreeOpsCreateRequest, user: Actor = Depends(current_user)
) -> dict[str, Any]:
    """P2: free-operation control mode — skip approval queue."""
    with db() as conn:
        if not get_flag(conn, "free_ops_enabled"):
            raise HTTPException(403, "free-ops path disabled by feature flag (path sealed)")
        if user.role != "free_ops_developer":
            raise HTTPException(403, "user lacks free-ops duty")
        target = conn.execute(
            "SELECT * FROM target_orders WHERE record_id = ?",
            (body.target_record_id,),
        ).fetchone()
        if not target:
            raise HTTPException(400, f"unknown target_record_id: {body.target_record_id}")
        wf = _next_workflow_id(conn)
        ts = now()
        cur = conn.execute(
            """
            INSERT INTO change_orders (
              workflow_instance_id, comment, classify, target_db, target_table,
              target_record_id, exec_sql, desired_status, estimate_affect_rows,
              exec_mode, committer_id, status_code, status_desc, list_status_code,
              created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'AUTO', ?, 'approved', ?, NULL, ?, ?)
            """,
            (
                wf,
                body.comment,
                body.classify + " [FREE_OPS]",
                body.target_db,
                body.target_table,
                body.target_record_id,
                body.exec_sql,
                body.desired_status,
                body.estimate_affect_rows,
                user.id,
                STATUS_DESC["approved"],
                ts,
                ts,
            ),
        )
        order_id = int(cur.lastrowid)
        row = conn.execute(
            "SELECT * FROM change_orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        _do_execute(conn, row, executor_id=user.id)
        row = conn.execute(
            "SELECT * FROM change_orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        return _order_dict(conn, row)


@app.get("/api/v1/todos")
def list_todos(user: Actor = Depends(current_user)) -> dict[str, Any]:
    with db() as conn:
        if get_flag(conn, "todo_lag"):
            rows = conn.execute(
                """
                SELECT t.*, c.status_code, c.comment FROM todo_inbox t
                JOIN change_orders c ON c.order_id = t.order_id
                WHERE t.user_id = ?
                ORDER BY t.id DESC
                """,
                (user.id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT t.*, c.status_code, c.comment FROM todo_inbox t
                JOIN change_orders c ON c.order_id = t.order_id
                WHERE t.user_id = ? AND t.active = 1
                ORDER BY t.id DESC
                """,
                (user.id,),
            ).fetchall()
        return {
            "items": [
                {
                    "id": r["id"],
                    "order_id": r["order_id"],
                    "kind": r["kind"],
                    "active": bool(r["active"]),
                    "order_status_code": r["status_code"],
                    "comment": r["comment"],
                }
                for r in rows
            ]
        }


# ---------- admin ----------

@app.post("/api/v1/admin/reset")
def admin_reset(_: None = Depends(optional_admin)) -> dict[str, str]:
    reset_database()
    return {"status": "reset"}


@app.get("/api/v1/admin/feature-flags")
def get_flags(_: None = Depends(optional_admin)) -> dict[str, bool]:
    with db() as conn:
        rows = conn.execute("SELECT key, value FROM feature_flags").fetchall()
        return {
            r["key"]: r["value"].lower() in {"1", "true", "yes", "on"} for r in rows
        }


@app.patch("/api/v1/admin/feature-flags")
def patch_flags(
    body: FeatureFlagsPatch, _: None = Depends(optional_admin)
) -> dict[str, bool]:
    with db() as conn:
        data = body.model_dump(exclude_none=True)
        for key, value in data.items():
            set_flag(conn, key, bool(value))
        rows = conn.execute("SELECT key, value FROM feature_flags").fetchall()
        return {
            r["key"]: r["value"].lower() in {"1", "true", "yes", "on"} for r in rows
        }
