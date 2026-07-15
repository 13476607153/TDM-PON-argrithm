"""统一的 1 us 离散时间与资源占用模型。"""

import math

from .config import GUARD_SLOTS, MIN_PAYLOAD_SLOTS, SLOT_SIZE_US


def transmission_time_us(size_bytes, link_rate_bps):
    """把字节数换算为物理链路发送时长（us）。"""
    return float(size_bytes) * 8.0 / float(link_rate_bps) * 1e6


def resource_slots(size_bytes, link_rate_bps, slot_size_us=SLOT_SIZE_US,
                   min_payload_slots=MIN_PAYLOAD_SLOTS, guard_slots=GUARD_SLOTS):
    """把连续物理发送时长映射为离散 payload/guard 资源。

    减去 1e-12 是为了抵消浮点计算在整数边界上产生的极小正误差，避免
    本应恰好占 N 个 slot 的包被错误地向上取整为 N+1 个 slot。
    """
    raw = transmission_time_us(size_bytes, link_rate_bps)
    payload = max(int(min_payload_slots), int(math.ceil(raw / slot_size_us - 1e-12)))
    guard = int(guard_slots)
    return {"raw_tx_time_us": raw, "payload_slots": payload,
            "guard_slots": guard, "occupied_slots": payload + guard}


def build_time_window(arrival_time, delay_limit, payload_slots, tse, tprop, tproc,
                      slot_size_us=SLOT_SIZE_US):
    """deadline 约束 payload 完成；保持旧模型的时延预算公式。"""
    # release 表示考虑同步误差后允许发送的最早时刻。
    release_time = float(arrival_time) + float(tse)
    # deadline 只约束 payload 完成，不包含其后的 guard；但后续资源可行性
    # 检查仍会要求 payload+guard 整体位于同一 DBA 周期内。
    deadline = (float(arrival_time) + float(delay_limit) - 2 * float(tse)
                - 2 * float(tproc) - float(tprop))
    release = int(math.ceil(release_time / slot_size_us - 1e-12))
    deadline_slot = int(math.floor(deadline / slot_size_us + 1e-12))
    return {"release_slot": release, "service_deadline_slot": deadline_slot,
            "latest_start_slot": deadline_slot - int(payload_slots)}


def eligible_dba_periods(release_slot, latest_start_slot, occupied_slots,
                         supercycle_slots, dba_period_slots):
    """返回同时满足业务时间窗和 DBA 边界的候选周期编号列表。"""
    periods = []
    for d in range(int(supercycle_slots) // int(dba_period_slots)):
        start, end = d * dba_period_slots, (d + 1) * dba_period_slots
        if max(release_slot, start) <= min(latest_start_slot, end - occupied_slots):
            periods.append(d)
    return periods


def effective_window(unit, dba_period, dba_period_slots):
    """求实例时间窗与指定 DBA 周期的交集，并为 guard 预留空间。"""
    start, end = dba_period * dba_period_slots, (dba_period + 1) * dba_period_slots
    return (max(int(unit["release_slot"]), start),
            min(int(unit["latest_start_slot"]), end - int(unit["occupied_slots"])))
