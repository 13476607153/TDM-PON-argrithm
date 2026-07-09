"""二次调度。

ILP 给出较粗的开始时间后，本模块按 Iso=1us、Cli=10us 的时隙粒度
重新排布同一时隙内的多个包，并保留 slot_size、slot_start、slot_end，
避免不同粒度的时隙编号在统计表中混淆。
"""

import pandas as pd


def _append_refined_row(rows, row, start_refined, end_refined, slot, slot_size, slot_end_time):
    """追加一条二次调度成功记录，并保留聚合流到原始流的映射字段。"""
    rows.append({
        "flow_id": int(row["flow_id"]),
        "onu_id": int(row["onu_id"]),
        "cycle_n": int(row["cycle_n"]) if "cycle_n" in row else 0,
        "start_time_refined": round(start_refined, 2),
        "end_time_refined": round(end_refined, 2),
        "duration": round(row["duration"], 6),
        "slot": int(slot),
        "slot_size": float(slot_size),
        "slot_start": round(slot * slot_size, 2),
        "slot_end": round(slot_end_time, 2),
        "status": "scheduled",
        "arrival_time": row["arrival_time"],
        "flow_type": row.get("flow_type", "Unknown"),
        "num_flows": int(row.get("num_flows", 1)),
        "flow_ids": row.get("flow_ids", [row["flow_id"]]),
    })


def _append_failed_row(rows, row, start_original, slot, slot_size, slot_end_time, current_time, reason_prefix):
    """追加一条二次调度失败记录，失败原因中写明目标时隙的剩余容量。"""
    rows.append({
        "flow_id": int(row["flow_id"]),
        "onu_id": int(row["onu_id"]),
        "cycle_n": int(row["cycle_n"]) if "cycle_n" in row else 0,
        "original_start_time": round(start_original, 2),
        "duration": round(row["duration"], 6),
        "slot": int(slot),
        "slot_size": float(slot_size),
        "slot_start": round(slot * slot_size, 2),
        "slot_end": round(slot_end_time, 2),
        "status": "failed",
        "failure_reason": f"{reason_prefix}时隙{slot}容量不足，需要{row['duration']:.6f}us，剩余{slot_end_time - current_time:.6f}us",
        "arrival_time": row["arrival_time"],
        "flow_type": row.get("flow_type", "Unknown"),
        "num_flows": int(row.get("num_flows", 1)),
        "flow_ids": row.get("flow_ids", [row["flow_id"]]),
    })


def secondary_scheduling_with_overload_handling(ilp_results, iso_slot_size=1.0, cli_slot_size=10.0, gap=0.01):
    """执行二次调度并返回成功表与失败表。

    同一 slot 内按开始时间排序，逐包放置；若剩余容量不足，则写入失败表，
    并保留原始聚合映射字段，方便后续按原始流数量加权统计。
    """
    refined_results = []
    failed_results = []
    ilp_results = ilp_results.copy()

    if ilp_results.empty:
        return pd.DataFrame(refined_results), pd.DataFrame(failed_results)

    iso_mask = ilp_results["flow_type"].str.contains("Iso", na=False)
    cli_mask = ilp_results["flow_type"].str.contains("Cli", na=False)

    iso_df = ilp_results[iso_mask].copy()
    if not iso_df.empty:
        iso_df["slot"] = (iso_df["start_time"] // iso_slot_size).astype(int)
        for slot, group in iso_df.groupby("slot"):
            group_sorted = group.sort_values(by="start_time").reset_index(drop=True)
            current_time = slot * iso_slot_size
            slot_end_time = (slot + 1) * iso_slot_size
            for _, row in group_sorted.iterrows():
                start_refined = current_time
                end_refined = start_refined + row["duration"]
                if end_refined <= slot_end_time + 1e-9:
                    _append_refined_row(refined_results, row, start_refined, end_refined, slot, iso_slot_size, slot_end_time)
                    current_time = end_refined + gap
                else:
                    _append_failed_row(failed_results, row, row["start_time"], slot, iso_slot_size, slot_end_time, current_time, "等时流")

    cli_df = ilp_results[cli_mask].copy()
    if not cli_df.empty:
        cli_df["slot"] = (cli_df["start_time"] // cli_slot_size).astype(int)
        for slot, group in cli_df.groupby("slot"):
            group_sorted = group.sort_values(by="start_time").reset_index(drop=True)
            current_time = slot * cli_slot_size
            slot_end_time = (slot + 1) * cli_slot_size
            for _, row in group_sorted.iterrows():
                start_refined = current_time
                end_refined = start_refined + row["duration"]
                if end_refined <= slot_end_time + 1e-9:
                    _append_refined_row(refined_results, row, start_refined, end_refined, slot, cli_slot_size, slot_end_time)
                    current_time = end_refined + gap
                else:
                    _append_failed_row(failed_results, row, row["start_time"], slot, cli_slot_size, slot_end_time, current_time, "循环流")

    return pd.DataFrame(refined_results), pd.DataFrame(failed_results)
