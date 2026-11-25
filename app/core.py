"""换包核对核心逻辑：有效搭配集合与批复比对。

包内规则：每个票种给出车站清单、介质清单和禁用搭配；
车站与介质可任意组合（笛卡尔积），禁用的除外。
核对时把"在用包 → 候选包"的实际增减与批复清单逐项比对，
只汇总多改/漏改项数并各举一例，不逐一枚举全部搭配。
"""
from __future__ import annotations

# 一条有效搭配 = (票种, 车站, 介质)
Combo = tuple[str, str, str]

DIRECTION_ADD = "add"       # 新增
DIRECTION_REMOVE = "remove"  # 撤销


def effective_combos(content) -> set[Combo]:
    """计算包内全部有效搭配：各票种 车站×介质 的笛卡尔积，扣除禁用搭配。"""
    combos: set[Combo] = set()
    for rule in content.ticket_types:
        forbidden = {(f.station, f.medium) for f in rule.forbidden}
        for station in set(rule.stations):
            for medium in set(rule.media):
                if (station, medium) not in forbidden:
                    combos.add((rule.ticket_type, station, medium))
    return combos


def _one_example(add_pool: set[Combo], remove_pool: set[Combo]) -> dict | None:
    """从差异集合中确定性地挑一个例子（新增优先）。"""
    for direction, pool in ((DIRECTION_ADD, add_pool), (DIRECTION_REMOVE, remove_pool)):
        if pool:
            ticket_type, station, medium = sorted(pool)[0]
            return {
                "direction": direction,
                "ticket_type": ticket_type,
                "station": station,
                "medium": medium,
            }
    return None


def verify_against_approval(
    old: set[Combo],
    new: set[Combo],
    approved_add: set[Combo],
    approved_remove: set[Combo],
) -> dict:
    """比对实际增减与批复清单。

    - 多改 over_changed：实际改了但批复没有（新增、撤销两个方向合并计数）
    - 漏改 under_changed：批复要求改但实际没改
    报告中只给项数和一个例子，不枚举完整差异清单。
    """
    actual_add = new - old
    actual_remove = old - new

    over_add = actual_add - approved_add
    over_remove = actual_remove - approved_remove
    under_add = approved_add - actual_add
    under_remove = approved_remove - actual_remove

    over_count = len(over_add) + len(over_remove)
    under_count = len(under_add) + len(under_remove)

    return {
        "result": "pass" if over_count == 0 and under_count == 0 else "fail",
        "actual_additions": len(actual_add),
        "actual_removals": len(actual_remove),
        "approved_additions": len(approved_add),
        "approved_removals": len(approved_remove),
        "over_changed": {
            "count": over_count,
            "example": _one_example(over_add, over_remove),
        },
        "under_changed": {
            "count": under_count,
            "example": _one_example(under_add, under_remove),
        },
    }
