"""流量生成与同周期合并。

包含 BE 泊松到达流量生成，以及 TS 周期流按 ONU 和周期进行合并的逻辑。
合并函数保留 num_flows 与 flow_ids，用于后续按原始流数量加权统计。
"""

from collections import defaultdict

import numpy as np

from .config import MAX_MERGE_SIZE, NUM_ONU, R


def generate_ts_flows(flow_counts, flow_params, num_onu=NUM_ONU, seed=1):
    """按给定数量生成四类 TS 周期流。

    flow_counts 的 key 为流类型名，例如 Iso_Type1、Cli_Type2；value 为该类型生成数量。
    start_time 使用局部随机生成器生成，避免污染 numpy 全局随机状态。
    """
    rng = np.random.default_rng(seed)
    flow_list = []
    flow_id = 0

    for flow_type, count in flow_counts.items():
        params = flow_params[flow_type]
        for i in range(count):
            flow_list.append({
                "flow_id": flow_id,
                "onu_id": i % num_onu,
                "cycle": params["cycle"],
                "size": params["size"],
                "delay": params["delay"],
                "jitter": params["jitter"],
                "start_time": int(rng.integers(0, params["cycle"])),
                "flow_type": flow_type,
                "num_flows": 1,
                "flow_ids": [flow_id],
            })
            flow_id += 1

    return flow_list


def generate_be_traffic(lambda_val, num_packets, seed=1):
    """生成 BE 流的到达时间、传输时长和 ONU 归属。

    到达间隔服从指数分布；包长在 64 到 1500 byte 之间随机取值，
    再根据链路速率换算为微秒级传输时长。
    """
    rng = np.random.default_rng(seed)
    current_time = 0.0
    arrival_times = []
    packet_sizes = []

    while len(arrival_times) < num_packets:
        U = rng.uniform(0, 1)
        interval = -np.log(U) / lambda_val
        current_time += interval
        arrival_times.append(current_time)

        size_bytes = rng.integers(64, 1501)
        size_transmit_time = size_bytes * 8 / R * 1e6
        packet_sizes.append(size_transmit_time)

    onu_ids = rng.integers(0, NUM_ONU, size=len(arrival_times))
    return np.array(arrival_times), np.array(packet_sizes), onu_ids


def _make_merged(onu_id, group):
    """把同 ONU、同周期的一组循环流封装为一条聚合流。"""
    ftype = group[0].get("_orig_flow_type", group[0]["flow_type"])
    return {
        "onu_id": onu_id,
        "cycle": group[0]["cycle"],
        "delay": min(f["delay"] for f in group),
        "jitter": min(f["jitter"] for f in group),
        "start_time": min(f["start_time"] for f in group),
        "size": sum(f["size"] for f in group),
        "flow_type": ftype,
        "num_flows": len(group),
        "flow_ids": [f["flow_id"] for f in group],
        "flows": group,
    }


def same_cycle_merge(flow_list):
    """按同 ONU、同周期合并循环流。

    Iso 流不合并，逐条透传；Cli/循环流按 (onu_id, cycle) 分组。
    每个合并桶累加 size，delay 和 jitter 取组内最严格值；若合并后
    超过单时隙容量，则切分为多条聚合流，避免改变容量约束语义。
    """
    onu_flows_map = defaultdict(list)
    for f in flow_list:
        onu_flows_map[f["onu_id"]].append(f)

    shaped = []
    for onu_id in sorted(onu_flows_map.keys()):
        all_flows = onu_flows_map[onu_id]
        iso_flows = [f for f in all_flows if f["flow_type"] == "isochronous"]
        cyc_flows = [f for f in all_flows if f["flow_type"] == "cyclic"]

        for f in iso_flows:
            shaped.append({
                "onu_id": onu_id,
                "cycle": f["cycle"],
                "delay": f["delay"],
                "jitter": f["jitter"],
                "start_time": f["start_time"],
                "size": f["size"],
                "flow_type": f.get("_orig_flow_type", f["flow_type"]),
                "num_flows": 1,
                "flow_ids": [f["flow_id"]],
                "flows": [f],
            })

        cycle_groups = defaultdict(list)
        for f in cyc_flows:
            cycle_groups[f["cycle"]].append(f)

        for cycle_val in sorted(cycle_groups.keys()):
            bucket = []
            bucket_size = 0
            for f in cycle_groups[cycle_val]:
                if bucket and bucket_size + f["size"] > MAX_MERGE_SIZE:
                    shaped.append(_make_merged(onu_id, bucket))
                    bucket = []
                    bucket_size = 0
                bucket.append(f)
                bucket_size += f["size"]

            if bucket:
                shaped.append(_make_merged(onu_id, bucket))

    return shaped
