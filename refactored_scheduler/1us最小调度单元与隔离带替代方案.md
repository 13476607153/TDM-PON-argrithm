# 1us 最小调度单元与隔离带替代方案

## 1. 问题背景

当前程序里有两个和物理实现不一致的点：

1. ILP 阶段使用连续时间 `s[u]` 和实际传输时长 `duration`。
   - 例如 200 byte 在 50Gbps 下只有约 `0.032us`。
   - 这会让模型认为一个包可以占用远小于 `1us` 的时间。

2. 二次调度阶段使用 `gap=0.01us` 排序塞包。
   - `0.01us` 不是系统的最小调度单位。
   - 如果硬件最小调度粒度是 `1us`，那么 `0.01us` 的间隔没有物理意义。

新的物理假设是：

- `1us` 是最小调度单位。
- 一个包即使实际传输时间小于 `1us`，也默认占用 `1us`。
- 隔离带宽/保护间隔也设置为 `1us`。
- 相同 ONU 和不同 ONU 的包之间都需要这个 `1us` 隔离带。
- 因此全链路在任意 `1us` 时间格上只能有一种状态：
  - 发送某一个包；
  - 或处于某一个包后的隔离带；
  - 或空闲。

## 2. 建议的统一时间口径

把所有调度时间离散成整数 us。

```python
SLOT_SIZE_US = 1
GUARD_SLOTS = 1
MIN_PACKET_SLOTS = 1
```

每个调度单元计算三个时间量：

```python
raw_tx_time_us = size_byte * 8 / R * 1e6
payload_slots = max(MIN_PACKET_SLOTS, ceil(raw_tx_time_us / SLOT_SIZE_US))
guard_slots = GUARD_SLOTS
occupied_slots = payload_slots + guard_slots
```

在当前包大小下，TS 和 BE 包实际传输时间大多小于 `1us`，所以通常：

```text
payload_slots = 1
guard_slots = 1
occupied_slots = 2
```

含义是：

- 第 1 个 `1us` slot 用于包发送。
- 第 2 个 `1us` slot 用于隔离带。
- 下一包最早只能从 `start_slot + 2` 开始。

## 3. 不建议继续使用的做法

### 3.1 不建议使用 pairwise 隔离约束

如果对任意两个包 `i, j` 加非重叠/隔离约束：

```text
s_i + occupied_i <= s_j 或 s_j + occupied_j <= s_i
```

需要为每对包增加二元排序变量。

约束规模约为：

```text
O(U^2)
```

其中 `U` 是超周期内展开后的调度单元数量。

按当前四类 TS 参数估算，不合并前超周期内 TS 调度单元数量约为：

| 类型 | 流数 | 每流超周期包数 | 调度单元数 |
|---|---:|---:|---:|
| `Iso_Type1` | 233 | 8 | 1864 |
| `Iso_Type2` | 233 | 4 | 932 |
| `Cli_Type1` | 233 | 2 | 466 |
| `Cli_Type2` | 233 | 1 | 233 |
| 合计 | 932 | - | 3495 |

即使同周期合并能减少部分循环流，pairwise 约束仍然容易爆炸。

因此不建议走“任意两包加隔离约束”的路线。

### 3.2 不建议保留二次调度里的 `0.01us gap`

现有二次调度逻辑是：

```python
current_time = end_refined + gap
```

其中 `gap=0.01`。

如果最小调度单位已经确定为 `1us`，则这个 gap 应替换为整数 slot 日历，而不是继续在连续时间里做小数排序。

## 4. 推荐方案：ILP 粗分配 + 1us 全局日历放置

我建议采用“两层调度”：

1. ILP 只决定一个调度单元是否调度，以及分配到哪个 DBA 周期。
2. 后处理不再做 `0.01us` 二次排序，而是使用 `1us` 全局日历放置包和隔离带。

这个方案的核心思想是：

- ILP 不处理任意两包的先后关系，避免 pairwise 约束爆炸。
- ILP 只做容量、时间窗、成功率、负载均衡等粗粒度决策。
- 真实的 `1us` 包占用和 `1us` 隔离带由全局日历统一保证。

## 5. ILP 阶段建议改法

### 5.1 去掉连续开始时间变量

当前 ILP 中有：

```python
s[u]  # 连续开始时间
y[u][k]  # 离散候选开始时间
```

建议减少为：

```python
z[u][d] = 1 表示调度单元 u 被分配到 DBA 周期 d
x[u] = sum_d z[u][d]
```

也就是说，ILP 不再直接决定精确 `start_time`。

### 5.2 DBA 容量约束使用 occupied_slots

