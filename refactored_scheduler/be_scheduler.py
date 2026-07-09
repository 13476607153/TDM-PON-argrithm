"""基于 TS 调度结果的 BE 流补调度。

先在每个 DBA 周期内扣除已经分配给 TS 的时间窗，再按到达时间顺序
把 BE 包放入剩余空闲区间。
"""

import numpy as np


def schedule_be_flows_based_on_ts(be_arrival_times, be_packet_sizes, be_onu_ids,
                                  ts_schedule, supercycle_size, t_dba, tg=1.0):
    """在 TS 调度结果的空隙中安排 BE 包。

    返回列表长度与输入 BE 包数量一致；元素为调度成功的起止时间和 ONU，
    未找到可用空隙时对应元素保持为 None。
    """
    num_dba = int(supercycle_size // t_dba)
    free_intervals = [[] for _ in range(num_dba)]
    for i in range(num_dba):
        free_intervals[i].append([i * t_dba, (i + 1) * t_dba])

    for tw in ts_schedule:
        start, end = tw["start"], tw["start"] + tw["size"]
        s_idx = int(start // t_dba)
        if 0 <= s_idx < num_dba:
            new_intervals = []
            for i_start, i_end in free_intervals[s_idx]:
                if end + tg <= i_start or start >= i_end:
                    new_intervals.append([i_start, i_end])
                else:
                    if i_start + tg < start:
                        new_intervals.append([i_start, start - tg])
                    if end + tg < i_end:
                        new_intervals.append([end + tg, i_end])
            free_intervals[s_idx] = new_intervals

    order = np.argsort(be_arrival_times)
    be_schedule = [None] * len(be_arrival_times)

    for idx in order:
        arrival = be_arrival_times[idx]
        duration = be_packet_sizes[idx]
        onu_id = int(be_onu_ids[idx])
        scheduled = False

        start_dba = max(0, int(arrival // t_dba))
        for s_idx in range(start_dba, num_dba):
            j = 0
            while j < len(free_intervals[s_idx]):
                f_start, f_end = free_intervals[s_idx][j]
                t = max(arrival, f_start)
                if t + duration + tg <= f_end:
                    be_schedule[idx] = {"start": t, "end": t + duration, "onu_id": onu_id}
                    left, right = f_start, f_end
                    free_intervals[s_idx].pop(j)
                    if left < t:
                        free_intervals[s_idx].insert(j, [left, t])
                        j += 1
                    if t + duration + tg < right:
                        free_intervals[s_idx].insert(j, [t + duration + tg, right])
                    scheduled = True
                    break
                j += 1
            if scheduled:
                break

    return be_schedule
