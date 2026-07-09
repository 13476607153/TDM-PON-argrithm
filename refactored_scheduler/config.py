"""全局配置与派生参数。

该模块集中保存仿真参数、流类型参数和由参数推导出的数量。
约定：时间单位为微秒(us)，链路速率单位为 bit/s，包大小单位为 byte。
"""

import math
from math import lcm

import matplotlib

# 设置 Matplotlib 中文字体，避免绘图标题和坐标轴中文乱码。
matplotlib.rcParams["font.sans-serif"] = ["SimHei"]
matplotlib.rcParams["axes.unicode_minus"] = False

LINK_RATE = 50e9
USE_SHAPING = True

MAX_B = 50
OLT_ONU_DISTANCE_KM = 7
propagation_delay = 5 * OLT_ONU_DISTANCE_KM
processing_delay = 1
synchronization_error = 0.4
time_slot_size = 1
R = 49.7664e9
TG = 1
NUM_ONU = 8

# 负载参数：traffic_load 为总负载。四类 TS 流各占总数 1/8，BE 流占剩余 1/2。
traffic_load = 0.1
TS_TYPE_RATIO = 1 / 8

# 固定随机种子，保证 TS 起始时刻和 BE 到达过程可复现。
TS_RANDOM_SEED = 20260709
BE_RANDOM_SEED = 123

flow_params = {
    "Iso_Type1": {"cycle": 600, "delay": 500, "jitter": 1, "size": 200},
    "Iso_Type2": {"cycle": 1200, "delay": 1000, "jitter": 1, "size": 400},
    "Cli_Type1": {"cycle": 2400, "delay": 1000, "jitter": 1000, "size": 800},
    "Cli_Type2": {"cycle": 4800, "delay": 2000, "jitter": 1500, "size": 1600},
}

TS_FLOW_TYPES = ("Iso_Type1", "Iso_Type2", "Cli_Type1", "Cli_Type2")

total_expr = (R * traffic_load) / 2.67e6
total_flows = math.ceil(total_expr)

flow_counts = {flow_type: math.ceil(total_flows * TS_TYPE_RATIO) for flow_type in TS_FLOW_TYPES}
num_be_packets = total_flows - sum(flow_counts.values())

num_Iso_Type1 = flow_counts["Iso_Type1"]
num_Iso_Type2 = flow_counts["Iso_Type2"]
num_Cli_Type1 = flow_counts["Cli_Type1"]
num_Cli_Type2 = flow_counts["Cli_Type2"]
num_Iso = num_Iso_Type1 + num_Iso_Type2
num_Cli = num_Cli_Type1 + num_Cli_Type2

supercycle_size = lcm(*(flow_params[flow_type]["cycle"] for flow_type in TS_FLOW_TYPES))


def compute_packet_count(cycle):
    """返回单条周期流在一个超周期内产生的数据包数量。"""
    return supercycle_size // cycle


packets_per_supercycle = {
    flow_type: compute_packet_count(flow_params[flow_type]["cycle"])
    for flow_type in TS_FLOW_TYPES
}

# 同周期合并时，一条聚合流在一个基本时隙内允许承载的最大字节数。
MAX_MERGE_SIZE = int(time_slot_size * LINK_RATE / 8 / 1e6)
