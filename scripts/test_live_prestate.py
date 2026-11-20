import base64
import struct
import unittest
from unittest.mock import patch

from devnet import LOADER, to_base58
from live_prestate import (
    PROPOSAL_TAG, PrestateRefused, capture_prestate, check_captured_accounts_unchanged,
    check_transaction_prestate, check_window_epoch, check_window_isolation,
)


def address(n):
    return to_base58(bytes([n]) * 32)


def account(owner, data, executable=False):
    return dict(owner=owner, executable=executable, data=[base64.b64encode(data).decode(), "base64"])


class LivePrestateTest(unittest.TestCase):
    def test_requires_static_loaded_and_table_accounts_with_known_prestate(self):
        payer, target, loaded_key, table = map(address, range(1, 5))
        item = {
            "meta": {"loadedAddresses": {"writable": [loaded_key], "readonly": []}},
            "transaction": {"message": {
                "accountKeys": [payer, target, "ComputeBudget111111111111111111111111111111"],
                "addressTableLookups": [{"accountKey": table, "writableIndexes": [0], "readonlyIndexes": []}],
            }},
        }
        captures = dict.fromkeys([payer, target, loaded_key, table])
        check_transaction_prestate(item, captures)
        for missing in (payer, loaded_key, table):
            with self.subTest(missing=missing), self.assertRaisesRegex(PrestateRefused, missing):
                check_transaction_prestate(item, {key: val for key, val in captures.items() if key != missing})
        item["meta"].pop("loadedAddresses")
        with self.assertRaisesRegex(PrestateRefused, "missing resolved ALT"):
            check_transaction_prestate(item, captures)

    def test_capture_requires_one_bank_and_locked_baseline(self):
        target, proposal, gate, buffer, program_data, seed = map(address, range(1, 7))
        event = dict(anchor_slot=100, replay_slots=10, nonce=9, target=target, proposal=proposal)
        raw = bytearray(248)
        raw[:8] = PROPOSAL_TAG
        struct.pack_into("<Q", raw, 9, 9)
        raw[17:49] = bytes([1]) * 32
        struct.pack_into("<Q", raw, 49, 90)
        raw[57:89] = bytes([4]) * 32
        struct.pack_into("<QQ", raw, 173, 100, 10)
        struct.pack_into("<q", raw, 231, 2000)
        proposal_record = account(gate, raw)
        executable = account(LOADER, struct.pack("<I", 2) + bytes([5]) * 32, True)
        old = account(LOADER, struct.pack("<IQB", 3, 90, 1) + bytes([8]) * 32 + b"old ELF")
        new = account(LOADER, struct.pack("<IB", 1, 1) + bytes([8]) * 32 + b"new ELF")
        owned = account(target, b"test state")
        seed_record = account(address(10), b"seed")
        responses = [
            dict(context=dict(slot=101), value=[dict(pubkey=address(7), account=owned)]),
            dict(context=dict(slot=101), value=[executable, proposal_record]),
            dict(context=dict(slot=101), value=[executable, proposal_record, new, old, seed_record]),
        ]
        with patch("live_prestate.rpc", side_effect=responses), patch(
            "live_prestate.check_captured_accounts_unchanged"
        ) as writes:
            bundle = capture_prestate(event, [seed], gate=gate)
            self.assertEqual(bundle["capture_slot"], 101)
            self.assertEqual(bundle["accounts"][program_data], old)
            self.assertEqual(bundle["accounts"][seed], seed_record)
            self.assertEqual(bundle["expires_at"], 2000)
            self.assertNotIn(proposal, writes.call_args.args[2])

        responses[1]["context"]["slot"] = 102
        with patch("live_prestate.rpc", side_effect=responses):
            with self.assertRaisesRegex(PrestateRefused, "RPC advanced"):
                capture_prestate(event, [seed], gate=gate)

    def test_rejects_written_state_and_missing_blocks(self):
        watched = address(1)
        item = {
            "meta": {"err": None, "loadedAddresses": {"writable": [watched]}},
            "transaction": {"message": {
                "accountKeys": [address(2), address(3)],
                "addressTableLookups": [{"accountKey": address(4), "writableIndexes": [0], "readonlyIndexes": []}],
                "header": {"numRequiredSignatures": 1, "numReadonlySignedAccounts": 0,
                           "numReadonlyUnsignedAccounts": 1},
            }},
        }
        with patch("live_prestate.rpc", return_value=[100, 102]), patch(
            "live_prestate.block", side_effect=[dict(parentSlot=99, transactions=[]),
                                               dict(parentSlot=100, transactions=[item])]
        ):
            with self.assertRaisesRegex(PrestateRefused, "could have changed"):
                check_captured_accounts_unchanged(100, 102, {watched})
        with patch("live_prestate.rpc", return_value=[100, 102]), patch(
            "live_prestate.block", side_effect=[dict(parentSlot=99, transactions=[]),
                                               dict(parentSlot=101, transactions=[])]
        ):
            with self.assertRaisesRegex(PrestateRefused, "parent chain"):
                check_captured_accounts_unchanged(100, 102, {watched})

    def test_window_checks_failed_fees_and_closing_parent(self):
        payer, target, outsider = map(address, range(1, 4))
        signature = bytes([9]) * 64
        record = dict(slot=100, index=0, transaction=(b"\x01" + signature).hex())

        def transaction(sender, program, error=None):
            return {
                "meta": {"err": error, "innerInstructions": [], "preBalances": [10, 20]},
                "transaction": {"signatures": [to_base58(signature)], "message": {
                    "accountKeys": [sender, program],
                    "instructions": [{"programIdIndex": 1}],
                    "header": {"numRequiredSignatures": 1, "numReadonlySignedAccounts": 0,
                               "numReadonlyUnsignedAccounts": 1},
                }},
            }

        event = dict(anchor_slot=100, replay_slots=2, target=target)
        first = dict(parentSlot=99, transactions=[transaction(payer, target), transaction(outsider, target, "failed")])
        tip = dict(parentSlot=100, transactions=[])
        for invalid in (None, "fee", "boundary", "selection", "prebalance"):
            with self.subTest(invalid=invalid):
                produced = dict(first)
                closing = dict(tip)
                if invalid == "fee":
                    produced["transactions"] = [transaction(payer, target), transaction(payer, outsider, "failed")]
                if invalid == "boundary":
                    closing["parentSlot"] = 102
                if invalid == "selection":
                    produced["transactions"] = [transaction(outsider, target), transaction(payer, target)]
                captures = {payer: {"lamports": 11 if invalid == "prebalance" else 10}, target: {"lamports": 20}}
                with patch("live_prestate.rpc", side_effect=[[100], [103]]), patch(
                    "live_prestate.block", side_effect=[produced, closing]
                ):
                    if invalid is None:
                        check_window_isolation(event, [record], {payer, target}, captures)
                    else:
                        with self.assertRaisesRegex(PrestateRefused, {
                            "fee": "modified by non-target",
                            "boundary": "parent chain",
                            "selection": "transaction list",
                            "prebalance": "first observed balance",
                        }[invalid]):
                            check_window_isolation(event, [record], {payer, target}, captures)

    def test_window_cannot_cross_epoch(self):
        with patch("live_prestate.rpc", return_value=dict(firstNormalSlot=100, slotsPerEpoch=50)):
            check_window_epoch(100, 149)
            for first, last in ((99, 100), (149, 150)):
                with self.assertRaisesRegex(PrestateRefused, "epoch"):
                    check_window_epoch(first, last)
