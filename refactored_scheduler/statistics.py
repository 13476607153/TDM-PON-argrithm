"""调度结果统计。

聚合流的成功率、平均延迟、DJSR 和 RUE 等指标按 num_flows 加权，
避免同周期合并后低估原始流数量。
"""

import numpy as np
import pandas as pd


def calculate_jitter_statistics(refined_df):
    """计算二次调度后各类 TS 流的聚合口径抖动。"""
    if refined_df.empty or "flow_id" not in refined_df.columns:
        return {"Iso": [], "Cli": []}, {}

    flow_jitter = []
    # 抖动必须在同一周期流的多个实例之间计算，不能跨 flow_id 混合。
    for flow_id in refined_df["flow_id"].unique():
        flow_packets = refined_df[refined_df["flow_id"] == flow_id]
        if len(flow_packets) <= 1:
            continue
        flow_type = flow_packets.iloc[0].get("flow_type", "Unknown")
        num_flows = int(flow_packets.iloc[0].get("num_flows", 1))
        delays = flow_packets["start_time_refined"] - flow_packets["arrival_time"]
        jitter = delays.max() - delays.min()
        flow_jitter.append({
            "flow_id": flow_id,
            "flow_type": flow_type,
            "jitter": jitter,
            "num_flows": num_flows,
        })

    jitter_df = pd.DataFrame(flow_jitter)
    if jitter_df.empty:
        return {"Iso": [], "Cli": []}, {}

    jitter_stats = {
        "Iso": jitter_df[jitter_df["flow_type"].str.contains("Iso", na=False)]["jitter"].tolist(),
        "Cli": jitter_df[jitter_df["flow_type"].str.contains("Cli", na=False)]["jitter"].tolist(),
    }

    worst_flows = {}
    for ftype in ["Iso", "Cli"]:
        subset = jitter_df[jitter_df["flow_type"].str.contains(ftype, na=False)]
        if not subset.empty:
            worst = subset.loc[subset["jitter"].idxmax()]
            worst_flows[ftype] = {
                "流量ID": int(worst["flow_id"]),
                "最大抖动": float(worst["jitter"]),
                "聚合原始流数量": int(worst.get("num_flows", 1)),
            }
            print(
                f"[WARN] {ftype} 类流中，最大抖动的是聚合 flow_id={worst['flow_id']}，"
                f"抖动={worst['jitter']:.2f} us，代表原始流数量={int(worst.get('num_flows', 1))}"
            )

    return jitter_stats, worst_flows


def _get_weights(df):
    """读取 num_flows 作为统计权重；缺失时按 1 条原始流处理。"""
    # 最终中文结果表使用“聚合原始流数量”；缺少该列表示每行权重默认为 1。
    if "聚合原始流数量" in df.columns:
        return pd.to_numeric(df["聚合原始流数量"], errors="coerce").fillna(1).astype(int)
    return pd.Series(np.ones(len(df), dtype=int), index=df.index)


def _weighted_mean(values, weights):
    """计算加权平均值；空输入或零权重时返回 NaN。"""
    values = pd.to_numeric(values, errors="coerce")
    # 失败记录通常没有数值延迟，先过滤 NaN，同时保持权重索引对齐。
    mask = values.notna()
    if mask.sum() == 0:
        return float("nan")
    return float(np.average(values[mask], weights=weights[mask]))


def build_weighted_delay_stats(final_schedule_df):
    """生成按原始流数量加权的延迟统计表。"""
    succ_df = final_schedule_df[final_schedule_df["调度状态"] == "成功"].copy()
    rows = []
    for ftype, group in succ_df.groupby("流类型"):
        weights = _get_weights(group)
        delays = pd.to_numeric(group["延迟(us)"], errors="coerce")
        rows.append({
            "流类型": ftype,
            "统计口径": "原始流加权" if "聚合原始流数量" in group.columns else "调度单元",
            "最大延迟": delays.max() if not delays.empty else float("nan"),
            "最小延迟": delays.min() if not delays.empty else float("nan"),
            "平均延迟": _weighted_mean(delays, weights),
            "抖动": (delays.max() - delays.min()) if len(delays.dropna()) > 1 else 0.0,
            "聚合调度单元数": len(group),
            "原始流加权数量": int(weights.sum()),
        })

    if not succ_df.empty:
        weights = _get_weights(succ_df)
        delays = pd.to_numeric(succ_df["延迟(us)"], errors="coerce")
        rows.append({
            "流类型": "ALL",
            "统计口径": "原始流加权" if "聚合原始流数量" in succ_df.columns else "调度单元",
            "最大延迟": delays.max(),
            "最小延迟": delays.min(),
            "平均延迟": _weighted_mean(delays, weights),
            "抖动": (delays.max() - delays.min()) if len(delays.dropna()) > 1 else 0.0,
            "聚合调度单元数": len(succ_df),
            "原始流加权数量": int(weights.sum()),
        })

    return pd.DataFrame(rows)


