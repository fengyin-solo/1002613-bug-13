"""廊桥对接业务规则：版本号乐观锁、状态流转、对接高度校验与靠接去重都收在这里。

交接班复核暴露的问题对应的处理原则：

- 每条廊桥记录带 ``version`` 版本号，任何落库改动（字段保存、靠接、撤离、检修）
  都要带上客户端读到的版本；版本落后则判定为并发冲突，以最早落库的那一版为准，
  后提交的一方拿到冲突提示，自己填写的内容原样退回。
- 保存请求可带幂等令牌 ``request_id``：同一座廊桥重复提交同一份请求，只认第一次
  落库的那份，后续重复请求直接回放首次结果。
- 对接高度必须落在适用机型的高度区间内，否则不允许保存并写明具体原因。
- 靠接登记按（预靠时间）去重，同一时间重复登记靠接视为重复操作。
- 已登记检修的廊桥冻结，不参与任何靠接/撤离/字段变更。
"""
from __future__ import annotations

import re
import threading
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "bridge"
REQUIRED_FIELDS = ["廊桥编号", "对应机位", "适用机型"]
EDITABLE_FIELDS = ["对接高度", "预靠时间", "操作人员"]
STATUS_ORDER = ["待靠接", "已靠接", "待撤离", "检修中"]
ACTION_RULES = {"靠接廊桥": "已靠接", "撤离廊桥": "待靠接", "登记检修": "检修中"}
ACTION_LABELS = {"靠接廊桥": "靠接", "撤离廊桥": "撤离", "登记检修": "登记检修"}
MAINTENANCE_STATUS = "检修中"

# 各机型机身前舱门的对接高度区间（米）。适用机型里出现机型名即按该机型区间校验；
# 一桥适用多机型时，高度落在任一机型区间内即可。
AIRCRAFT_HEIGHT_RANGES: dict[str, tuple[float, float]] = {
    "A320": (2.7, 4.2),
    "A319": (2.7, 4.2),
    "A321": (2.7, 4.2),
    "B737": (2.6, 4.2),
    "A330": (3.8, 5.6),
    "A350": (4.0, 5.8),
    "B777": (4.0, 5.8),
    "B787": (3.9, 5.7),
}

_lock = threading.RLock()


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_time(value: Any) -> str:
    """把日期选择器给的 ``2026-09-30T08:00`` 统一成 ``2026-09-30 08:00``。"""
    text = str(value or "").strip()
    return re.sub(r"T", " ", text)[:19]