每个 DBA 周期容量按 `1us slot` 计算。

假设 `t_dba = 300us`，则每个 DBA 周期有：

```python
DBA_SLOTS = 300
```

容量约束改为：

```text
sum(occupied_slots[u] * z[u][d]) <= DBA_SLOTS
```

如果所有包都是 `1us payload + 1us guard`，则等价于：

```text
sum(2 * z[u][d]) <= 300
```

这表示一个 DBA 周期最多容纳 150 个包发送机会。

### 5.3 可选 DBA 周期由时间窗决定

对每个调度单元先计算整数时间窗：

```python
release_slot = ceil(arrival + synchronization_error)
latest_payload_end_slot = floor(ubd)
latest_start_slot = latest_payload_end_slot - payload_slots
```

如果隔离带也必须落在同一个 DBA 周期内，则还要满足：

```python
start_slot + occupied_slots <= dba_end_slot
```

如果隔离带允许跨 DBA 周期，只要求 payload 在 deadline 前结束，则 DBA 可选范围可以稍宽。

建议第一版采用更保守、更容易解释的口径：

```text
payload + guard 都必须落在同一个 DBA 周期内
```

这样后处理放置更稳，统计也更清晰。

### 5.4 抖动约束建议先降级为后处理检查

如果 ILP 不直接决定精确开始时间，那么严格 jitter 约束不适合继续放在 ILP 里。

原因是：

- 抖动依赖同一流多次发送的实际开始时间。
- 实际开始时间由后面的 1us 日历放置决定。
- 如果强行在 ILP 中预测精确 start，会重新引入大量时间索引变量。

建议第一版：

1. ILP 保留流级成功/失败变量。
2. 日历放置后计算真实 jitter。
3. 如果某条流真实 jitter 超限：
   - 方案 A：标记该流失败；
   - 方案 B：在日历放置时按流分组和周期顺序优先放置，尽量减少 jitter。

如果后续必须把 jitter 硬约束放入模型，再考虑更重的 time-indexed ILP。

## 6. 替代二次调度：1us 全局日历放置器

替代现有 `secondary_scheduling_with_overload_handling(...)`。

新函数可以命名为：

```python
slot_calendar_scheduling(...)
```

核心数据结构：

```python
calendar = [None] * supercycle_size
```

其中每个下标代表一个 `1us slot`。

每个 slot 状态可以是：

```text
None                  空闲
("payload", unit_id)  包发送占用
("guard", unit_id)    隔离带占用
```

放置算法：

1. 按 DBA 周期分组。
2. 每个 DBA 周期内，对 ILP 分配过来的包排序。
3. 对每个包，在允许窗口内寻找连续 `occupied_slots` 个空闲 slot。
4. 找到则写入：
   - 前 `payload_slots` 个 slot 标记为 payload；
   - 后 `guard_slots` 个 slot 标记为 guard。
5. 找不到则标记失败。

排序建议：

```text
Iso_Type1 / Iso_Type2 优先
然后 Cli_Type1 / Cli_Type2
同优先级内按 latest_start_slot 从早到晚
再按 arrival_slot 从早到晚
再按 flow_id、cycle_n 稳定排序
```

这个排序是启发式，不产生 ILP 约束。

## 7. 相同 ONU / 不同 ONU 隔离如何保证

用户要求：相同 ONU 和不同 ONU 的流量包都需要 `1us` 隔离带。

因此不能按 ONU 建多个独立日历。

应该使用一个全局链路日历：

```python
global_link_calendar[t]
```

只要某个包的 payload 或 guard 占用了 slot `t`，任何 ONU 的其他包都不能占用这个 slot。

这样天然同时保证：

- 同 ONU 包之间隔离；
- 不同 ONU 包之间隔离；
- TS 与 BE 包之间隔离。

## 8. BE 调度也应使用同一日历

BE 不应再只从连续空闲区间里扣除实际微秒级传输时长。

建议 BE 也使用同样规则：

```python
payload_slots = 1
guard_slots = 1
occupied_slots = 2
```

BE 调度顺序：

1. TS 日历放置完成。
2. BE 按到达时间排序。
3. 从 `ceil(be_arrival_time)` 开始寻找连续 `2` 个空闲 slot。
4. 放置成功则记录 start/end。
5. 找不到则失败。

这样 TS、BE 共用同一个隔离模型。

## 9. 统计口径建议调整

建议同时保存两个时长：