def build_flow_type_success_stats(final_schedule_df, expected_counts=None):
    """按 Iso_Type1/Iso_Type2/Cli_Type1/Cli_Type2/BE_Type 生成细分类成功率统计。"""
    expected_counts = expected_counts or {}
    rows = []

    for flow_type, group in final_schedule_df.groupby("流类型", dropna=False):
        # 周期流在结果表中包含多个 cycle_n；成功/失败数量必须按唯一流计数，
        # 不能把每个周期实例重复乘以 num_flows。
        # TS 一条流会有多个周期实例，因此先折叠到流级；任一实例失败，
        # 该流整体即按失败处理。BE 每个 flow_id 只有一个包。
        flow_level = group.sort_values("流ID").groupby("流ID", as_index=False).agg({
            "聚合原始流数量": "first",
            "调度状态": lambda values: "成功" if (values == "成功").all() else "失败",
        })
        weights = _get_weights(flow_level)
        success_group = group[group["调度状态"] == "成功"]
        success_flows = flow_level[flow_level["调度状态"] == "成功"]
        failed_flows = flow_level[flow_level["调度状态"] == "失败"]
        success_weights = _get_weights(success_flows) if not success_flows.empty else pd.Series(dtype=int)
        failed_weights = _get_weights(failed_flows) if not failed_flows.empty else pd.Series(dtype=int)

        # 提供生成数量时以参数快照为分母，使未进入结果表的业务也计入失败。
        expected = expected_counts.get(flow_type)
        observed_total = int(weights.sum()) if not group.empty else 0
        denominator = expected if expected is not None else observed_total
        success_total = int(success_weights.sum()) if not success_flows.empty else 0
        failed_observed = int(failed_weights.sum()) if not failed_flows.empty else 0
        failed_total = max(0, int(denominator) - success_total) if expected is not None else failed_observed
        success_rate = success_total / denominator * 100 if denominator else 0.0

        delays = pd.to_numeric(success_group["延迟(us)"], errors="coerce") if not success_group.empty else pd.Series(dtype=float)
        delay_weights = _get_weights(success_group) if not success_group.empty else pd.Series(dtype=int)

        rows.append({
            "流类型": flow_type,
            "参数生成数量": expected if expected is not None else observed_total,
            "结果表原始流加权数量": observed_total,
            "成功数量": success_total,
            "失败数量": failed_total,
            "成功率(%)": round(success_rate, 2),
            "平均延迟(us)": _weighted_mean(delays, delay_weights) if not success_group.empty else float("nan"),
            "最大延迟(us)": delays.max() if not delays.empty else float("nan"),
            "最小延迟(us)": delays.min() if not delays.empty else float("nan"),
        })

    order = ["Iso_Type1", "Iso_Type2", "Cli_Type1", "Cli_Type2", "BE_Type"]
    result = pd.DataFrame(rows)
    if not result.empty:
        result["_order"] = result["流类型"].apply(lambda value: order.index(value) if value in order else len(order))
        result = result.sort_values(["_order", "流类型"]).drop(columns=["_order"]).reset_index(drop=True)
    return result


