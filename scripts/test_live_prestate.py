import base64
import struct
import unittest
from unittest.mock import patch

from devnet import LOADER, from_base58, to_base58
from live_prestate import (
    CLOCK, CONFIG_TAG, SYSTEM, PROPOSAL_TAG, SYSVAR_OWNER, PrestateRefused, capture_prestate,
    check_captured_accounts_unchanged, check_transaction_prestate, check_window_epoch,
    check_window_isolation, replay_archive, window_clocks,
)
from solana_tx import find_program_address


def address(n):
    return to_base58(bytes([n]) * 32)


def account(owner, data, executable=False, lamports=1):
    return dict(owner=owner, executable=executable, data=[base64.b64encode(data).decode(), "base64"],
                lamports=lamports, rentEpoch=2 ** 64 - 1)


def config_record(target, gate, verifiers, mode=0):
    raw = bytearray(371)
    raw[:8] = CONFIG_TAG
    raw[8] = 2
    raw[10:42] = from_base58(target)
    raw[106] = mode
    for i, key in enumerate(verifiers):
        raw[107 + 32 * i:139 + 32 * i] = from_base58(key)
    raw[331], raw[332] = len(verifiers), 2
    struct.pack_into("<HIQ", raw, 333, 1500, 1, 2)
    return account(gate, bytes(raw))


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
        config, _ = find_program_address([b"gatekeeper_cfg", from_base58(target)], gate)
        authority = from_base58(config)
        executable = account(LOADER, struct.pack("<I", 2) + bytes([5]) * 32, True)
        old = account(LOADER, struct.pack("<IQB", 3, 90, 1) + authority + b"old ELF")
        new = account(LOADER, struct.pack("<IB", 1, 1) + authority + b"new ELF")
        owned = account(target, b"test state")
        seed_record = account(address(10), b"seed")
        voters = [address(20), address(21), address(22)]
        clock = account(SYSVAR_OWNER, struct.pack("<QqQQq", 101, 900, 4, 5, 1234))

        def responses(slot=101, clock_record=clock, authority_record=new, last=101):
            return [
                dict(context=dict(slot=101), value=[dict(pubkey=address(7), account=owned)]),
                dict(context=dict(slot=slot), value=[executable, proposal_record]),
                dict(context=dict(slot=last), value=[executable, proposal_record, authority_record, old, seed_record,
                                                     config_record(target, gate, voters, mode=1), clock_record]),
            ]

        with patch("live_prestate.rpc", side_effect=responses()), patch(
            "live_prestate.check_captured_accounts_unchanged"
        ) as writes:
            bundle = capture_prestate(event, [seed], gate=gate)
            self.assertEqual(bundle["capture_slot"], 101)
            self.assertEqual(bundle["accounts"][program_data], old)
            self.assertEqual(bundle["accounts"][seed], seed_record)
            self.assertEqual(bundle["expires_at"], 2000)
            self.assertEqual(bundle["config_address"], config)
            self.assertEqual(bundle["config"]["replay_mode"], "NoRevertAndCuBound")
            self.assertEqual(bundle["config"]["verifiers"], voters)
            self.assertEqual(bundle["clock"]["epoch_start_timestamp"], 900)
            self.assertNotIn(proposal, writes.call_args.args[2])
            self.assertNotIn(config, writes.call_args.args[2])

        # Successive finalized banks are accepted. The unchanged-state scan then
        # runs through the latest read.
        later_clock = account(SYSVAR_OWNER, struct.pack("<QqQQq", 104, 900, 4, 5, 1234))
        with patch("live_prestate.rpc", side_effect=responses(slot=102, last=104, clock_record=later_clock)), patch(
            "live_prestate.check_captured_accounts_unchanged"
        ) as writes:
            bundle = capture_prestate(event, [seed], gate=gate)
            self.assertEqual(bundle["capture_slot"], 104)
            self.assertEqual(writes.call_args.args[:2], (100, 104))

        stale_clock = account(SYSVAR_OWNER, struct.pack("<QqQQq", 100, 900, 4, 5, 1234))
        late_clock = account(SYSVAR_OWNER, struct.pack("<QqQQq", 133, 900, 4, 5, 1234))
        foreign = account(LOADER, struct.pack("<IB", 1, 1) + bytes([8]) * 32 + b"new ELF")
        old_foreign = account(LOADER, struct.pack("<IQB", 3, 90, 1) + bytes([8]) * 32 + b"old ELF")
        for kwargs, reason in ((dict(slot=102), "older bank"), (dict(last=133, clock_record=late_clock), "too far"),
                               (dict(clock_record=stale_clock), "Clock"),
                               (dict(authority_record=foreign), "authority differ")):
            with self.subTest(reason=reason), patch("live_prestate.rpc", side_effect=responses(**kwargs)), \
                    patch("live_prestate.check_captured_accounts_unchanged"):
                with self.assertRaisesRegex(PrestateRefused, reason):
                    capture_prestate(event, [seed], gate=gate)
        # The loader authorities agree, but not with the gate config PDA.
        mismatched = responses(authority_record=foreign)
        mismatched[2]["value"][3] = old_foreign
        with patch("live_prestate.rpc", side_effect=mismatched), patch(
                "live_prestate.check_captured_accounts_unchanged"):
            with self.assertRaisesRegex(PrestateRefused, "gate config does not hold"):
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
        with patch("live_prestate.rpc", return_value=dict(firstNormalSlot=100, slotsPerEpoch=50, firstNormalEpoch=7)):
            self.assertEqual(check_window_epoch(100, 149), 7)
            self.assertEqual(check_window_epoch(150, 160), 8)
            for first, last in ((99, 100), (149, 150)):
                with self.assertRaisesRegex(PrestateRefused, "epoch"):
                    check_window_epoch(first, last)


