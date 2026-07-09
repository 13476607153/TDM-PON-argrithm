"""TS 流 ILP 调度模型。

该模块负责把周期流展开为超周期内的调度单元，建立时间窗、DBA 周期容量、
抖动和负载均衡约束，并调用 docplex 求解。
"""

import numpy as np
from docplex.mp.model import Model


def optimize_schedule(flow_list, supercycle_size, t_dba, tse=1.0, tprop=25.0, tproc=1.0, tg=1.0, time_unit=1.0):
    """求解 TS 流的一阶段 ILP 调度。

    参数 flow_list 可传入原始 TS 流，也可传入同周期合并后的聚合流。
    返回成功调度的时间窗列表、成功调度单元数，以及按 Iso/Cli 聚合口径统计的延迟。
    """
    R_model = 50e9
    LINEARIZATION_BOUND = 1e6  # 条件约束线性化使用的充分大上界。

    # 将每条周期流展开为超周期内的多个调度单元。
    units = []
    flow_map = {}
    for f in flow_list:
        N = supercycle_size // f["cycle"]
        duration = f["size"] * 8 / R_model * 1e6
        for n in range(int(N)):
            at = f["start_time"] + n * f["cycle"]
            ubd = at + f["delay"] - 2 * tse - 2 * tproc - tprop - duration
            unit = {
                "flow_id": f["flow_id"],
                "onu_id": f["onu_id"],
                "cycle": f["cycle"],
                "n": n,
                "arrival": at,
                "ubd": ubd,
                "jitter": f["jitter"],
                "size": duration,
                "stream_key": f["flow_id"],
                "flow_type": f.get("flow_type", "Unknown"),
                "num_flows": f.get("num_flows", 1),
                "flow_ids": f.get("flow_ids", [f["flow_id"]]),
            }
            units.append(unit)
            flow_map.setdefault(f["flow_id"], []).append(len(units) - 1)

    U = len(units)
    print(f"调度单元的数量 U = {U}")
    S = int(supercycle_size // t_dba)

    # 候选开始时刻采用离散化网格，降低 ILP 变量数量。
    candidate_step = 10.0
    candidate_ts = []
    for unit in units:
        t_start = unit["arrival"] + tse
        t_end = unit["ubd"]
        cands = list(np.arange(t_start, t_end + 1e-6, candidate_step))
        candidate_ts.append(cands)

    for u, cands in enumerate(candidate_ts):
        if not cands:
            print(f"[WARN] 调度单元 {u} 无候选起点：arrival={units[u]['arrival']:.3f}, ubd={units[u]['ubd']:.3f}")

    # x 表示调度单元是否成功，s 表示开始时间，y 表示选中的离散候选起点。
    mdl = Model(name="TA-DetBA_ILP_OBJ2_FIXED_WEIGHTED_STATS")
    x = mdl.binary_var_list(U, name="x")
    s = mdl.continuous_var_list(U, name="s")
    y = [
        [mdl.binary_var(name=f"y_{u}_{k}") for k in range(len(candidate_ts[u]))]
        for u in range(U)
    ]
    dmax = mdl.continuous_var(name="Dmax")

    for u, unit in enumerate(units):
        arrival, ubd, dur = unit["arrival"], unit["ubd"], unit["size"]
        if len(candidate_ts[u]) == 0:
            mdl.add_constraint(x[u] == 0)
            continue
        mdl.add_constraint(mdl.sum(y[u]) == x[u])
        mdl.add_constraint(s[u] == mdl.sum(candidate_ts[u][k] * y[u][k] for k in range(len(candidate_ts[u]))))
        mdl.add_constraint(s[u] >= (arrival + tse) * x[u])
        mdl.add_constraint(s[u] + dur <= ubd + (1 - x[u]) * LINEARIZATION_BOUND)
        mdl.add_constraint(s[u] >= 0)
        mdl.add_constraint(s[u] + dur <= supercycle_size)

    z = [[mdl.binary_var(name=f"z_{u}_{s_idx}") for s_idx in range(S)] for u in range(U)]
    for u in range(U):
        for s_idx in range(S):
            t_start = s_idx * t_dba
            t_end = (s_idx + 1) * t_dba - 1e-6
            b1 = mdl.binary_var(name=f"b1_{u}_{s_idx}")
            b2 = mdl.binary_var(name=f"b2_{u}_{s_idx}")
            mdl.add_constraint(s[u] >= t_start - (1 - b1) * LINEARIZATION_BOUND)
            mdl.add_constraint(s[u] <= t_end + (1 - b2) * LINEARIZATION_BOUND)
            mdl.add_constraint(z[u][s_idx] <= b1)
            mdl.add_constraint(z[u][s_idx] <= b2)
            mdl.add_constraint(z[u][s_idx] >= b1 + b2 - 1)
            mdl.add_constraint(z[u][s_idx] <= x[u])

    for s_idx in range(S):
        mdl.add_constraint(
            mdl.sum(z[u][s_idx] * units[u]["size"] for u in range(U)) <= t_dba
        )

    delta_max = {}
    delta_min = {}
    for fid, idxs in flow_map.items():
        jitter_i = units[idxs[0]]["jitter"]
        delta_max[fid] = mdl.continuous_var(name=f"delta_max_{fid}")
        delta_min[fid] = mdl.continuous_var(name=f"delta_min_{fid}")
        for u in idxs:
            arrival = units[u]["arrival"]
            delay = s[u] - arrival
            mdl.add_constraint(delta_max[fid] >= delay - (1 - x[u]) * LINEARIZATION_BOUND)
            mdl.add_constraint(delta_min[fid] <= delay + (1 - x[u]) * LINEARIZATION_BOUND)
        mdl.add_constraint(delta_max[fid] - delta_min[fid] <= jitter_i)

    for s1 in range(S):
        for s2 in range(s1 + 1, S):
            load1 = mdl.sum(z[u][s1] for u in range(U))
            load2 = mdl.sum(z[u][s2] for u in range(U))
            mdl.add_constraint(dmax >= load1 - load2)
            mdl.add_constraint(dmax >= load2 - load1)

    S_us = mdl.binary_var_list(len(flow_map), name="S_us")
    fid_to_pos = {fid: i for i, fid in enumerate(flow_map.keys())}
    for fid, idxs in flow_map.items():
        pos = fid_to_pos[fid]
        for u in idxs:
            mdl.add_constraint(S_us[pos] >= 1 - x[u])

    alpha = 1.0 / max(1, len(flow_map))
    beta = 1.0 / supercycle_size
    mdl.minimize(alpha * mdl.sum(S_us) + beta * dmax)

    sol = mdl.solve(log_output=False)
    if not sol:
        print("[ERROR] 未找到可行解")
        return None, 0, {}

    print("[OK] 调度成功")
    result = []
    count = 0
    for u, unit in enumerate(units):
        if sol.get_value(x[u]) > 0.5:
            start = sol.get_value(s[u])
            result.append({
                "flow_id": unit["flow_id"],
                "onu_id": unit["onu_id"],
                "n": unit["n"],
                "start": start,
                "size": unit["size"],
                "flow_type": unit["flow_type"],
                "arrival": unit["arrival"],
                "num_flows": unit.get("num_flows", 1),
                "flow_ids": unit.get("flow_ids", [unit["flow_id"]]),
            })
            count += 1
    print(f"成功调度 {count}/{U} 个调度单元")

    delay_dict = {"Iso": [], "Cli": []}
    for item in result:
        delay = item["start"] - item["arrival"]
        ftype = item.get("flow_type", "Unknown")
        if "Iso" in ftype or "isochronous" in ftype:
            delay_dict["Iso"].append(delay)
        elif "Cli" in ftype or "cyclic" in ftype:
            delay_dict["Cli"].append(delay)

    return result, count, delay_dict