def compute_statistics(final_schedule_df, supercycle_size, flow_params=None):
    """
    统计等时流/循环流指标。
    注意：
    - 成功率、平均延迟、DJSR 采用“原始流数量加权口径”；
    - 最大延迟、最大抖动仍按实际调度结果取最大值；
    - RUE/资源利用率是传输时长口径，不按 num_flows 加权。
    """
    print("\n" + "=" * 60)
    print("[INFO] 调度统计模块（原始流加权口径）")
    print("=" * 60)

    stats_result = {}

    for ftype, label in [("Iso", "等时流"), ("Cli", "循环流")]:
        mask = final_schedule_df["流类型"].str.contains(ftype, na=False)
        subset = final_schedule_df[mask].copy()
        if subset.empty:
            total_weight = 0
            success_weight = 0
            success_rate = 0.0
            succ_subset = subset
        else:
            weights_all = _get_weights(subset)
            total_weight = int(weights_all.sum())
            succ_subset = subset[subset["调度状态"] == "成功"].copy()
            weights_succ = _get_weights(succ_subset) if not succ_subset.empty else pd.Series(dtype=int)
            success_weight = int(weights_succ.sum()) if not succ_subset.empty else 0
            success_rate = success_weight / total_weight * 100 if total_weight > 0 else 0.0

        delays = pd.to_numeric(succ_subset["延迟(us)"], errors="coerce") if not succ_subset.empty else pd.Series(dtype=float)
        max_delay = delays.max() if not delays.empty else float("nan")
        avg_delay = _weighted_mean(delays, _get_weights(succ_subset)) if not succ_subset.empty else float("nan")

        # 资源占用按实际实例求和，不乘聚合权重；聚合只影响业务数量口径。
        tx_success_ftype = pd.to_numeric(succ_subset["传输时长"], errors="coerce").sum() if not succ_subset.empty else 0.0
        tx_allocated_ftype = pd.to_numeric(subset["传输时长"], errors="coerce").sum() if not subset.empty else 0.0
        rue_ftype = tx_success_ftype / tx_allocated_ftype * 100 if tx_allocated_ftype > 0 else 0.0

        max_jitter = float("nan")
        djsr_satisfy_weight = 0
        djsr = float("nan")
        if flow_params is not None and total_weight > 0:
            matched_key = next((k for k in flow_params if k.startswith(ftype)), None)
            if matched_key:
                delay_limit = flow_params[matched_key]["delay"]
                jitter_limit = flow_params[matched_key]["jitter"]

                if not succ_subset.empty and "流ID" in succ_subset.columns:
                    flow_actual_jitter = succ_subset.groupby("流ID")["延迟(us)"].agg(
                        lambda d: d.max() - d.min() if len(d) > 1 else 0.0
                    )
                    max_jitter = flow_actual_jitter.max() if not flow_actual_jitter.empty else float("nan")

                    succ_with_jitter = succ_subset.copy()
                    succ_with_jitter["_flow_jitter"] = succ_with_jitter["流ID"].map(flow_actual_jitter)
                    delay_ok = pd.to_numeric(succ_with_jitter["延迟(us)"], errors="coerce") <= delay_limit
                    jitter_ok = succ_with_jitter["_flow_jitter"] <= jitter_limit
                    ok_mask = delay_ok & jitter_ok
                    djsr_satisfy_weight = int(_get_weights(succ_with_jitter[ok_mask]).sum())
                djsr = djsr_satisfy_weight / total_weight * 100 if total_weight > 0 else float("nan")
        else:
            if not succ_subset.empty and "流ID" in succ_subset.columns:
                flow_actual_jitter = succ_subset.groupby("流ID")["延迟(us)"].agg(
                    lambda d: d.max() - d.min() if len(d) > 1 else 0.0
                )
                max_jitter = flow_actual_jitter.max() if not flow_actual_jitter.empty else float("nan")

        stats_result[ftype] = {
            "统计口径": "原始流数量加权",
            "聚合调度单元总数": int(len(subset)),
            "聚合调度单元成功数": int(len(succ_subset)),
            "总数": total_weight,
            "成功数": success_weight,
            "失败数": total_weight - success_weight,
            "调度成功率(%)": round(success_rate, 2),
            "最大延迟(us)": round(max_delay, 2) if not np.isnan(max_delay) else None,
            "平均延迟(us)": round(avg_delay, 2) if not np.isnan(avg_delay) else None,
            "最大抖动(us)": round(max_jitter, 2) if not np.isnan(max_jitter) else None,
            "成功传输时长(us)": round(tx_success_ftype, 6),
            "分配传输时长(us)": round(tx_allocated_ftype, 6),
            "资源利用效率RUE(%)": round(rue_ftype, 2),
            "延迟抖动满足数": djsr_satisfy_weight,
            "延迟抖动满足率DJSR(%)": round(djsr, 2) if not np.isnan(djsr) else None,
        }

        print(f"\n【{label} ({ftype})】")
        print(f"  聚合调度单元: 总数={len(subset)} 成功={len(succ_subset)}")
        print(f"  原始流加权:   总数={total_weight} 成功={success_weight} 失败={total_weight - success_weight}")
        print(f"  调度成功率: {success_rate:.2f}%")
        if not np.isnan(max_delay):
            print(f"  最大延迟: {max_delay:.2f} us  加权平均延迟: {avg_delay:.2f} us")
        else:
            print("  延迟: 无成功调度数据")
        if not np.isnan(max_jitter):
            print(f"  最大抖动: {max_jitter:.2f} us")
        else:
            print("  抖动: 数据不足")
        print(f"  RUE: {rue_ftype:.2f}%  (成功传输={tx_success_ftype:.6f}us / 分配总带宽={tx_allocated_ftype:.6f}us)")
        if not np.isnan(djsr):
            print(f"  DJSR: {djsr:.2f}%  (满足原始流加权数={djsr_satisfy_weight} / 总数={total_weight})")

    succ_mask = final_schedule_df["调度状态"] == "成功"
    total_tx_time = pd.to_numeric(final_schedule_df[succ_mask]["传输时长"], errors="coerce").sum()
    payload_time = pd.to_numeric(final_schedule_df[succ_mask].get("包发送占用(us)", 0), errors="coerce").sum()
    guard_time = pd.to_numeric(final_schedule_df[succ_mask].get("隔离带占用(us)", 0), errors="coerce").sum()
    occupied_time = pd.to_numeric(final_schedule_df[succ_mask].get("资源占用(us)", 0), errors="coerce").sum()
    raw_time = pd.to_numeric(final_schedule_df[succ_mask].get("原始传输时长(us)", 0), errors="coerce").sum()
    resource_util = occupied_time / supercycle_size * 100

    ts_mask = final_schedule_df["流类型"].str.contains("Iso|Cli", na=False)
    ts_success_tx = pd.to_numeric(final_schedule_df[ts_mask & succ_mask]["传输时长"], errors="coerce").sum()
    ts_allocated_tx = pd.to_numeric(final_schedule_df[ts_mask]["传输时长"], errors="coerce").sum()
    rue_total = ts_success_tx / ts_allocated_tx * 100 if ts_allocated_tx > 0 else 0.0

    stats_result["资源利用率"] = {
        "总传输时长(us)": round(total_tx_time, 6),
        "超周期大小(us)": supercycle_size,
        "资源利用率(%)": round(resource_util, 2),
        "Payload占用时长(us)": round(payload_time, 6),
        "隔离带占用时长(us)": round(guard_time, 6),
        "总资源占用时长(us)": round(occupied_time, 6),
        "Payload利用率(%)": round(payload_time / supercycle_size * 100, 2),
        "链路占用率(%)": round(resource_util, 2),
        "Guard开销率(%)": round(guard_time / occupied_time * 100, 2) if occupied_time else 0.0,
        "物理有效载荷率(%)": round(raw_time / supercycle_size * 100, 2),
        "TS流成功传输时长(us)": round(ts_success_tx, 6),
        "TS流分配总带宽(us)": round(ts_allocated_tx, 6),
        "资源利用效率RUE(%)": round(rue_total, 2),
    }

    print("\n【算法总体资源利用率 & 资源利用效率】")
    print(f"  成功调度总传输时长: {total_tx_time:.6f} us")
    print(f"  超周期大小: {supercycle_size} us")
    print(f"  资源利用率: {resource_util:.2f}%")
    print(f"  TS流成功传输时长: {ts_success_tx:.6f} us")
    print(f"  TS流分配总带宽:   {ts_allocated_tx:.6f} us")
    print(f"  RUE: {rue_total:.2f}%")
    print("=" * 60)

    return stats_result
