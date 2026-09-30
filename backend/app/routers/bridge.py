"""廊桥对接接口：维护廊桥，覆盖靠接廊桥、撤离廊桥、登记检修等动作。

所有写操作（字段保存、靠接、撤离、检修）都要求带 ``version``：后端比对廊桥当前版本，
两边打架时以最早落库的一版为准，版本落后的一方收到 ok=False 的冲突提示。
``request_id`` 为可选的客户端幂等令牌，同一座廊桥重复提交只认第一次落库的那份。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.bridge import BridgeService

router = APIRouter(prefix="/api/bridge", tags=["廊桥对接"])

service = BridgeService()

LIST_FIELDS = ["廊桥编号", "对应机位", "适用机型", "对接高度", "预靠时间", "撤桥时间", "操作人员", "廊桥状态"]
STATUSES = ["待靠接", "已靠接", "待撤离", "检修中"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按廊桥编号检索"),
    status: str | None = Query(default=None, description="待靠接、已靠接、待撤离、检修中"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按廊桥编号与状态过滤廊桥对接列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/summary")
def bridge_summary() -> dict[str, Any]:
    """桥位明细的实时汇总，供总览页核对：靠接台数必须与明细逐条对得上。"""
    return {"module": "bridge", **service.summary()}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出廊桥对接清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "bridge", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条廊桥明细；不存在时给出可读的错误说明。

    明细与列表读的是 store 里同一条记录，版本号、预靠/撤桥时间口径一致。
    """
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"廊桥 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条廊桥，缺字段或对接高度超出机型范围时说明原因而不是静默丢弃。"""
    entry, errors, height_error = service.create_entry(payload.values)
    if height_error:
        return ActionResult(ok=False, message=height_error)
    if errors:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(errors)}")
    return ActionResult(ok=True, message="廊桥已登记", entry=entry)


@router.put("/{entry_id}", response_model=ActionResult)
def save_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """保存廊桥字段（对接高度、预靠时间、操作人员）。

    - 必须携带 version；落后于当前版本返回冲突提示，已落库的旧版数据不被覆盖；
    - 对接高度超出适用机型范围直接拒绝并写明原因；
    - 检修中的廊桥不允许变更；
    - 相同 request_id 的重复提交只回放第一次落库结果。
    """
    version = _extract_version(payload)
    request_id = str(payload.values.get("request_id") or "").strip() or None
    entry, message, conflict = service.save_entry(
        entry_id, payload.values, version=version, request_id=request_id
    )
    return ActionResult(ok=conflict is None and entry is not None, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条廊桥执行靠接廊桥、撤离廊桥、登记检修。

    必须携带 version 做并发比对；检修中的廊桥不参与靠接/撤离变更；
    同一预靠时间重复登记靠接按时间去重；相同 request_id 只认第一次落库。
    """
    action = str(payload.values.get("action") or "").strip()
    version = _extract_version(payload)
    request_id = str(payload.values.get("request_id") or "").strip() or None
    entry, message, conflict = service.run_action(
        entry_id, action, version=version, request_id=request_id
    )
    return ActionResult(ok=conflict is None and entry is not None, message=message, entry=entry)


def _extract_version(payload: EntryPayload) -> int | None:
    raw = payload.values.get("version")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None
