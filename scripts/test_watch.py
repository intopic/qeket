import base64
import struct
import unittest
from unittest.mock import patch

import live_capture
from live_capture import EVENT, GATE


class Stop(Exception):
    pass


def proposal_logs(proposal, program=GATE):
    raw = EVENT + bytes([proposal]) * 32 + bytes([1]) * 32 + struct.pack("<QQQ", 0, 90, 10)
    return [f"Program {program} invoke [1]", "Program data: " + base64.b64encode(raw).decode(),
            f"Program {program} success"]


def block_tx(signature, logs, err=None):
    return {"transaction": {"signatures": [signature]}, "meta": {"err": err, "logMessages": logs}}


# Slot 100 holds more successful gate transactions than gate_history reads.
BUSY_BLOCK = {"transactions": [
    block_tx("other", proposal_logs(9, program="11111111111111111111111111111111")),
    block_tx("first", proposal_logs(2)),
    block_tx("failed", proposal_logs(3), err={"InstructionError": [0, "InvalidArgument"]}),
    block_tx("second", proposal_logs(4)),
]}


class BusySlotTest(unittest.TestCase):
    def watch_busy_slot(self, blocks, tip=100):
        page = [{"slot": 100, "signature": f"s{i}", "err": None} for i in range(100)]
        reads, events, checkpoints, waits = [], [], [], []

        def rpc(method, *params):
            if method == "getSlot":
                return tip
            if method == "getSignaturesForAddress":
                return page
            if method == "getBlock":
                reads.append(params[0])
                result = blocks.pop(0)
                if isinstance(result, Exception):
                    raise result
                return result
            raise AssertionError(method)

        def checkpoint(cursor):
            checkpoints.append(cursor)
            if cursor > tip:
                raise Stop

        with patch("live_capture.rpc", side_effect=rpc), self.assertRaises(Stop):
            live_capture.watch(100, on_event=events.append, checkpoint=checkpoint, wait=waits.append)
        return reads, events, checkpoints, waits

    def assert_busy_events(self, events):
        self.assertEqual([(e["proposal"], e["signature"], e["slot"]) for e in events], [
            (live_capture.to_base58(bytes([2]) * 32), "first", 100),
            (live_capture.to_base58(bytes([4]) * 32), "second", 100),
        ])

    def test_busy_slot_is_read_from_its_block_in_order(self):
        reads, events, checkpoints, waits = self.watch_busy_slot([BUSY_BLOCK])
        self.assertEqual((reads, checkpoints, waits), ([100], [101], []))
        self.assert_busy_events(events)

    def test_busy_slot_block_read_is_retried_on_a_node_behind(self):
        """A node that has not stored the block must not stop the listener or skip the slot."""
        reads, events, checkpoints, waits = self.watch_busy_slot([
            None,
            RuntimeError("getBlock: -32004 Block not available for slot 100, logs=None"),
            RuntimeError("getBlock: -32016 Minimum context slot has not been reached, logs=None"),
            BUSY_BLOCK,
        ])
        self.assertEqual((reads, checkpoints, waits), ([100] * 4, [101], [1, 1, 1]))
        self.assert_busy_events(events)

    def test_busy_slot_other_block_failures_still_raise(self):
        def rpc(method, *params):
            if method == "getSlot":
                return 100
            if method == "getSignaturesForAddress":
                return [{"slot": 100, "signature": "s", "err": None}] * 100
            raise RuntimeError("getBlock: HTTP 400")

        with patch("live_capture.rpc", side_effect=rpc), self.assertRaisesRegex(RuntimeError, "HTTP 400"):
            live_capture.watch(100, wait=lambda _: None)

    def test_busy_range_is_split_down_to_the_busy_slot(self):
        reads, events, checkpoints, waits = self.watch_busy_slot([BUSY_BLOCK], tip=107)
        # 100..107 and its halves exceed the limit. Slot 100 alone is read from
        # its block, then the window grows again past the busy slot.
        self.assertEqual(reads, [100])
        self.assertEqual(checkpoints, [101, 103, 107, 108])
        self.assert_busy_events(events)


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

    def test_throttled_or_unreachable_endpoint_backs_off_without_stopping(self):
        """A restart would drop every captured pre-state, so an RPC outage only delays the cursor."""
        failures = [RuntimeError("getSlot: HTTP 429"), RuntimeError("getSlot: HTTP 503"),
                    ConnectionResetError("connection reset"), RuntimeError("getSlot: endpoint unavailable")]
        failures += [RuntimeError("getSlot: HTTP 429")] * 6

        def rpc(method, *params):
            if failures:
                raise failures.pop(0)
            if method == "getSlot":
                return 120
            if method == "getSignaturesForAddress":
                return []
            raise AssertionError(method)

        checkpoints, waits = [], []

        def checkpoint(cursor):
            checkpoints.append(cursor)
            raise Stop

        with patch("live_capture.rpc", side_effect=rpc), patch("builtins.print") as log, self.assertRaises(Stop):
            live_capture.watch(100, checkpoint=checkpoint, wait=waits.append)
        # Exponential backoff, capped. The range is read again from the same cursor.
        self.assertEqual(waits, [2, 4, 8, 16, 32, 60, 60, 60, 60, 60])
        self.assertEqual(checkpoints, [121])
        first = log.call_args_list[0].args[0]
        self.assertIn('"event": "rpc_unavailable"', first)
        self.assertIn("HTTP 429", first)

    def test_backoff_resets_after_a_complete_read(self):
        failures = [RuntimeError("getSlot: HTTP 429")]
        calls = {"getSlot": 0}

        def rpc(method, *params):
            if method == "getSlot":
                calls["getSlot"] += 1
                if calls["getSlot"] == 3:
                    failures.append(RuntimeError("getSlot: HTTP 429"))
                if failures:
                    raise failures.pop(0)
                return 100 + calls["getSlot"]
            return []

        checkpoints, waits = [], []

        def checkpoint(cursor):
            checkpoints.append(cursor)
            if len(checkpoints) == 2:
                raise Stop

        with patch("live_capture.rpc", side_effect=rpc), patch("builtins.print"), self.assertRaises(Stop):
            live_capture.watch(100, checkpoint=checkpoint, wait=waits.append)
        self.assertEqual(waits, [2, 2])