class ReplayArchiveTest(unittest.TestCase):
    """The worker turns a finalized window into the archive the Rust verifier replays."""

    def setUp(self):
        self.target, self.data, self.counter, self.payer, self.buffer, self.proposal, self.outsider = [
            address(n) for n in (1, 2, 3, 4, 5, 6, 7)]
        self.signature = bytes([9]) * 64
        program = account(LOADER, struct.pack("<I", 2) + from_base58(self.data), True)
        program_data = account(LOADER, struct.pack("<IQB", 3, 90, 1) + bytes(32) + b"\x7fELF baseline" + bytes(8))
        self.bundle = dict(
            accounts={self.target: program, self.data: program_data, self.counter: account(self.target, bytes(8), lamports=100),
                      self.buffer: account(LOADER, struct.pack("<IB", 1, 1) + bytes(32) + b"\x7fELF new"),
                      self.proposal: account(address(30), bytes(248)), address(8): None},
            program_data=self.data, buffer=self.buffer, config_address=address(31), source_commit="ab" * 20,
            candidate_sha256="cd" * 32, clock=dict(epoch=3, epoch_start_timestamp=900, leader_schedule_epoch=4),
            config=dict(replay_mode="StrictEquality", min_replay_txs=1, max_cu_delta_bps=1500,
                        verifiers=[address(20), address(21)]),
        )
        self.event = dict(target=self.target, proposal=self.proposal, nonce=0, anchor_slot=100, replay_slots=3)

    def transaction(self, keys, program_index, signature=None, error=None, balances=None, readonly=1):
        return {
            "meta": {"err": error, "innerInstructions": [], "preBalances": balances or [5_000_000, 100, 1][:len(keys)],
                     "postBalances": [1] * len(keys), "fee": 5000, "computeUnitsConsumed": 300,
                     "loadedAddresses": {"writable": [], "readonly": []}},
            "transaction": {"signatures": [to_base58(signature or bytes([7]) * 64)], "message": {
                "accountKeys": keys, "instructions": [{"programIdIndex": program_index}],
                "header": {"numRequiredSignatures": 1, "numReadonlySignedAccounts": 0,
                           "numReadonlyUnsignedAccounts": readonly},
            }},
        }

    def run_archive(self, later=(), after=(), alpenglow=None, final=None, epoch_slots=1000):
        target_tx = self.transaction([self.payer, self.counter, self.target], 2, self.signature)
        record = dict(slot=101, index=0, transaction=(b"\x01" + self.signature).hex(), baseline={"fee": 5000})
        blocks = {101: dict(parentSlot=99, blockTime=1010, transactions=[target_tx]),
                  103: dict(parentSlot=101, blockTime=1030, transactions=list(later)),
                  105: dict(parentSlot=103, blockTime=1050, transactions=list(after))}
        feature = None if alpenglow is None else account(
            "Feature111111111111111111111111111111111111", b"\x01" + struct.pack("<Q", alpenglow))
        payer_end = final if final is not None else dict(account(SYSTEM, b"", lamports=4_995_000))

        def fake_rpc(method, *params):
            if method == "getBlocks":
                return [101, 103] if params[0] == 100 else [105]
            return {"getEpochSchedule": dict(firstNormalSlot=0, slotsPerEpoch=epoch_slots, firstNormalEpoch=0),
                    "getBlocksWithLimit": [105], "getBlockTime": 990, "getAccountInfo": {"value": feature},
                    "getMultipleAccounts": {"context": {"slot": 105}, "value": [payer_end]}}[method]

        def fake_capture(snapshot, limit, validate_transaction=None, include_baseline=False):
            self.assertTrue(include_baseline)
            validate_transaction(target_tx)
            return dict(snapshot, format_version=2, records=[record], transaction_manifest=[1] * 32)

        self.bundle["clock"]["epoch"] = 100 // epoch_slots
        with patch("live_prestate.rpc", side_effect=fake_rpc), patch(
                "live_prestate.block", side_effect=lambda slot, _: blocks[slot]), patch(
                "live_prestate.capture", side_effect=fake_capture):
            return replay_archive(self.event, self.bundle, 500)

    def test_archive_reconstructs_fee_payer_clocks_and_policy(self):
        archive = self.run_archive()
        self.assertEqual([c["unix_timestamp"] for c in archive["clocks"]], [990, 1010, 1010, 1030])
        self.assertEqual({c["epoch_start_timestamp"] for c in archive["clocks"]}, {900})
        accounts = {to_base58(bytes(key)): value for key, value in archive["accounts"]}
        # The runner installs the target ELF itself. The counter keeps its captured
        # state, and the unknown fee payer starts as a System account.
        self.assertEqual(sorted(accounts), sorted([self.counter, self.payer]))
        self.assertEqual(accounts[self.payer], dict(lamports=5_000_000, data=[], owner=[0] * 32, executable=False,
                                                    rentEpoch=2 ** 64 - 1))
        self.assertEqual(accounts[self.counter]["data"], [0] * 8)
        self.assertEqual(archive["absent_accounts"], [])
        self.assertEqual(bytes(archive["gate"]), from_base58(address(31)))
        self.assertEqual(bytes(archive["baseline_elf"]),
                         __import__("hashlib").sha256(b"\x7fELF baseline" + bytes(8)).digest())
        self.assertEqual((archive["replay_mode"], archive["min_txs"], archive["cu_limit_bps"]),
                         ("StrictEquality", 1, 1500))
        self.assertEqual(archive["buffer"]["data"][37:], list(b"\x7fELF new"))
        # The runner must arrive at the payer state the chain finalized after the window.
        (key, end_state), = archive["final_accounts"]
        self.assertEqual((to_base58(bytes(key)), end_state["lamports"], end_state["data"]), (self.payer, 4_995_000, []))

    def test_alpenglow_transactions_read_the_parent_block_time(self):
        # Slot 101 executes with the time of its parent 99, slot 103 with that of 101.
        archive = self.run_archive(alpenglow=0, epoch_slots=40)
        self.assertEqual([c["unix_timestamp"] for c in archive["clocks"]], [990, 990, 990, 1010])
        # Without the feature account the TowerBFT rule applies.
        self.assertEqual([c["unix_timestamp"] for c in self.run_archive()["clocks"]], [990, 1010, 1010, 1030])

    def test_window_near_the_alpenglow_switch_is_refused(self):
        # Window epoch 2. Activation in epochs 1 or 2, or after the anchor, is too close.
        for activated in (50, 99, 101):
            with self.subTest(activated=activated), self.assertRaisesRegex(PrestateRefused, "Alpenglow"):
                self.run_archive(alpenglow=activated, epoch_slots=40)

    def test_fee_payer_must_be_quiet_until_its_final_state_is_read(self):
        reuse = self.transaction([self.payer, self.outsider], 1, balances=[4_995_000, 0])
        with self.assertRaisesRegex(PrestateRefused, "used again"):
            self.run_archive(after=[reuse])

    def test_reconstructed_fee_payer_is_isolated(self):
        # A later failed transaction from the same payer could have charged its fee.
        rival = self.transaction([self.payer, self.outsider], 1, error="failed", balances=[4_000_000, 0])
        with self.assertRaisesRegex(PrestateRefused, "modified by non-target"):
            self.run_archive([rival])
        # Unrelated traffic is fine.
        self.run_archive([self.transaction([self.outsider, address(12)], 1, balances=[1, 1])])

    def test_clocks_need_capture_epoch_and_block_times(self):
        scan = dict(parent_slot=99, block_times={100: 1000, 102: 1020}, parents={100: 99, 102: 100})
        self.assertEqual([c["unix_timestamp"] for c in window_clocks(self.event, self.bundle, 3, scan)],
                         [1000, 1000, 1020, 1020])
        with self.assertRaisesRegex(PrestateRefused, "different epoch"):
            window_clocks(self.event, self.bundle, 2, scan)
        with self.assertRaisesRegex(PrestateRefused, "block time"):
            window_clocks(self.event, self.bundle, 3, dict(scan, block_times={100: None}))
