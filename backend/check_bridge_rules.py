"""廊桥对接规则的交接班复核场景脚本：不依赖网络，直接打 service 层。"""
from __future__ import annotations

from app.services.bridge import BridgeService


def expect(cond: bool, message: str) -> None:
    if not cond:
        raise AssertionError(message)
    print("  ok -", message)


s = BridgeService()

print("场景1：对接高度超出适用机型范围，拒绝保存并写明原因")
entry, missing, height_error = s.create_entry(
    {"廊桥编号": "B-TEST1", "对应机位": "X1", "适用机型": "A320", "对接高度": "9.9m"}
)
expect(entry is None, "超范围高度创建被拦下")
expect(height_error and "超出适用机型范围" in height_error, f"原因可读：{height_error}")
entry, missing, height_error = s.create_entry(
    {"廊桥编号": "B-TEST1", "对应机位": "X1", "适用机型": "A320", "对接高度": "3.6",
     "预靠时间": "2026-09-30T09:00", "操作人员": "甲"}
)
expect(entry is not None and entry["version"] == 1, "范围内高度登记成功，初始版本 v1")
bridge_id = entry["id"]

print("场景2：两人同读 v1，先保存者落库，后保存者收到冲突且自己内容被保留")
e2, msg2, c2 = s.save_entry(
    bridge_id, {"对接高度": "3.8", "预靠时间": "2026-09-30 09:05", "操作人员": "甲"},
    version=1,
)
expect(c2 is None and e2 is not None and e2["version"] == 2, f"甲先保存成功：{msg2}")
e3, msg3, c3 = s.save_entry(
    bridge_id, {"对接高度": "4.0", "预靠时间": "2026-09-30 09:20", "操作人员": "乙"},
    version=1,
)
expect(c3 == "version", "乙基于 v1 提交被判版本冲突")
expect("最早落库" in msg3, f"冲突提示说明以最早落库为准：{msg3}")
expect(e3 is not None and e3["对接高度"] == "4.0" and e3["操作人员"] == "乙",
       "冲突返回里保留乙填写的内容")
expect(s.get_entry(bridge_id)["对接高度"] == "3.8", "库里仍是甲落库的 3.8，未被乙覆盖")

print("场景3：同一座廊桥重复提交保存请求（相同 request_id），只认第一次落库")
e4a, m4a, c4a = s.save_entry(
    bridge_id, {"对接高度": "4.1", "预靠时间": "2026-09-30 09:30"},
    version=2, request_id="dup-save",
)
expect(c4a is None and e4a["version"] == 3, f"首次提交落库：{m4a}")
e4b, m4b, c4b = s.save_entry(
    bridge_id, {"对接高度": "2.9", "预靠时间": "2026-09-30 10:30"},
    version=2, request_id="dup-save",
)
expect(c4b is None and e4b["version"] == 3, "重复提交回放首次结果，版本不再增长")
expect(s.get_entry(bridge_id)["对接高度"] == "4.1", "重复提交的 2.9 没有覆盖首次的 4.1")

print("场景4：动作也走版本号；撤桥后记录立即回写撤桥时间与状态")
e5, m5, c5 = s.run_action(bridge_id, "靠接廊桥", version=99)
expect(c5 == "version", "落后版本靠接被拒")
e6, m6, c6 = s.run_action(bridge_id, "靠接廊桥", version=3, request_id="act-dock")
expect(c6 is None and e6["status"] == "已靠接" and e6["version"] == 4, f"靠接成功：{m6}")
e7, m7, c7 = s.run_action(bridge_id, "靠接廊桥", version=4, request_id="act-dock-2")
expect(c7 == "duplicate", "已靠接后重复登记靠接被去重")
e8, m8, c8 = s.run_action(bridge_id, "撤离廊桥", version=4, request_id="act-leave")
expect(c8 is None and e8["status"] == "待靠接" and e8["撤桥时间"], f"撤离成功：{m8}")
expect(s.get_entry(bridge_id)["status"] == "待靠接", "撤桥后详情不再显示已靠接")
# 撤桥后仍按同一预靠时间再次登记靠接：历史里已有该时间的靠接，按时间去重
e9, m9, c9 = s.run_action(bridge_id, "靠接廊桥", version=5)
expect(c9 == "duplicate" and "按时间去重" in m9, f"同一预靠时间重复靠接被去重：{m9}")
# 换新预靠时间后允许再次靠接
s.save_entry(bridge_id, {"预靠时间": "2026-09-30 11:00"}, version=5)
e9b, m9b, c9b = s.run_action(bridge_id, "靠接廊桥", version=6)
expect(c9b is None and e9b["status"] == "已靠接", f"新预靠时间允许重新靠接：{m9b}")

print("场景5：检修中的廊桥不参与靠接/撤离/保存变更")
maint_entry, _, _ = s.create_entry(
    {"廊桥编号": "B-MAINT", "对应机位": "X2", "适用机型": "B737", "对接高度": "3.2",
     "预靠时间": "2026-09-30 12:00", "操作人员": "丙"}
)
mid = maint_entry["id"]
s.run_action(mid, "登记检修", version=1)
e10, m10, c10 = s.run_action(mid, "靠接廊桥", version=2)
expect(c10 == "maintenance" and "检修" in m10, "检修桥拒绝靠接")
e11, m11, c11 = s.run_action(mid, "撤离廊桥", version=2)
expect(c11 == "maintenance", "检修桥拒绝撤离")
e12, m12, c12 = s.save_entry(mid, {"对接高度": "3.5"}, version=2)
expect(c12 == "maintenance", "检修桥拒绝字段保存")

print("场景6：汇总口径与明细逐条一致")
summary = s.summary()
rows, _ = s.list_entries(page=1, size=10000)
docked = sum(1 for r in rows if r["status"] in ("已靠接", "待撤离"))
expect(summary["靠接台数"] == docked,
       f"靠接台数 {summary['靠接台数']} == 明细逐条计数 {docked}")
expect(all(summary[k] == sum(1 for r in rows if r["status"] == k)
           for k in ("待靠接", "已靠接", "待撤离", "检修中")),
       "各状态计数与明细一致")

print("场景7：列表与详情读同一份记录")
listed = next(r for r in rows if r["id"] == bridge_id)
detail = s.get_entry(bridge_id)
expect(listed is detail, "列表元素与详情是同一条底层记录（时间/版本不会分叉）")

print("\n全部场景通过 ✔")
