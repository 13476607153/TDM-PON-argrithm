"""程序入口。

运行方式：在 outputs 目录下执行 `python -m refactored_scheduler.main`。
主流程依次生成 TS 流、执行同周期合并、求解 ILP、二次调度、补调度 BE、
输出 Excel 结果并绘制延迟分析图。
"""

import sys
import time

import pandas as pd

from .config import (
    BE_RANDOM_SEED, DBA_PERIOD_US, NUM_ONU, TS_RANDOM_SEED, USE_SHAPING,
    flow_counts, flow_params, num_be_packets, processing_delay, propagation_delay,
    supercycle_size, synchronization_error, target_payload_packets,
    total_flows, traffic_load,
)
from .slot_calendar import refine_ts_schedule, schedule_be_packets
from .statistics import (
    _get_weights, build_flow_type_success_stats, build_weighted_delay_stats,
    calculate_jitter_statistics, compute_statistics,
)
from .traffic import generate_be_traffic, generate_ts_flows, same_cycle_merge


def main():
    """执行完整仿真流程。"""
    start_time = time.time()
    print(f"[INFO] 程序开始执行时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(start_time))}")

    # 生成四种 TS 原始流；expected_counts 作为后续成功率的固定统计分母。
    flow_list = generate_ts_flows(flow_counts, flow_params, num_onu=NUM_ONU, seed=TS_RANDOM_SEED)
    expected_counts = {**flow_counts, "BE_Type": num_be_packets}
    flow_generation_rows = []
    for flow_type, count in flow_counts.items():
        packet_instances = count * (supercycle_size // flow_params[flow_type]["cycle"])
        flow_generation_rows.append({
            "rho": traffic_load,
            "流类型": flow_type,
            "生成数量": count,
            "计数单位": "TS流",
            "超周期包实例数": packet_instances,
            "payload槽数": packet_instances,
            "payload占比": packet_instances / supercycle_size,
        })
    flow_generation_rows.append({
        "rho": traffic_load,
        "流类型": "BE_Type",
        "生成数量": num_be_packets,
        "计数单位": "BE包",
        "超周期包实例数": num_be_packets,
        "payload槽数": num_be_packets,
        "payload占比": num_be_packets / supercycle_size,
    })
    flow_generation_df = pd.DataFrame(flow_generation_rows)

    print(f"目标payload包数 = {target_payload_packets} (rho={traffic_load:.1f})")
    print(f"生成对象数 = {total_flows} (TS按流、BE按包)")
    print(f"TS随机种子 = {TS_RANDOM_SEED}, BE随机种子 = {BE_RANDOM_SEED}")
    for flow_type, count in flow_counts.items():
        print(f"{flow_type} 生成数量 = {count}")
    print(f"BE_Type 生成数量 = {num_be_packets}")

    # 聚合函数识别 isochronous/cyclic 大类；_orig_flow_type 保留细分类名称。
    merge_input = []
    for f in flow_list:
        ft = f["flow_type"]
        normalized = "isochronous" if "Iso" in ft else "cyclic"
        merge_input.append({**f, "flow_type": normalized, "_orig_flow_type": ft})

    shaped_flows_list = same_cycle_merge(merge_input)
    print(f"原始流数量          = {len(flow_list)}")
    print(f"同周期合并后流数量  = {len(shaped_flows_list)}")
    if shaped_flows_list:
        print(f"流量压缩比          = {len(flow_list) / len(shaped_flows_list):.2f}x")

    # USE_SHAPING 只改变优化器输入实体，不改变生成数量和统计分母。
    if USE_SHAPING:
        scheduling_flow_list = []
        for sf_id, sf in enumerate(shaped_flows_list):
            scheduling_flow_list.append({
                "flow_id": sf_id,
                "onu_id": sf["onu_id"],
                "cycle": sf["cycle"],
                "delay": sf["delay"],
                "jitter": sf["jitter"],
                "start_time": sf["start_time"],
                "size": sf["size"],
                "flow_type": sf["flow_type"],
                "num_flows": sf.get("num_flows", 1),
                "flow_ids": sf.get("flow_ids", [sf_id]),
            })
        print(f"使用同周期合并后流进行调度: {len(scheduling_flow_list)} 条聚合流")
    else:
        scheduling_flow_list = []
        for f in flow_list:
            scheduling_flow_list.append({
                **f,
                "num_flows": 1,
                "flow_ids": [f["flow_id"]],
            })
        print(f"使用原始流进行调度: {len(scheduling_flow_list)} 条原始流")

    # ILP 负责整流接纳与 DBA 粗分配，不决定精确开始 slot。
    from .ts_optimizer import optimize_schedule

    result, _accepted_count, delay_dict = optimize_schedule(
        flow_list=scheduling_flow_list,
        supercycle_size=supercycle_size,
        t_dba=DBA_PERIOD_US,
        tse=synchronization_error,
        tprop=propagation_delay,
        tproc=processing_delay,
    )

    if not result:
        print("[ERROR] 没有成功调度的 TS 流，程序结束。")
        sys.exit(1)

    ilp_results_df = pd.DataFrame(result)
    # 在共享 1 us 日历中事务式放置 TS；失败流会完整回滚。
    refined_df, failed_df, link_calendar = refine_ts_schedule(
        ilp_results_df, return_calendar=True
    )

    print("\n[OK] 二次调度后的 TS 流调度表（成功，聚合单元口径）：")
    print(refined_df if not refined_df.empty else "无成功调度的包")

    print("\n[ERROR] 二次调度失败的 TS 流（聚合单元口径）：")
    if not failed_df.empty:
        print(failed_df)
        print("\n调度失败原因汇总:")
        for reason, reason_count in failed_df["failure_reason"].value_counts().items():
            print(f"  - {reason}: {reason_count} 个聚合单元")
    else:
        print("无调度失败的包")

    # BE 复用已包含 TS payload 与 guard 占用的全局日历。
    be_lambda = 0.005
    be_times, be_sizes, be_onu_ids = generate_be_traffic(be_lambda, num_be_packets, seed=BE_RANDOM_SEED)

    be_schedule = schedule_be_packets(
        arrival_times=be_times,
        packet_durations=be_sizes,
        onu_ids=be_onu_ids,
        calendar=link_calendar,
        supercycle_slots=supercycle_size,
        dba_period_slots=DBA_PERIOD_US,
    )

    # 把 TS/BE 的成功和失败记录统一映射到中文输出字段。
    all_schedule_results = []

    if not refined_df.empty:
        for _, row in refined_df.iterrows():
            num_flows = int(row.get("num_flows", 1))
            flow_ids = row.get("flow_ids", [row["flow_id"]])
            all_schedule_results.append({
                "流类型": row["flow_type"],
                "流ID": int(row["flow_id"]),
                "ONU_ID": int(row["onu_id"]),
                "到达时间": round(row["arrival_time"], 2),
                "调度开始时间": round(row["start_time_refined"], 2),
                "调度结束时间": round(row["end_time_refined"], 2),
                "传输时长": round(row["duration"], 6),
                "原始传输时长(us)": round(row["raw_tx_time_us"], 6),
                "包发送占用(us)": int(row["payload_slots"]),
                "隔离带占用(us)": int(row["guard_slots"]),
                "资源占用(us)": int(row["occupied_slots"]),
                "时隙": int(row["slot"]),
                "时隙大小(us)": float(row.get("slot_size", 1.0)),
                "时隙起点(us)": round(row.get("slot_start", int(row["slot"])), 2),
                "时隙终点(us)": round(row.get("slot_end", int(row["slot"]) + 1), 2),
                "调度状态": "成功",
                "cycle_n": int(row["cycle_n"]),
                "聚合原始流数量": num_flows,
                "原始流ID列表": str(flow_ids),
                "统计口径": "聚合流承载，统计按原始流数量加权" if num_flows > 1 else "原始流",
            })

    if not failed_df.empty:
        for _, row in failed_df.iterrows():
            num_flows = int(row.get("num_flows", 1))
            flow_ids = row.get("flow_ids", [row["flow_id"]])
            all_schedule_results.append({
                "流类型": row.get("flow_type", "Unknown"),
                "流ID": int(row["flow_id"]),
                "ONU_ID": int(row["onu_id"]),
                "到达时间": round(row["arrival_time"], 2),
                "调度开始时间": "失败",
                "调度结束时间": "失败",
                "传输时长": round(row["duration"], 6),
                "原始传输时长(us)": round(row["raw_tx_time_us"], 6),
                "包发送占用(us)": int(row["payload_slots"]),
                "隔离带占用(us)": int(row["guard_slots"]),
                "资源占用(us)": int(row["occupied_slots"]),
                "时隙": row["slot"],
                "时隙大小(us)": float(row.get("slot_size", 1.0)),
                "时隙起点(us)": round(row.get("slot_start", 0), 2),
                "时隙终点(us)": round(row.get("slot_end", 0), 2),
                "调度状态": "失败",
                "失败原因": row["failure_reason"],
                "cycle_n": int(row["cycle_n"]),
                "聚合原始流数量": num_flows,
                "原始流ID列表": str(flow_ids),
                "统计口径": "聚合流承载，统计按原始流数量加权" if num_flows > 1 else "原始流",
            })

    be_flow_id = len(flow_list)
    for i, (be_time, be_size, be_onu) in enumerate(zip(be_times, be_sizes, be_onu_ids)):
        be_tw = be_schedule[i]
        if be_tw is not None:
            all_schedule_results.append({
                "流类型": "BE_Type",
                "流ID": be_flow_id + i,
                "ONU_ID": int(be_tw["onu_id"]),
                "到达时间": round(be_time, 2),
                "调度开始时间": round(be_tw["start"], 2),
                "调度结束时间": round(be_tw["end"], 2),
                "传输时长": round(be_tw["end"] - be_tw["start"], 6),
                "原始传输时长(us)": round(be_tw["raw_tx_time_us"], 6),
                "包发送占用(us)": int(be_tw["payload_slots"]),
                "隔离带占用(us)": int(be_tw["guard_slots"]),
                "资源占用(us)": int(be_tw["occupied_slots"]),
                "时隙": int(be_tw["start"] // 1),
                "时隙大小(us)": 1.0,
                "时隙起点(us)": int(be_tw["start"] // 1),
                "时隙终点(us)": int(be_tw["start"] // 1) + 1,
                "调度状态": "成功",
                "聚合原始流数量": 1,
                "原始流ID列表": str([be_flow_id + i]),
                "统计口径": "BE原始包",
            })
        else:
            all_schedule_results.append({
                "流类型": "BE_Type",
                "流ID": be_flow_id + i,
                "ONU_ID": int(be_onu),
                "到达时间": round(be_time, 2),
                "调度开始时间": "失败",
                "调度结束时间": "失败",
                "传输时长": round(be_size, 6),
                "原始传输时长(us)": round(be_size, 6),
                "包发送占用(us)": 1,
                "隔离带占用(us)": 1,
                "资源占用(us)": 2,
                "时隙": "未分配",
                "时隙大小(us)": 1.0,
                "调度状态": "失败",
                "失败原因": "无可用时间窗",
                "聚合原始流数量": 1,
                "原始流ID列表": str([be_flow_id + i]),
                "统计口径": "BE原始包",
            })

    final_schedule_df = pd.DataFrame(all_schedule_results)
    # “失败”等文本转换为 NaN，并在排序时自然放到成功记录之后。
    final_schedule_df["调度开始时间_num"] = pd.to_numeric(final_schedule_df["调度开始时间"], errors="coerce")
    # 延迟(us)是排队延迟；完成时延(us)还包含 payload 发送占用。
    final_schedule_df["延迟(us)"] = final_schedule_df["调度开始时间_num"] - final_schedule_df["到达时间"]
    final_schedule_df["完成时延(us)"] = pd.to_numeric(
        final_schedule_df["调度结束时间"], errors="coerce"
    ) - final_schedule_df["到达时间"]
    final_schedule_df = final_schedule_df.sort_values(
        by=["调度开始时间_num", "ONU_ID", "流ID"],
        na_position="last",
    ).reset_index(drop=True)

    stats_df = build_weighted_delay_stats(final_schedule_df)
    flow_type_stats_df = build_flow_type_success_stats(final_schedule_df, expected_counts=expected_counts)
    print("\n[INFO] 延迟/抖动统计（原始流加权口径）：")
    print(stats_df)
    print("\n[INFO] 细分类成功率统计：")
    print(flow_type_stats_df)

    sched_stats = compute_statistics(final_schedule_df, supercycle_size, flow_params=flow_params)

    excel_filename = "完整调度结果_含聚合映射_原始流加权统计.xlsx"
    # 同名工作簿若正被 Excel/WPS 打开，Windows 会在此处触发 PermissionError。
    with pd.ExcelWriter(excel_filename, engine="openpyxl") as writer:
        final_schedule_df.to_excel(writer, index=False, sheet_name="完整调度结果")
        ilp_results_df.to_excel(writer, index=False, sheet_name="ILP聚合输入结果")
        if not refined_df.empty:
            refined_df.to_excel(writer, index=False, sheet_name="TS聚合单元成功")
        if not failed_df.empty:
            failed_df.to_excel(writer, index=False, sheet_name="TS聚合单元失败")
        stats_df.to_excel(writer, index=False, sheet_name="延迟统计_原始流加权")
        flow_type_stats_df.to_excel(writer, index=False, sheet_name="细分类成功率统计")
        flow_generation_df.to_excel(writer, index=False, sheet_name="流量生成参数")

        sched_stats_rows = []
        for ftype in ["Iso", "Cli"]:
            if ftype in sched_stats:
                sched_stats_rows.append({"流类型": ftype, **sched_stats[ftype]})
        if sched_stats_rows:
            pd.DataFrame(sched_stats_rows).to_excel(writer, index=False, sheet_name="成功率_RUE_DJSR")

        util_info = sched_stats.get("资源利用率", {})
        if util_info:
            pd.DataFrame([util_info]).to_excel(writer, index=False, sheet_name="资源利用率")

    print(f"\n[INFO] 完整调度结果已保存到 Excel 文件: {excel_filename}")

    ts_df = final_schedule_df[final_schedule_df["流类型"].str.contains("Iso|Cli", na=False)]
    ts_succ_df = ts_df[ts_df["调度状态"] == "成功"]
    ts_fail_df = ts_df[ts_df["调度状态"] == "失败"]
    success_ts_agg = len(ts_succ_df)
    failed_ts_agg = len(ts_fail_df)
    # 先从实例级折叠到流级，再应用聚合权重，避免周期实例被重复计数。
    ts_flow_level = ts_df.groupby("流ID", as_index=False).agg({
        "聚合原始流数量": "first",
        "调度状态": lambda values: "成功" if (values == "成功").all() else "失败",
    })
    ts_success_flows = ts_flow_level[ts_flow_level["调度状态"] == "成功"]
    ts_failed_flows = ts_flow_level[ts_flow_level["调度状态"] == "失败"]
    success_ts_weighted = int(_get_weights(ts_success_flows).sum()) if not ts_success_flows.empty else 0
    failed_ts_weighted = sum(flow_counts.values()) - success_ts_weighted

    be_df = final_schedule_df[final_schedule_df["流类型"] == "BE_Type"]
    success_be = len(be_df[be_df["调度状态"] == "成功"])
    failed_be = len(be_df[be_df["调度状态"] == "失败"])

    print("\n[INFO] 调度数量汇总：")
    print(f"   - TS聚合单元成功数量: {success_ts_agg}")
    print(f"   - TS聚合单元失败数量: {failed_ts_agg}")
    print(f"   - TS原始流加权成功数量: {success_ts_weighted}")
    print(f"   - TS原始流加权失败数量: {failed_ts_weighted}")
    print(f"   - BE流调度成功数量: {success_be}")
    print(f"   - BE流调度失败数量: {failed_be}")
    if success_ts_weighted + failed_ts_weighted > 0:
        ts_success_rate = success_ts_weighted / (success_ts_weighted + failed_ts_weighted) * 100
        print(f"   - TS原始流加权成功率: {ts_success_rate:.2f}%")

    jitter_stats, worst_flows = calculate_jitter_statistics(refined_df)
    print("\n二次调度后各类流抖动最大值（聚合流口径）：")
    for ftype, res in worst_flows.items():
        print(
            f"{ftype} -> 聚合流 {res['流量ID']} 抖动 = {res['最大抖动']:.2f} us，"
            f"代表原始流数量 = {res['聚合原始流数量']}"
        )

    # 绘图库仅在确实生成图像时加载，避免配置和核心调度依赖可视化环境。
    from .plotting import plot_violin_with_failure_analysis

    plot_violin_with_failure_analysis(
        delay_dict, be_schedule, be_times, refined_df, failed_df, jitter_stats
    )

    end_time = time.time()
    execution_time = end_time - start_time
    print(f"\n[INFO] 程序执行完成时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(end_time))}")
    print(f"[INFO] 总执行时间: {execution_time:.2f} 秒")
    print("\n[OK] 程序执行完成，系统自动停止")
    return 0


if __name__ == "__main__":
    main()
