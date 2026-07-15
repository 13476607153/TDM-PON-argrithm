"""全局配置与派生参数。

该模块集中保存仿真参数、流类型参数和由参数推导出的数量。
约定：时间单位为微秒(us)，链路速率单位为 bit/s，包大小单位为 byte。
"""

from math import lcm

# LINK_RATE 用于估算“一个 1 us 时隙最多可以聚合多少字节”；R 则表示
# 扣除线路开销后的有效传输速率，用于真实发送时长计算。
# 两者含义不同，修改链路模型时不要只改其中一个。
LINK_RATE = 50e9

# True：循环流先按 ONU、周期聚合后参与调度；False：所有原始流直接参与调度。
USE_SHAPING = True

# 以下参数统一采用 us（微秒）作为时间单位。
OLT_ONU_DISTANCE_KM = 7
propagation_delay = 5 * OLT_ONU_DISTANCE_KM
processing_delay = 1
synchronization_error = 0.4
SLOT_SIZE_US = 1
MIN_PAYLOAD_SLOTS = 1
GUARD_SLOTS = 1
DBA_PERIOD_US = 300
R = 49.7664e9
NUM_ONU = 8

# 负载参数：traffic_load 仅表示超周期内的目标 payload-slot 比例。
# guard 由调度过程产生，不参与流量生成和 rho 的计算。
traffic_load = 0.9

# 固定随机种子，保证 TS 起始时刻和 BE 到达过程可复现。
TS_RANDOM_SEED = 20260709
BE_RANDOM_SEED = 123

TS_PACKET_SIZE_BYTES = 6220
flow_params = {
    "Iso_Type1": {"cycle": 600, "delay": 500, "jitter": 1, "size": TS_PACKET_SIZE_BYTES},
    "Iso_Type2": {"cycle": 1200, "delay": 1000, "jitter": 1, "size": TS_PACKET_SIZE_BYTES},
    "Cli_Type1": {"cycle": 2400, "delay": 1000, "jitter": 1000, "size": TS_PACKET_SIZE_BYTES},
    "Cli_Type2": {"cycle": 4800, "delay": 2000, "jitter": 1500, "size": TS_PACKET_SIZE_BYTES},
}

TS_FLOW_TYPES = ("Iso_Type1", "Iso_Type2", "Cli_Type1", "Cli_Type2")

# 与四种对比算法使用同一流量生成口径。
supercycle_size = lcm(*(
    flow_params[flow_type]["cycle"] for flow_type in TS_FLOW_TYPES
))
packets_per_supercycle = {
    flow_type: supercycle_size // flow_params[flow_type]["cycle"]
    for flow_type in TS_FLOW_TYPES
}
packets_per_equal_ts_group = sum(packets_per_supercycle.values())  # 8+4+2+1=15


def calculate_flow_counts(rho):
    """按 payload-only rho 返回四类等流数 TS、BE 包数和生成对象总数。"""
    target_packets = int(round(float(rho) * float(supercycle_size)))
    ts_packet_budget = target_packets // 2
    count_per_type = ts_packet_budget // packets_per_equal_ts_group
    counts = {flow_type: count_per_type for flow_type in TS_FLOW_TYPES}
    ts_packets = count_per_type * packets_per_equal_ts_group
    num_be = target_packets - ts_packets
    return counts, num_be, sum(counts.values()) + num_be


target_payload_packets = int(round(traffic_load * supercycle_size))
flow_counts, num_be_packets, total_flows = calculate_flow_counts(traffic_load)

# 同周期合并时，一条聚合流在一个基本时隙内允许承载的最大字节数。
MAX_MERGE_SIZE = int(SLOT_SIZE_US * LINK_RATE / 8 / 1e6)
