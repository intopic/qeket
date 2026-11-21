import unittest
from unittest.mock import patch

import live_capture


class Stop(Exception):
    pass


class WatchTest(unittest.TestCase):
    def test_cursor_waits_for_a_node_that_indexed_the_tip(self):
        """A load-balanced RPC may answer from a node behind the tip. Its events must not be skipped."""
        calls = []
        logs = {"meta": {"logMessages": []}}

        def rpc(method, *params):
            calls.append((method, params))
            if method == "getSlot":
                return 120
            if method == "getSignaturesForAddress":
                self.assertEqual(params[1]["minContextSlot"], 120)
                if sum(1 for m, _ in calls if m == "getSignaturesForAddress") == 1:
                    raise RuntimeError("getSignaturesForAddress: -32016 Minimum context slot has not been reached")
                return [{"slot": 115, "signature": "sig", "err": None}]
            if method == "getTransaction":
                self.assertEqual(params[1]["minContextSlot"], 120)
                return logs
            raise AssertionError(method)

        checkpoints, waits = [], []

        def checkpoint(cursor):
            checkpoints.append(cursor)
            raise Stop

        with patch("live_capture.rpc", side_effect=rpc), self.assertRaises(Stop):
            live_capture.watch(100, checkpoint=checkpoint, wait=waits.append)
        self.assertEqual(waits, [1])
        self.assertEqual(checkpoints, [121])
        self.assertEqual([m for m, _ in calls].count("getTransaction"), 1)

    def test_other_rpc_failures_still_raise(self):
        def rpc(method, *params):
            if method == "getSlot":
                return 120
            raise RuntimeError("getSignaturesForAddress: -32602 invalid params")

        with patch("live_capture.rpc", side_effect=rpc), self.assertRaisesRegex(RuntimeError, "-32602"):
            live_capture.watch(100, wait=lambda _: None)


if __name__ == "__main__":
    unittest.main()
