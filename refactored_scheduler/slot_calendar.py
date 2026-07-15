"""全局 1 μs 链路日历，以及 TS 精确放置和 BE 补调度。"""

from collections import defaultdict
import math

import numpy as np
import pandas as pd

from .config import DBA_PERIOD_US, GUARD_SLOTS, supercycle_size
from .time_model import effective_window


class LinkCalendar:
    """共享链路的离散占用表；每个元素对应一个 1 us slot。"""
    def __init__(self, size):
        self.slots = [None] * int(size)

    def is_free(self, start, end):
        return 0 <= start <= end <= len(self.slots) and all(v is None for v in self.slots[start:end])

    def reserve(self, unit, start):
        """原子检查并写入一个实例的 payload 与 guard；冲突时不修改日历。"""
        payload_end = start + int(unit["payload_slots"])
        occupied_end = start + int(unit["occupied_slots"])
        if not self.is_free(start, occupied_end):
            return False
        # token 同时包含流 ID 和周期实例序号，便于追踪日历中的占用来源。
        token = (int(unit["flow_id"]), int(unit.get("cycle_n", 0)))
        for idx in range(start, payload_end):
            self.slots[idx] = ("PAYLOAD", token)
        for idx in range(payload_end, occupied_end):
            self.slots[idx] = ("GUARD", token)
        return True

    def release_flow(self, flow_id):
        """释放指定流的所有周期实例，用于整流失败后的事务式回滚。"""
        for idx, value in enumerate(self.slots):
            if value is not None and value[1][0] == int(flow_id):
                self.slots[idx] = None

    def occupied_count(self):
        return sum(value is not None for value in self.slots)


def _flow_key(units):
    """优先调度约束更严格的流，最后用 flow_id 保证排序稳定。"""
    first = units[0]
    widths = [u["latest_start_slot"] - u["release_slot"] for u in units]
    return (first["jitter"], min(widths), min(u["latest_start_slot"] for u in units),
            -first["occupied_slots"], first["flow_id"])


def _candidate_starts(unit, target, dba_period_slots):
    """按“最接近统一相位、同距离时更早”生成候选起点。"""
    low, high = effective_window(unit, unit["dba_period"], dba_period_slots)
    if low > high:
        return []
    return sorted(range(low, high + 1), key=lambda value: (abs(value - target), value))


def schedule_ts_units(units, supercycle_slots, dba_period_slots):
    """按整条流事务式放置，失败时回滚其全部实例。"""
    calendar = LinkCalendar(supercycle_slots)
    by_flow = defaultdict(list)
    for unit in units:
        by_flow[int(unit["flow_id"])].append(unit)

    successful, failed = [], []
    for flow_units in sorted(by_flow.values(), key=_flow_key):
        flow_units.sort(key=lambda unit: unit["cycle_n"])
        first = flow_units[0]
        low, high = effective_window(first, first["dba_period"], dba_period_slots)
        placed = None
        failure_reason = "CALENDAR_CAPACITY_CONFLICT"

        # 枚举首实例起点，相当于枚举整条周期流的候选相位偏移。
        for first_start in range(low, high + 1):
            phase = first_start - first["arrival_time"]
            trial, delays, valid = [], [], True
            for unit in flow_units:
                target = round(unit["arrival_time"] + phase)
                chosen = next((start for start in _candidate_starts(unit, target, dba_period_slots)
                               if calendar.is_free(start, start + unit["occupied_slots"])), None)
                if chosen is None:
                    valid = False
                    break
                # 此处先临时写入共享日历；本流后续失败时统一 release_flow。
                calendar.reserve(unit, chosen)
                trial.append((unit, chosen))
                delays.append(chosen - unit["arrival_time"])

            if valid and max(delays) - min(delays) <= first["jitter"] + 1e-9:
                placed = trial
                break
            # 任一实例失败或整流抖动超限，必须撤销本流已放置的全部实例。
            calendar.release_flow(first["flow_id"])
            if valid:
                failure_reason = "JITTER_VIOLATION"

        if placed is None:
            failed.extend({**unit, "failure_reason": failure_reason} for unit in flow_units)
            continue

        jitter = max(start - unit["arrival_time"] for unit, start in placed) - min(
            start - unit["arrival_time"] for unit, start in placed)
        for unit, start in placed:
            successful.append({**unit, "start_time_refined": float(start),
                "end_time_refined": float(start + unit["payload_slots"]),
                "payload_end_slot": start + unit["payload_slots"],
                "occupied_end_slot": start + unit["occupied_slots"],
                "actual_flow_jitter_us": float(jitter)})

    return pd.DataFrame(successful), pd.DataFrame(failed), calendar


