import base64
import struct
import unittest
from unittest.mock import patch

from devnet import LOADER, to_base58
from live_prestate import PROPOSAL_TAG, PrestateRefused, capture_prestate, check_captured_accounts_unchanged


def address(n):
    return to_base58(bytes([n]) * 32)


def account(owner, data, executable=False):
    return dict(owner=owner, executable=executable, data=[base64.b64encode(data).decode(), "base64"])


class LivePrestateTest(unittest.TestCase):
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
