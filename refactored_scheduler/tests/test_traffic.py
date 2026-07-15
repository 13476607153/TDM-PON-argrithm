"""Proposed 与四种对比算法共用流量口径的一致性测试。"""

import unittest

import numpy as np

from ..config import (
    BE_RANDOM_SEED, NUM_ONU, R, TS_FLOW_TYPES, TS_PACKET_SIZE_BYTES,
    calculate_flow_counts, flow_params, supercycle_size,
)
from ..traffic import generate_be_traffic, generate_ts_flows


class TrafficGenerationConsistencyTest(unittest.TestCase):
    def test_ts_packet_occupies_one_payload_slot(self):
        raw_tx_time_us = TS_PACKET_SIZE_BYTES * 8.0 / R * 1e6
        self.assertLessEqual(raw_tx_time_us, 1.0)
        self.assertTrue(all(
            flow_params[name]["size"] == TS_PACKET_SIZE_BYTES
            for name in TS_FLOW_TYPES
        ))

    def test_all_load_points_use_equal_ts_flow_counts(self):
        for step in range(1, 10):
            rho = step / 10
            with self.subTest(rho=rho):
                counts, num_be, _ = calculate_flow_counts(rho)
                self.assertEqual(len(set(counts.values())), 1)
                ts_packets = sum(
                    counts[name] * (supercycle_size // flow_params[name]["cycle"])
                    for name in TS_FLOW_TYPES
                )
                target_packets = round(rho * supercycle_size)
                self.assertEqual(ts_packets + num_be, target_packets)
                self.assertEqual(ts_packets, target_packets // 2)

    def test_fixed_seed_be_generation_matches_comparison_algorithms(self):
        count = 240
        first = generate_be_traffic(0.005, count, seed=BE_RANDOM_SEED)
        second = generate_be_traffic(999.0, count, seed=BE_RANDOM_SEED)
        for left, right in zip(first, second):
            np.testing.assert_array_equal(left, right)
        arrivals, durations, onus = first
        self.assertTrue(np.all(arrivals[:-1] <= arrivals[1:]))
        self.assertTrue(np.all((arrivals >= 0) & (arrivals < supercycle_size)))
        self.assertTrue(np.all(durations > 0))
        self.assertTrue(np.all((onus >= 0) & (onus < NUM_ONU)))

    def test_fixed_seed_ts_generation_is_reproducible(self):
        counts, _, _ = calculate_flow_counts(0.1)
        first = generate_ts_flows(counts, flow_params, seed=20260709)
        second = generate_ts_flows(counts, flow_params, seed=20260709)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
