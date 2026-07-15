"""1 μs 离散调度核心不变量测试。"""

import unittest

import numpy as np

from ..slot_calendar import LinkCalendar, schedule_be_packets, schedule_ts_units
from ..time_model import resource_slots


class Scheduler1usTests(unittest.TestCase):
    def test_minimum_payload_and_guard(self):
        result = resource_slots(200, 50e9)
        self.assertEqual(result["payload_slots"], 1)
        self.assertEqual(result["guard_slots"], 1)
        self.assertEqual(result["occupied_slots"], 2)

    def test_calendar_payload_guard_and_flow_jitter(self):
        resource = resource_slots(200, 50e9)
        common = {
            "onu_id": 0, "flow_type": "Iso_Type1", "jitter": 1,
            "delay_limit": 500, "num_flows": 1, "flow_ids": [1], **resource,
        }
        units = [
            {
                **common, "unit_id": n, "flow_id": 1, "cycle_n": n,
                "arrival_time": arrival, "release_slot": arrival + 1,
                "latest_start_slot": arrival + 5,
                "service_deadline_slot": arrival + 6, "dba_period": 0,
            }
            for n, arrival in enumerate((10, 20))
        ]
        success, failed, calendar = schedule_ts_units(units, 300, 300)
        self.assertEqual(len(success), 2)
        self.assertTrue(failed.empty)
        self.assertEqual(calendar.occupied_count(), 4)
        self.assertLessEqual(success["actual_flow_jitter_us"].max(), 1)

    def test_be_uses_same_calendar(self):
        calendar = LinkCalendar(10)
        calendar.reserve(
            {"flow_id": 1, "cycle_n": 0, "payload_slots": 1, "occupied_slots": 2},
            0,
        )
        result = schedule_be_packets(
            np.array([0.0]), np.array([0.1]), np.array([2]),
            calendar=calendar, supercycle_slots=10, dba_period_slots=10,
        )
        self.assertEqual(result[0]["start"], 2.0)
        self.assertEqual(calendar.slots[2][0], "PAYLOAD")
        self.assertEqual(calendar.slots[3][0], "GUARD")


if __name__ == "__main__":
    unittest.main()
