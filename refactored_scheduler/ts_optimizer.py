"""TS 整流接纳与 DBA 周期粗分配 ILP。"""

from collections import defaultdict

from docplex.mp.model import Model

from .config import R
from .time_model import build_time_window, eligible_dba_periods, resource_slots


def _build_units(flow_list, supercycle_size, t_dba, tse, tprop, tproc):
    """把周期流展开为超周期内的实例，并预计算资源与候选 DBA 周期。"""
    units = []
    for flow in flow_list:
        resource = resource_slots(flow["size"], R)
        # n（cycle_n）是该流在超周期内的实例序号。
        for n in range(int(supercycle_size // flow["cycle"])):
            arrival = flow["start_time"] + n * flow["cycle"]
            window = build_time_window(arrival, flow["delay"], resource["payload_slots"],
                                       tse, tprop, tproc)
            periods = eligible_dba_periods(window["release_slot"], window["latest_start_slot"],
                                           resource["occupied_slots"], supercycle_size, t_dba)
            units.append({"unit_id": len(units), "flow_id": flow["flow_id"],
                "onu_id": flow["onu_id"], "cycle_n": n, "n": n,
                "arrival_time": float(arrival), "arrival": float(arrival),
                "flow_type": flow.get("flow_type", "Unknown"),
                "jitter": float(flow["jitter"]), "delay_limit": float(flow["delay"]),
                "num_flows": int(flow.get("num_flows", 1)),
                "flow_ids": flow.get("flow_ids", [flow["flow_id"]]),
                "eligible_dba_periods": periods, **resource, **window})
    return units


def optimize_schedule(flow_list, supercycle_size, t_dba, tse=1.0, tprop=25.0,
                      tproc=1.0, tg=1.0, time_unit=1.0):
    """返回 ILP 接纳实例；精确开始时间由全局 slot 日历决定。"""
    del tg, time_unit
    units = _build_units(flow_list, int(supercycle_size), int(t_dba), tse, tprop, tproc)
    # by_flow 用于建立整流接纳约束；by_period 用于建立周期容量约束。
    by_flow, by_period = defaultdict(list), defaultdict(list)
    for unit in units:
        by_flow[unit["flow_id"]].append(unit)
        for period in unit["eligible_dba_periods"]:
            by_period[period].append(unit)

    mdl = Model(name="TS_DBA_COARSE_ALLOCATION_1US")
    # accept[f]=1 表示整条周期流被接纳；assign[u,d]=1 表示实例 u 被分配到 DBA d。
    accept = {fid: mdl.binary_var(name=f"accept_{fid}") for fid in by_flow}
    assign = {(u["unit_id"], d): mdl.binary_var(name=f"z_{u['unit_id']}_{d}")
              for u in units for d in u["eligible_dba_periods"]}

    # 同一流的每个实例都必须等于同一个 accept 值，禁止只接纳部分周期实例。
    for fid, flow_units in by_flow.items():
        if any(not unit["eligible_dba_periods"] for unit in flow_units):
            mdl.add_constraint(accept[fid] == 0)
        for unit in flow_units:
            mdl.add_constraint(mdl.sum(assign[unit["unit_id"], d]
                                       for d in unit["eligible_dba_periods"]) == accept[fid])

    period_load = {}
    for period in range(int(supercycle_size // t_dba)):
        period_units = by_period.get(period, [])
        period_load[period] = mdl.sum(unit["occupied_slots"] * assign[unit["unit_id"], period]
                                      for unit in period_units)
        mdl.add_constraint(period_load[period] <= t_dba)

        # 仅有 DBA 总容量仍可能出现大量窄时间窗集中在周期前部的情况。
        # 因此增加截止点前缀必要条件：必须在 deadline 前完成的总工作量，
        # 不得超过从 DBA 起点到该 deadline 的可用长度。
        deadlines = sorted({min(unit["service_deadline_slot"], (period + 1) * t_dba)
                            for unit in period_units})
        period_start = period * t_dba
        for deadline in deadlines:
            forced = [unit for unit in period_units
                      if min(unit["service_deadline_slot"], (period + 1) * t_dba) <= deadline]
            mdl.add_constraint(mdl.sum(unit["occupied_slots"] * assign[unit["unit_id"], period]
                                       for unit in forced) <= max(0, deadline - period_start))

    # dmax 表示任意两个 DBA 周期之间的最大负载差，用作次级均衡目标。
    dmax = mdl.continuous_var(lb=0, name="dmax")
    periods = list(period_load)
    for left in periods:
        for right in periods[left + 1:]:
            mdl.add_constraint(dmax >= period_load[left] - period_load[right])
            mdl.add_constraint(dmax >= period_load[right] - period_load[left])

    total_weight = sum(int(flow_units[0].get("num_flows", 1)) for flow_units in by_flow.values())
    # 主目标按 num_flows 最大化接纳的原始流数量；足够大的主目标权重确保
    # 负载均衡项不会以少接纳业务为代价获得更优目标值。
    mdl.maximize((int(supercycle_size) + 1) * mdl.sum(
        int(flow_units[0].get("num_flows", 1)) * accept[fid]
        for fid, flow_units in by_flow.items()) - dmax / max(1, total_weight))

    solution = mdl.solve(log_output=False)
    if not solution:
        print("[ERROR] 未找到可行的 TS DBA 粗分配解")
        return None, 0, {}

    result = []
    rejected = 0
    for fid, flow_units in by_flow.items():
        if solution.get_value(accept[fid]) <= 0.5:
            rejected += 1
            continue
        for unit in flow_units:
            period = next(d for d in unit["eligible_dba_periods"]
                          if solution.get_value(assign[unit["unit_id"], d]) > 0.5)
            result.append({**unit, "dba_period": period, "size": unit["raw_tx_time_us"]})

    print(f"[OK] ILP 接纳 {len(by_flow) - rejected}/{len(by_flow)} 条 TS 流，"
          f"分配 {len(result)}/{len(units)} 个实例")
    return result, len(result), {"Iso": [], "Cli": []}