def parse_height(value: Any) -> float | None:
    """解析对接高度，兼容 ``4.2``、``4.2m``、``4.2 米`` 三种写法。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        raise ValueError("对接高度必须是数字，单位为米")
    height = float(match.group())
    if height <= 0:
        raise ValueError("对接高度必须大于 0 米")
    return height


def matched_ranges(aircraft_text: Any) -> list[tuple[str, tuple[float, float]]]:
    text = str(aircraft_text or "").upper()
    return [
        (name, bounds)
        for name, bounds in AIRCRAFT_HEIGHT_RANGES.items()
        if name in text
    ]


def validate_height(aircraft_text: Any, height_value: Any) -> str | None:
    """校验对接高度是否在适用机型的区间内；不通过时返回可读原因。"""
    try:
        height = parse_height(height_value)
    except ValueError as exc:
        return str(exc)
    if height is None:
        return None  # 高度允许暂缺，后续动作/保存时再补
    ranges = matched_ranges(aircraft_text)
    if not ranges:
        names = "、".join(AIRCRAFT_HEIGHT_RANGES)
        return (
            f"适用机型「{aircraft_text}」没有配置对接高度区间，"
            f"暂无法校验，请在适用机型中写明具体机型（当前支持：{names}）"
        )
    if any(low <= height <= high for _, (low, high) in ranges):
        return None
    scope = "；".join(f"{name}: {low}~{high} 米" for name, (low, high) in ranges)
    return (
        f"对接高度 {height:g} 米超出适用机型范围（{scope}），请按机型舱门高度调整后再保存"
    )


def make_history(action: str, operator: Any = None) -> dict[str, str]:
    return {
        "time": _now_text(),
        "action": action,
        "operator": str(operator or "").strip(),
    }


class BridgeService:
    def __init__(self) -> None:
        # 幂等结果缓存：key = (廊桥id, 客户端令牌)，value 为首次落库结果
        self._idempotent: dict[tuple[int, str], tuple[dict[str, Any], str, str | None]] = {}

    # ---------- 查询：列表与详情读同一份底层记录，时间口径天然一致 ----------

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("廊桥编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def summary(self) -> dict[str, int]:
        """桥位明细的汇总口径，总览页与明细页共用同一份统计来源。"""
        rows = store.rows(MODULE)
        counts = {status: 0 for status in STATUS_ORDER}
        for row in rows:
            status = str(row.get("status") or "")
            if status in counts:
                counts[status] += 1
        counts["靠接台数"] = counts["已靠接"] + counts["待撤离"]
        return counts

    # ---------- 登记 ----------

    def create_entry(
        self, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, list[str], str | None]:
        missing = [
            field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing, None
        height_error = validate_height(values.get("适用机型"), values.get("对接高度"))
        if height_error:
            return None, [height_error], height_error
        with _lock:
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry["对接高度"] = values.get("对接高度")
            entry["预靠时间"] = normalize_time(values.get("预靠时间")) or None
            entry["撤桥时间"] = None
            entry["操作人员"] = str(values.get("操作人员") or "").strip()
            entry["status"] = STATUS_ORDER[0]
            entry["廊桥状态"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            entry["version"] = 1
            entry["history"] = [make_history("登记廊桥", entry["操作人员"])]
            rows.append(entry)
        return entry, [], None

    # ---------- 字段保存：版本比对 + 幂等 + 高度/检修拦截 ----------

    def save_entry(
        self,
        entry_id: int,
        values: dict[str, Any],
        version: int | None,
        request_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, str, str | None]:
        """返回 (记录, 提示语, 冲突标记)。冲突时 conflict='version'，记录不做任何改动。"""
        idem_key = (entry_id, request_id) if request_id else None
        with _lock:
            if idem_key and idem_key in self._idempotent:
                entry, message, conflict = self._idempotent[idem_key]
                return entry, f"{message}（重复提交已忽略，以第一次落库为准）", conflict

            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"廊桥 {entry_id} 不存在或已归档", None
            if version is None:
                return None, "保存请求缺少版本号，请刷新详情后基于最新版本再保存", None

            if entry.get("status") == MAINTENANCE_STATUS:
                return None, "廊桥已登记检修，冻结期间不参与靠接变更，无法保存", "maintenance"
            if int(entry.get("version", 1)) != int(version):
                conflict_values = {
                    field: values.get(field, entry.get(field)) for field in EDITABLE_FIELDS
                }
                return (
                    {"id": entry_id, **conflict_values},
                    (
                        f"记录已被他人更新（当前版本 v{entry.get('version')}，"
                        f"您基于 v{version} 提交），系统以最早落库的版本为准；"
                        "您填写的内容已保留，刷新确认后可在最新版本上重新保存"
                    ),
                    "version",
                )

            height_error = validate_height(entry.get("适用机型"), values.get("对接高度"))
            if height_error:
                return None, height_error, "height"

            before = {field: entry.get(field) for field in EDITABLE_FIELDS}
            if "对接高度" in values:
                height = parse_height(values.get("对接高度"))
                entry["对接高度"] = f"{height:g}" if height is not None else None
            if "预靠时间" in values:
                entry["预靠时间"] = normalize_time(values.get("预靠时间")) or None
            if "操作人员" in values:
                entry["操作人员"] = str(values.get("操作人员") or "").strip()

            entry["version"] = int(entry.get("version", 1)) + 1
            changed = [
                field
                for field in EDITABLE_FIELDS
                if entry.get(field) != before[field]
            ]
            entry.setdefault("history", []).append(
                make_history(
                    "保存修改：" + ("、".join(changed) if changed else "无字段变化"),
                    entry.get("操作人员"),
                )
            )
            result = (entry, f"廊桥信息已保存（v{entry['version']}）", None)
            if idem_key:
                self._idempotent[idem_key] = result
            return result

    # ---------- 动作：靠接/撤离/检修同样走版本比对 ----------

    def run_action(
        self,
        entry_id: int,
        action: str,
        version: int | None = None,
        request_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, str, str | None]:
        idem_key = (entry_id, request_id) if request_id else None
        with _lock:
            if idem_key and idem_key in self._idempotent:
                entry, message, conflict = self._idempotent[idem_key]
                return entry, f"{message}（重复提交已忽略，以第一次落库为准）", conflict

            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"廊桥 {entry_id} 不存在或已归档", None
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于廊桥对接可执行范围", None
            if version is None:
                return None, "操作请求缺少版本号，请刷新列表后基于最新版本再操作", None
            if int(entry.get("version", 1)) != int(version):
                return (
                    entry,
                    (
                        f"记录已被他人更新（当前版本 v{entry.get('version')}，"
                        f"您基于 v{version} 操作），本次动作未执行，请刷新后重试"
                    ),
                    "version",
                )

            status = str(entry.get("status") or "")
            if action == "靠接廊桥":
                if status == MAINTENANCE_STATUS:
                    return None, "廊桥已登记检修，不参与靠接变更", "maintenance"
                if status == "已靠接":
                    return None, "廊桥当前已处于已靠接状态，无需重复登记靠接", "duplicate"
                if status == "待撤离":
                    return None, "廊桥已靠接待撤离，不能重复登记靠接", "duplicate"
                planned = normalize_time(entry.get("预靠时间"))
                if not planned:
                    return None, "预靠时间尚未填写，无法登记靠接", None
                for item in entry.get("history", []):
                    if (
                        item.get("action") == "靠接廊桥"
                        and item.get("plannedTime") == planned
                    ):
                        return (
                            None,
                            f"预靠时间 {planned} 的靠接已经登记过，按时间去重不再重复落库",
                            "duplicate",
                        )
                entry["status"] = "已靠接"
                entry["撤桥时间"] = None
            elif action == "撤离廊桥":
                if status == MAINTENANCE_STATUS:
                    return None, "廊桥已登记检修，不参与撤离变更", "maintenance"
                if status not in ("已靠接", "待撤离"):
                    return None, f"廊桥当前为「{status}」，尚未靠接，不能撤离", "invalid"
                entry["status"] = "待靠接"
                entry["撤桥时间"] = _now_text()
            else:  # 登记检修
                if status == MAINTENANCE_STATUS:
                    return None, "廊桥已处于检修中，无需重复登记", "duplicate"
                entry["status"] = MAINTENANCE_STATUS

            entry["廊桥状态"] = entry["status"]
            entry["pending"] = entry["status"] != MAINTENANCE_STATUS
            entry["abnormal"] = False
            entry["version"] = int(entry.get("version", 1)) + 1
            history_item = make_history(action, entry.get("操作人员"))
            if action == "靠接廊桥":
                history_item["plannedTime"] = normalize_time(entry.get("预靠时间"))
            entry.setdefault("history", []).append(history_item)
            result = (entry, f"廊桥已{ACTION_LABELS[action]}（v{entry['version']}）", None)
            if idem_key:
                self._idempotent[idem_key] = result
            return result