| 字段 | 含义 |
|---|---|
| `原始传输时长(us)` | 按 bit 数和链路速率算出的物理传输时间 |
| `包发送占用(us)` | 至少 `1us` 的 payload slot |
| `隔离带占用(us)` | 固定 `1us` |
| `资源占用(us)` | `包发送占用 + 隔离带占用` |

延迟建议仍使用：

```text
调度开始时间 - 到达时间
```

资源利用率建议分两种：

1. Payload 利用率：

```text
sum(包发送占用) / supercycle_size
```

2. 链路占用率：

```text
sum(资源占用) / supercycle_size
```

由于隔离带也消耗链路调度资源，所以后续 RUE 更建议使用“链路占用率”口径。

## 10. 约束数量对比

### 方案 A：pairwise 隔离约束

不推荐。

```text
变量/约束规模约 O(U^2)
```

优点：

- ILP 内严格保证隔离。

缺点：

- 约束爆炸；
- 求解很慢；
- 不适合当前上千级调度单元。

### 方案 B：1us time-indexed ILP

可选，但不是第一推荐。

变量：

```text
a[u][t] = 1 表示 u 在 slot t 开始
```

约束：

```text
对每个 slot τ：
sum(a[u][t] for all intervals covering τ) <= 1
```

优点：

- 没有 pairwise；
- 隔离严格；
- 精确 start 在 ILP 内确定。

缺点：

- 变量数量可能很大；
- 对每个包的候选 start slot 都要建变量；
- 如果窗口较宽，模型仍然会重。

### 方案 C：ILP 粗分配 + 1us 全局日历

第一推荐。

约束主要是：

```text
每个调度单元最多选择一个 DBA 周期
每个 DBA 周期总 occupied_slots 不超过容量
流级成功/失败变量
负载均衡目标
```

优点：

- 不加任意两包隔离约束；
- 去掉 `0.01us` 二次排序；
- 物理时间粒度清晰；
- 约束数量少；
- 后处理日历天然保证全局隔离。

缺点：

- ILP 不直接保证所有被选包一定能在 DBA 周期内按时间窗放下；
- 日历放置可能产生少量后处理失败；
- jitter 如果要硬保证，需要后续增强。

我建议先采用方案 C。

## 11. 推荐实施步骤

### 第一步：参数统一

新增：

```python
SLOT_SIZE_US = 1
MIN_PACKET_SLOTS = 1
GUARD_SLOTS = 1
```

每个调度单元增加：

```python
raw_tx_time
payload_slots
guard_slots
occupied_slots
release_slot
latest_start_slot
```

### 第二步：替换 ILP 中的时间口径

当前 ILP 中的 `duration` 继续保留为 `raw_tx_time`，但模型容量不再使用它。

DBA 容量改用：

```python
occupied_slots
```

### 第三步：移除二次调度 `gap=0.01`

不再使用：

```python
current_time = end_refined + 0.01
```

改为：

```python
start_slot = first_free_block(...)
end_payload_slot = start_slot + payload_slots
end_occupied_slot = start_slot + occupied_slots
```

### 第四步：BE 调度共用日历

BE 不再单独使用连续时间空闲区间算法，而是在 TS 日历基础上继续找 `2 slot` 空闲块。

### 第五步：统计表增加资源占用字段

Excel 建议增加列：

- `原始传输时长(us)`
- `包发送占用(us)`
- `隔离带占用(us)`
- `资源占用(us)`
- `slot_start`
- `payload_slot_end`
- `guard_slot_end`

## 12. 我建议先确认的关键点

在改代码前建议确认以下口径：

1. 每个包是否统一占用：

```text
1us payload + 1us guard = 2us 资源占用
```

2. 隔离带是否必须落在同一个 DBA 周期内？

我建议第一版要求落在同一个 DBA 周期内，这样容量统计最清楚。

3. jitter 是否先作为后处理检查，而不是第一版 ILP 硬约束？

我建议第一版先后处理检查，避免为了 jitter 重新引入大量 start-time 变量。

4. BE 是否也必须遵守同样的 `1us payload + 1us guard`？

我建议是，否则 TS 和 BE 共存时仍会出现口径不一致。

## 13. 当前推荐结论

推荐用“少约束”的路线：

```text
ILP 粗分配 DBA 周期
+ 每周期 occupied_slots 容量约束
+ 1us 全局日历后处理放置
+ 全局 calendar 保证同 ONU/不同 ONU/TS/BE 全部隔离
```

这能替代：

- pairwise 隔离约束；
- 当前二次调度中的 `0.01us gap`；
- 小于 `1us` 的不可实现传输占用。

同时可以把约束规模控制在接近当前模型，避免因为隔离约束导致求解爆炸。