class CatchUpTest(unittest.TestCase):
    def test_long_gap_is_read_in_widening_ranges_without_skipping_slots(self):
        """A cursor left 50,000 slots behind by downtime catches up in a few reads, not one per 64 slots."""
        head = 100 + 50_000
        ranges = []

        def rpc(method, *params):
            if method == "getSlot":
                return head
            if method == "getSignaturesForAddress":
                ranges.append(params[1]["minContextSlot"])
                return []
            raise AssertionError(method)

        checkpoints = []

        def checkpoint(cursor):
            checkpoints.append(cursor)
            if cursor > head:
                raise Stop

        with patch("live_capture.rpc", side_effect=rpc), self.assertRaises(Stop):
            live_capture.watch(100, checkpoint=checkpoint, wait=lambda _: None)
        # Each read covers [previous checkpoint, tip]; the tips are contiguous and end at the head.
        self.assertEqual(checkpoints, [tip + 1 for tip in ranges])
        widths = [b - a for a, b in zip([100] + checkpoints, checkpoints)]
        self.assertEqual(widths[:7], [64, 128, 256, 512, 1024, 2048, 4096])
        self.assertLessEqual(max(widths), live_capture.MAX_SPAN)
        self.assertEqual(checkpoints[-1], head + 1)
        self.assertLess(len(ranges), 20)

    def test_proposal_event_records_the_finalized_slot_it_was_read_at(self):
        def rpc(method, *params):
            if method == "getSlot":
                return 150
            if method == "getSignaturesForAddress":
                return [{"slot": 120, "signature": "sig", "err": None}]
            if method == "getTransaction":
                return {"meta": {"logMessages": proposal_logs(5)}}
            raise AssertionError(method)

        events = []

        def checkpoint(cursor):
            raise Stop

        with patch("live_capture.rpc", side_effect=rpc), self.assertRaises(Stop):
            live_capture.watch(100, on_event=events.append, checkpoint=checkpoint, wait=lambda _: None)
        self.assertEqual([(e["slot"], e["anchor_slot"], e["seen_at_slot"]) for e in events], [(120, 90, 150)])


class LifecycleEventTest(unittest.TestCase):
    def test_cancellation_and_policy_update_are_decoded(self):
        proposal, config = bytes([7]) * 32, bytes([8]) * 32
        raws = [live_capture.CANCELLED + proposal, live_capture.POLICY_UPDATED + config + struct.pack("<Q", 1 << 32),
                # Another program's event with the same length is ignored.
                bytes(8) + proposal]
        logs = [f"Program {GATE} invoke [1]"] + ["Program data: " + base64.b64encode(raw).decode() for raw in raws]
        logs.append(f"Program {GATE} success")
        self.assertEqual(list(live_capture.gate_events(logs)), [
            ("ProposalCancelled", {"proposal": live_capture.to_base58(proposal)}),
            ("PolicyUpdated", {"config": live_capture.to_base58(config), "next_nonce": 1 << 32}),
        ])


if __name__ == "__main__":
    unittest.main()
