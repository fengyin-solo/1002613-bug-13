"""廊桥对接业务规则：版本号乐观锁、幂等保存、状态流转、字段校验与筛选口径都收在这里。

交接班对账暴露的问题统一在这里兜底：

- 每座廊桥的对接记录带 ``version`` 版本号，保存时先比对版本：两边已经打架时
  以最早落库的那一版为准，版本落后的一方只收到冲突提示，自己填的内容原样保留。
- 保存/动作请求可带幂等令牌（``request_token``），同一座廊桥重复提交只认第一次
  落库的那份，后续重复请求直接回放首次结果。
- 对接高度按「适用机型」的舱门离地高度范围校验，超范围不允许保存并写明原因。
- 重复靠接登记按预靠时间去重；已登记检修的廊桥不参与任何靠接变更。
"""
from __future__ import annotations

import re
import threading
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "bridge"
REQUIRED_FIELDS = ["廊桥编号", "对应机位", "适用机型"]
# 保存接口允许改动的字段；撤桥时间只能由「撤离廊桥」动作写入，不接受手工改。
EDITABLE_FIELDS = ["对接高度", "预靠时间", "操作人员"]
STATUS_ORDER = ["待靠接", "已靠接", "待撤离", "检修中"]
ACTION_RULES = {"靠接廊桥": "已靠接", "撤离廊桥": "待靠接", "登记检修": "检修中"}
NEGATIVE_ACTIONS = []

# 各机型族客舱门离地高度范围（厘米），取各机型手册的包络值。
NARROW_BODY_RANGE = (260.0, 300.0)  # B737、A320 系列等窄体机
WIDE_BODY_RANGE = (440.0, 500.0)    # B777/B787/A330/A350/A380 等宽体机
NARROW_BODY_CODES = (
    "B737", "波音737", "B707", "B717", "B727", "B757",
    "A318", "A319", "A320", "A321", "A220", "C919", "ARJ21",
    "E170", "E175", "E190", "E195", "ERJ", "CRJ", "MD8", "MD9",
    "窄体", "窄体机", "NARROW",
)
WIDE_BODY_CODES = (
    "B747", "波音747", "B767", "B777", "B787",
    "A300", "A310", "A330", "A340", "A350", "A380",
    "宽体", "宽体机", "WIDE",
)
# 适用机型无法识别时的兜底范围：仅拦明显非法值（负数、超高值），不冤枉陌生机型。
FALLBACK_HEIGHT_RANGE = (200.0, 600.0)
MODEL_SPLIT_RE = re.compile(r"[/、,，;；\s]+")
HEIGHT_RE = re.compile(r"\d+(?:\.\d+)?")


def _text(value: Any) -> str:
    """把入参统一成去空白的字符串；None/空串都按空值处理。"""
    if value is None:
        return ""
    return str(value).strip()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_height(value: Any) -> tuple[float | None, str]:
    """解析对接高度，返回（厘米数值, 原始文本）。

    页面允许填「280」（厘米）或「2.8」（米）；不超过 6 的小数按米换算成厘米。
    """
    raw = _text(value)
    if not raw:
        return None, raw
    match = HEIGHT_RE.search(raw.replace(",", ""))
    if match is None:
        return None, raw
    height = float(match.group())
    if height <= 6:  # 2.8 米这种写法
        height *= 100
    return height, raw


def _height_range_for_models(model_text: str) -> tuple[float, float]:
    """按适用机型求允许高度的并集：兼容「B737/A320」「宽体机」等写法。"""
    low: float | None = None
    high: float | None = None
    for token in MODEL_SPLIT_RE.split(model_text.upper().replace(" ", "")):
        token_range: tuple[float, float] | None = None
        if any(code in token for code in NARROW_BODY_CODES):
            token_range = NARROW_BODY_RANGE
        elif any(code in token for code in WIDE_BODY_CODES):
            token_range = WIDE_BODY_RANGE
        if token_range is not None:
            low = token_range[0] if low is None else min(low, token_range[0])
            high = token_range[1] if high is None else max(high, token_range[1])
    if low is None or high is None:
        return FALLBACK_HEIGHT_RANGE
    return low, high