def refine_ts_schedule(ilp_results, return_calendar=False):
    """把 ILP 粗分配结果转换为最终 TS 日历结果。"""
    units = (
        ilp_results.to_dict("records")
        if isinstance(ilp_results, pd.DataFrame)
        else list(ilp_results)
    )
    success, failed, calendar = schedule_ts_units(
        units, supercycle_size, DBA_PERIOD_US
    )
    if not success.empty:
        success["duration"] = success["payload_slots"].astype(float)
        success["slot"] = success["start_time_refined"].astype(int)
        success["slot_size"] = 1.0
        success["slot_start"] = success["start_time_refined"]
        success["slot_end"] = success["occupied_end_slot"].astype(float)
        success["status"] = "scheduled"
    if not failed.empty:
        failed["duration"] = failed["payload_slots"].astype(float)
        failed["slot"] = failed["dba_period"].astype(int) * DBA_PERIOD_US
        failed["slot_size"] = 1.0
        failed["slot_start"] = failed["slot"].astype(float)
        failed["slot_end"] = failed["slot_start"] + DBA_PERIOD_US
        failed["status"] = "failed"
    if return_calendar:
        return success, failed, calendar
    return success, failed


def _load_legacy_ts_schedule(calendar, ts_schedule):
    """把旧格式 TS 时间窗装入空日历，仅用于兼容旧数据输入。"""
    if not ts_schedule or any(value is not None for value in calendar.slots):
        return
    for index, time_window in enumerate(ts_schedule):
        start = int(math.floor(time_window["start"]))
        payload = max(1, int(math.ceil(time_window.get("size", 1.0) - 1e-12)))
        unit = {
            "flow_id": -(index + 1),
            "cycle_n": 0,
            "payload_slots": payload,
            "occupied_slots": payload + GUARD_SLOTS,
        }
        calendar.reserve(unit, start)


def schedule_be_packets(
    arrival_times,
    packet_durations,
    onu_ids,
    *,
    calendar=None,
    ts_schedule=None,
    supercycle_slots=supercycle_size,
    dba_period_slots=DBA_PERIOD_US,
):
    """按到达顺序将 BE 包首次适配到 TS 日历的剩余空间。"""
    calendar = calendar or LinkCalendar(int(supercycle_slots))
    _load_legacy_ts_schedule(calendar, ts_schedule)
    schedule = [None] * len(arrival_times)

    for index in np.argsort(arrival_times, kind="stable"):
        arrival = float(arrival_times[index])
        raw_duration = float(packet_durations[index])
        payload = max(1, int(math.ceil(raw_duration - 1e-12)))
        occupied = payload + GUARD_SLOTS
        start = int(math.ceil(arrival - 1e-12))

        while start + occupied <= int(supercycle_slots):
            dba_end = (
                start // int(dba_period_slots) + 1
            ) * int(dba_period_slots)
            if start + occupied > dba_end:
                start = dba_end
                continue
            if calendar.is_free(start, start + occupied):
                unit = {
                    "flow_id": 1_000_000_000 + int(index),
                    "cycle_n": 0,
                    "payload_slots": payload,
                    "occupied_slots": occupied,
                }
                calendar.reserve(unit, start)
                schedule[index] = {
                    "start": float(start),
                    "end": float(start + payload),
                    "occupied_end": float(start + occupied),
                    "onu_id": int(onu_ids[index]),
                    "raw_tx_time_us": raw_duration,
                    "payload_slots": payload,
                    "guard_slots": GUARD_SLOTS,
                    "occupied_slots": occupied,
                }
                break
            start += 1
    return schedule
