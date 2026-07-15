"""调度结果可视化。

绘制 Iso、Cli、BE 的延迟分布、各时隙成功调度数量，以及 TS 聚合单元成功/失败比例。
"""

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

plt.rcParams["font.sans-serif"] = ["SimHei"]
plt.rcParams["axes.unicode_minus"] = False

try:
    import seaborn as sns
    HAS_SEABORN = True
except Exception:
    HAS_SEABORN = False


def plot_violin_with_failure_analysis(delay_dict, be_schedule, be_times, refined_df=None, failed_df=None,
                                      jitter_stats=None, save_path="延迟分布分析.png"):
    """绘制延迟分布、时隙负载和调度成功率分析图。"""
    delays = []
    types = []

    for k, vals in delay_dict.items():
        for v in vals:
            delays.append(v)
            types.append(k)

    for i, tw in enumerate(be_schedule):
        if tw is not None:
            delays.append(tw["start"] - be_times[i])
            types.append("BE")

    df = pd.DataFrame({"flow_type": types, "delay": delays})
    fig, (ax1, ax2, ax3) = plt.subplots(1, 3, figsize=(18, 6))

    if not df.empty:
        if HAS_SEABORN:
            sns.violinplot(x="flow_type", y="delay", data=df, inner="box", density_norm="width", ax=ax1)
        else:
            labels = list(df["flow_type"].unique())
            grouped = [df[df["flow_type"] == t]["delay"].values for t in labels]
            ax1.violinplot(grouped, showmeans=True)
            ax1.set_xticks(range(1, len(labels) + 1))
            ax1.set_xticklabels(labels)
        ax1.set_title("三类流量延迟分布")
        ax1.set_xlabel("流量类型")
        ax1.set_ylabel("延迟 (us)")
        ax1.grid(True, axis="y", linestyle="--", alpha=0.6)

    if refined_df is not None and not refined_df.empty:
        slot_load = refined_df.groupby("slot_start").size()
        ax2.bar(slot_load.index, slot_load.values, alpha=0.7)
        ax2.set_title("各时间桶成功调度包数量")
        ax2.set_xlabel("时间桶起点 (us)")
        ax2.set_ylabel("调度包数量")
        ax2.grid(True, axis="y", linestyle="--", alpha=0.6)

    if refined_df is not None and failed_df is not None:
        success_count = len(refined_df) if not refined_df.empty else 0
        failure_count = len(failed_df) if not failed_df.empty else 0
        total_count = success_count + failure_count
        if total_count > 0:
            ax3.pie([success_count, failure_count], labels=["调度成功", "调度失败"], autopct="%1.1f%%", startangle=90)
            ax3.set_title(f"TS聚合调度单元成功率\n(总数: {total_count})")

    plt.tight_layout()
    plt.savefig(save_path, dpi=300)
    plt.close(fig)

    print("\n[INFO] 调度统计信息:")
    if refined_df is not None:
        print(f"TS聚合调度单元成功数量: {len(refined_df)}")
    if failed_df is not None:
        print(f"TS聚合调度单元失败数量: {len(failed_df)}")
    successful_be = sum(1 for tw in be_schedule if tw is not None)
    print(f"BE流调度成功数量: {successful_be}/{len(be_schedule)}")
    if delays:
        print(f"平均延迟: {np.mean(delays):.2f} us")
        print(f"最大延迟: {np.max(delays):.2f} us")

    if jitter_stats:
        print("\n[INFO] 抖动统计信息（聚合流口径）:")
        for flow_type, jitters in jitter_stats.items():
            if jitters:
                print(
                    f"{flow_type}流抖动 - 平均: {np.mean(jitters):.2f} us, "
                    f"最大: {np.max(jitters):.2f} us, 最小: {np.min(jitters):.2f} us"
                )
            else:
                print(f"{flow_type}流抖动: 无数据")