class BridgeService:
    def __init__(self) -> None:
        # 读-比对-写必须在同一把锁里完成，避免并发请求交叉落库。
        self._lock = threading.RLock()
        # 幂等令牌缓存：f"{入口}:{廊桥id}:{token}" -> 首次落库后回放给调用方的记录。
        self._idempotent: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ 读取
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            rows = [self._normalize(dict(row)) for row in store.rows(MODULE)]
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("廊桥编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        with self._lock:
            entry = store.find(MODULE, entry_id)
            return self._normalize(dict(entry)) if entry is not None else None

    def stats(self) -> dict[str, Any]:
        """总览台数：直接逐条数桥位明细，保证卡片和明细永远是同一份口径。"""
        by_status = {status: 0 for status in STATUS_ORDER}
        with self._lock:
            rows = store.rows(MODULE)
            for row in rows:
                status = row.get("status")
                if status in by_status:
                    by_status[status] += 1
        return {
            "total": len(rows),
            "by_status": by_status,
            "docked": by_status["已靠接"] + by_status["待撤离"],
        }

    # ------------------------------------------------------------------ 登记
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not _text(values.get(field))]
        if missing:
            return None, missing
        with self._lock:
            rows = store.rows(MODULE)
            entry: dict[str, Any] = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: _text(values.get(field)) for field in REQUIRED_FIELDS})
            entry["status"] = STATUS_ORDER[0]
            entry["廊桥状态"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            entry["version"] = 1
            entry["对接高度"] = _text(values.get("对接高度"))
            entry["预靠时间"] = _text(values.get("预靠时间")).replace("T", " ")
            entry["撤桥时间"] = ""
            entry["操作人员"] = _text(values.get("操作人员"))
            entry["更新时间"] = ""
            entry["靠接记录"] = []
            rows.append(entry)
            return dict(entry), []

    # ------------------------------------------------------------------ 保存
    def save_entry(
        self,
        entry_id: int,
        values: dict[str, Any],
        request_token: str = "",
    ) -> tuple[dict[str, Any] | None, str, str]:
        """带版本号的保存。

        返回 ``(记录, 说明, 结果码)``，结果码取值：
        saved / duplicate / conflict / invalid_version / invalid_height / locked / not_found。
        """
        token = _text(request_token)
        with self._lock:
            duplicate_key = f"save:{entry_id}:{token}"
            if token and duplicate_key in self._idempotent:
                cached = self._idempotent[duplicate_key]
                return dict(cached), "重复的保存请求已忽略，以第一次落库的版本为准", "duplicate"

            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"廊桥 {entry_id} 不存在或已归档", "not_found"
            self._normalize(entry)

            # 已登记检修的廊桥冻结，任何靠接相关变更都不接收。
            if entry["status"] == "检修中":
                return dict(entry), "廊桥已登记检修，不参与靠接变更", "locked"

            # 先比对版本：版本落后说明已经有人先落库，本次一律不覆盖。
            version_raw = values.get("version")
            try:
                base_version = int(_text(version_raw)) if version_raw is not None and _text(version_raw) else None
            except (TypeError, ValueError):
                base_version = None
            if base_version is None:
                return dict(entry), "缺少版本号，请刷新详情后基于最新版本重新编辑", "invalid_version"
            if base_version != entry["version"]:
                return (
                    dict(entry),
                    f"记录已被他人更新（您基于 v{base_version} 修改，当前为 v{entry['version']}），"
                    "已保留最早落库的内容；您填写的内容未被覆盖，请核对后按新版本重试",
                    "conflict",
                )

            # 对接高度必须落在适用机型的允许范围内，否则拦下并写明原因。
            if "对接高度" in values:
                height, raw = _parse_height(values.get("对接高度"))
                if raw and height is None:
                    return dict(entry), f"对接高度「{raw}」不是有效数值，请按厘米填写", "invalid_height"
                if height is not None:
                    low, high = _height_range_for_models(entry["适用机型"])
                    if not low <= height <= high:
                        return (
                            dict(entry),
                            f"对接高度 {height:.0f} 厘米超出适用机型「{entry['适用机型']}」"
                            f"允许范围 {low:.0f}–{high:.0f} 厘米，不允许保存",
                            "invalid_height",
                        )

            changed = False
            for field in EDITABLE_FIELDS:
                if field in values:
                    new_value = _text(values.get(field)).replace("T", " ") if field == "预靠时间" else _text(values.get(field))
                    if new_value != entry.get(field):
                        entry[field] = new_value
                        changed = True
            entry["version"] += 1
            entry["更新时间"] = _now()
            snapshot = dict(entry)
            if token:
                self._idempotent[duplicate_key] = snapshot
            note = "内容无变化，版本已刷新" if not changed else "廊桥对接记录已保存"
            return snapshot, f"{note}（当前版本 v{entry['version']}）", "saved"

    # ------------------------------------------------------------------ 动作
    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str, str]:
        values = values or {}
        token = _text(values.get("request_token"))
        with self._lock:
            duplicate_key = f"action:{entry_id}:{token}"
            if token and duplicate_key in self._idempotent:
                cached = self._idempotent[duplicate_key]
                return dict(cached), "重复的动作请求已忽略，以第一次落库的结果为准", "duplicate"

            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"廊桥 {entry_id} 不存在或已归档", "not_found"
            self._normalize(entry)
            if action not in ACTION_RULES:
                return dict(entry), f"动作「{action}」不属于廊桥对接可执行范围", "invalid_action"

            if entry["status"] == "检修中":
                if action == "登记检修":
                    return dict(entry), "廊桥已处于检修中，无需重复登记", "duplicate"
                return dict(entry), "廊桥已登记检修，不参与靠接变更", "locked"

            if action == "靠接廊桥":
                result = self._dock(entry, values)
            elif action == "撤离廊桥":
                result = self._withdraw(entry, values)
            else:
                result = self._register_maintenance(entry, values)

            record, message, code = result
            if token and code in {"saved", "duplicate_dock"}:
                self._idempotent[duplicate_key] = record
            return result

    # ------------------------------------------------------------- 动作细则
    def _dock(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
        plan_time = _text(values.get("预靠时间")) or entry["预靠时间"]
        plan_time = plan_time.replace("T", " ")
        operator = _text(values.get("操作人员")) or entry["操作人员"]

        # 重复靠接登记按预靠时间去重：同一座廊桥同一预靠时间只认第一次。
        if plan_time:
            for item in entry["靠接记录"]:
                if item.get("动作") == "靠接廊桥" and item.get("预靠时间") == plan_time:
                    return (
                        dict(entry),
                        f"预靠时间 {plan_time} 的靠接已登记过，按时间去重，未重复登记",
                        "duplicate_dock",
                    )

        if not plan_time:
            return dict(entry), "缺少预靠时间，无法登记靠接", "invalid_action"

        entry["status"] = "已靠接"
        entry["廊桥状态"] = "已靠接"
        entry["预靠时间"] = plan_time
        entry["操作人员"] = operator
        entry["撤桥时间"] = ""
        entry["pending"] = True
        entry["abnormal"] = False
        entry["version"] += 1
        entry["更新时间"] = _now()
        entry["靠接记录"].append({
            "序号": len(entry["靠接记录"]) + 1,
            "动作": "靠接廊桥",
            "预靠时间": plan_time,
            "撤桥时间": "",
            "操作人员": operator,
            "对接高度": entry["对接高度"],
            "登记时间": _now(),
        })
        return dict(entry), f"廊桥已靠接，预靠时间 {plan_time}（版本 v{entry['version']}）", "saved"

    def _withdraw(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
        if entry["status"] not in {"已靠接", "待撤离"}:
            return dict(entry), "廊桥当前不是已靠接状态，无需撤离", "invalid_action"
        leave_time = _text(values.get("撤桥时间")) or _now()
        leave_time = leave_time.replace("T", " ")
        operator = _text(values.get("操作人员")) or entry["操作人员"]
        entry["status"] = "待靠接"
        entry["廊桥状态"] = "待靠接"
        entry["撤桥时间"] = leave_time
        entry["操作人员"] = operator
        entry["pending"] = True
        entry["abnormal"] = False
        entry["version"] += 1
        entry["更新时间"] = _now()
        entry["靠接记录"].append({
            "序号": len(entry["靠接记录"]) + 1,
            "动作": "撤离廊桥",
            "预靠时间": entry["预靠时间"],
            "撤桥时间": leave_time,
            "操作人员": operator,
            "对接高度": entry["对接高度"],
            "登记时间": _now(),
        })
        return dict(entry), f"廊桥已撤离，撤桥时间 {leave_time}（版本 v{entry['version']}）", "saved"

    def _register_maintenance(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
        entry["status"] = "检修中"
        entry["廊桥状态"] = "检修中"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["version"] += 1
        entry["更新时间"] = _now()
        entry["靠接记录"].append({
            "序号": len(entry["靠接记录"]) + 1,
            "动作": "登记检修",
            "预靠时间": entry["预靠时间"],
            "撤桥时间": entry["撤桥时间"],
            "操作人员": _text(values.get("操作人员")) or entry["操作人员"],
            "对接高度": entry["对接高度"],
            "登记时间": _now(),
        })
        return dict(entry), f"廊桥已登记检修，冻结靠接变更（版本 v{entry['version']}）", "saved"

    # ------------------------------------------------------------- 记录规整
    def _normalize(self, entry: dict[str, Any]) -> dict[str, Any]:
        """给老数据补齐版本号、时间与靠接记录字段，保证列表与详情读到同一份结构。"""
        entry.setdefault("version", 1)
        entry.setdefault("对接高度", "")
        entry.setdefault("预靠时间", "")
        entry.setdefault("撤桥时间", "")
        entry.setdefault("操作人员", "")
        entry.setdefault("更新时间", "")
        entry.setdefault("靠接记录", [])
        # 业务展示字段以内部状态为准，避免「撤了桥还写着已靠接」这类脱节。
        if entry.get("status") in STATUS_ORDER:
            entry["廊桥状态"] = entry["status"]
        return entry
