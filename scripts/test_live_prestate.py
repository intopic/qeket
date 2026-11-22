import base64
import struct
import unittest
from unittest.mock import patch

from devnet import LOADER, from_base58, to_base58
from live_prestate import (
    CLOCK, CONFIG_TAG, FEATURE_OWNER, NATIVE_LOADER, SYSTEM, PROPOSAL_TAG, SYSVAR_OWNER, PrestateRefused,
    capture_prestate, check_captured_accounts_unchanged, check_transaction_prestate, check_window_epoch,
    check_window_isolation, final_states, replay_archive, window_clocks,
)

TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
BPF_LOADER_2 = "BPFLoader2111111111111111111111111111111111"


def token_account(mint, owner, amount, state=1, program=TOKEN, extensions=None):
    data = bytearray(165)
    data[:32], data[32:64] = from_base58(mint), from_base58(owner)
    data[64:72] = amount.to_bytes(8, "little")
    data[108] = state
    if extensions is not None:
        # Token-2022: account type 2, then type-length-value extensions.
        data += b"\x02" + b"".join(struct.pack("<HH", kind, len(value)) + value for kind, value in extensions)
    return account(program, bytes(data), lamports=2_039_280)
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
                "instructions": [{"programIdIndex": 1, "accounts": [0, 3]}],
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
                "accountKeys": keys, "instructions": [{"programIdIndex": program_index, "accounts": []}],
                "header": {"numRequiredSignatures": 1, "numReadonlySignedAccounts": 0,
                           "numReadonlyUnsignedAccounts": readonly},
            }},
        }

    def run_archive(self, later=(), after=(), alpenglow=None, final=None, epoch_slots=1000, target_tx=None,
                    chain=(), features=None):
        """`chain` holds account states read after the window, besides the fee payer's."""
        target_tx = target_tx or self.transaction([self.payer, self.counter, self.target], 2, self.signature)
        record = dict(slot=101, index=0, transaction=(b"\x01" + self.signature).hex(), baseline={"fee": 5000})
        blocks = {101: dict(parentSlot=99, blockTime=1010, transactions=[target_tx]),
                  103: dict(parentSlot=101, blockTime=1030, transactions=list(later)),
                  105: dict(parentSlot=103, blockTime=1050, transactions=list(after))}
        feature = None if alpenglow is None else account(FEATURE_OWNER, b"\x01" + struct.pack("<Q", alpenglow))
        payer_end = final if final is not None else dict(account(SYSTEM, b"", lamports=4_995_000))
        states = {self.payer: payer_end, **dict(chain)}
        if features is None:
            features = [dict(pubkey=address(40), account=account(FEATURE_OWNER, b"\x01" + struct.pack("<Q", 0)))]

        def fake_rpc(method, *params):
            if method == "getBlocks":
                return [101, 103] if params[0] == 100 else [105]
            if method == "getMultipleAccounts":
                return {"context": {"slot": 105}, "value": [states.get(key) for key in params[0]]}
            if method == "getProgramAccounts":
                self.assertEqual(params[0], FEATURE_OWNER)
                return features
            return {"getEpochSchedule": dict(firstNormalSlot=0, slotsPerEpoch=epoch_slots, firstNormalEpoch=0),
                    "getBlocksWithLimit": [105], "getBlockTime": 990, "getAccountInfo": {"value": feature}}[method]

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

    def test_payer_passed_to_the_program_is_rebuilt_and_checked(self):
        """The usual DeFi case: the wallet pays the fee and signs as an account of the call."""
        tx = self.transaction([self.payer, self.counter, self.target], 2, self.signature)
        tx["transaction"]["message"]["instructions"][0]["accounts"] = [0, 1]
        archive = self.run_archive(target_tx=tx)
        accounts = {to_base58(bytes(key)): value for key, value in archive["accounts"]}
        self.assertEqual((accounts[self.payer]["lamports"], accounts[self.payer]["data"]), (5_000_000, []))
        self.assertEqual([to_base58(bytes(key)) for key, _ in archive["final_accounts"]], [self.payer])
        # Data after the window means the program changed more than lamports.
        with self.assertRaisesRegex(PrestateRefused, "captured initial state"):
            self.run_archive(target_tx=tx, final=account(SYSTEM, bytes(80)))
        bad = self.transaction([self.payer, self.target], 1)
        bad["transaction"]["message"]["instructions"][0]["accounts"] = [7]
        with self.assertRaisesRegex(PrestateRefused, "invalid compiled instruction account index"):
            check_transaction_prestate(bad, self.bundle["accounts"], {self.payer})

    def test_written_wallet_is_rebuilt_from_its_first_balance(self):
        """A wallet that signs and pays out, while another account pays the fee."""
        wallet = address(70)
        keys = [self.payer, self.counter, wallet, self.target]
        tx = self.transaction(keys, 3, self.signature, balances=[5_000_000, 100, 1_000, 1], readonly=1)
        archive = self.run_archive(target_tx=tx, chain={wallet: account(SYSTEM, b"", lamports=900)})
        accounts = {to_base58(bytes(key)): value for key, value in archive["accounts"]}
        self.assertEqual(accounts[wallet], dict(lamports=1_000, data=[], owner=[0] * 32, executable=False,
                                                rentEpoch=2 ** 64 - 1))
        finals = {to_base58(bytes(key)): value for key, value in archive["final_accounts"]}
        self.assertEqual(finals[wallet]["lamports"], 900)
        # A wallet the window created starts absent.
        tx = self.transaction(keys, 3, self.signature, balances=[5_000_000, 100, 0, 1], readonly=1)
        archive = self.run_archive(target_tx=tx, chain={wallet: account(SYSTEM, b"", lamports=900)})
        self.assertIn(list(from_base58(wallet)), archive["absent_accounts"])
        # Data or another owner after the window cannot be rebuilt.
        for state in (account(SYSTEM, bytes(80), lamports=900), account(address(71), b"", lamports=900)):
            with self.subTest(owner=state["owner"]), self.assertRaisesRegex(PrestateRefused, "not captured"):
                self.run_archive(target_tx=tx, chain={wallet: state})

    def test_payer_with_nonce_data_requires_snapshot(self):
        with self.assertRaisesRegex(PrestateRefused, "captured initial state"):
            self.run_archive(final=account(SYSTEM, bytes(80)))

    def test_final_accounts_batch_at_distinct_banks(self):
        keys = sorted(address(n) for n in range(1, 103))
        state = account(SYSTEM, b"")
        calls = []
        def rpc(method, *args):
            if method == "getBlocks":
                return [104, 105]
            calls.append(args[0])
            return {"context": {"slot": 103 + len(calls)}, "value": [state] * len(args[0])}
        blocks = [dict(parentSlot=103, transactions=[]), dict(parentSlot=104, transactions=[
            self.transaction([keys[0], self.outsider], 1)])]
        with patch("live_prestate.rpc", side_effect=rpc), patch("live_prestate.block", side_effect=blocks):
            self.assertEqual(len(final_states(keys, 103)), 102)
        self.assertEqual(calls, [keys[:100], keys[100:]])

    def test_archive_carries_the_cluster_features_active_at_the_anchor(self):
        def feature(n, data):
            return dict(pubkey=address(n), account=account(FEATURE_OWNER, data))
        archive = self.run_archive(features=[
            feature(41, b"\x01" + struct.pack("<Q", 100)), feature(42, b"\x01" + struct.pack("<Q", 101)),
            feature(43, b"\x00" + bytes(8)), feature(40, b"\x01" + struct.pack("<Q", 7)),
        ])
        # Activated after the anchor (101) or still pending (43): not active in the window.
        self.assertEqual(archive["features"], [[list(from_base58(address(40))), 7],
                                               [list(from_base58(address(41))), 100]])
        with self.assertRaisesRegex(PrestateRefused, "invalid feature account"):
            self.run_archive(features=[feature(40, b"\x05")])

    def test_read_only_accounts_and_programs_are_read_after_the_window(self):
        """A mint, an external program and its ProgramData, all untouched from anchor to read."""
        mint, program, program_data, vote = address(50), address(51), address(52), address(53)
        keys = [self.payer, self.counter, mint, program, vote, self.target]
        tx = self.transaction(keys, 5, self.signature, balances=[5_000_000, 100, 1, 1, 1, 1], readonly=4)
        chain = {mint: account(TOKEN, bytes(82)), vote: account(NATIVE_LOADER, b"", True),
                 program: account(LOADER, struct.pack("<I", 2) + from_base58(program_data), True),
                 program_data: account(LOADER, struct.pack("<IQB", 3, 10, 0) + b"\x7fELF other")}
        archive = self.run_archive(target_tx=tx, chain=chain)
        accounts = {to_base58(bytes(key)): value for key, value in archive["accounts"]}
        self.assertEqual(sorted(accounts), sorted([self.counter, self.payer, mint, program, program_data]))
        self.assertEqual(bytes(accounts[program_data]["data"][13:]), b"\x7fELF other")
        self.assertTrue(accounts[program]["executable"])
        # Read-only accounts need no end-state check. The builtin is left to LiteSVM.
        self.assertEqual([to_base58(bytes(key)) for key, _ in archive["final_accounts"]], [self.payer])
        # Another transaction writing the mint inside the window, or before its read, is refused.
        writer = self.transaction([self.outsider, mint], 1, balances=[1, 1], readonly=0)
        with self.assertRaisesRegex(PrestateRefused, "modified by non-target"):
            self.run_archive(target_tx=tx, chain=chain, later=[writer])
        with self.assertRaisesRegex(PrestateRefused, "used again"):
            self.run_archive(target_tx=tx, chain=chain, after=[writer])
        # So is an upgrade of the external program after the window.
        upgrade = self.transaction([self.outsider, program_data], 1, balances=[1, 1], readonly=0)
        with self.assertRaisesRegex(PrestateRefused, "used again"):
            self.run_archive(target_tx=tx, chain=chain, after=[upgrade])

    def test_sysvars_and_unsupported_loaders_are_refused(self):
        rent, program = "SysvarRent111111111111111111111111111111111", address(54)
        for key, state, reason in ((rent, account(SYSVAR_OWNER, bytes(17)), "sysvar"),
                                   (program, account("LoaderV411111111111111111111111111111111111", b"", True),
                                    "loader")):
            tx = self.transaction([self.payer, self.counter, key, self.target], 3, self.signature,
                                  balances=[5_000_000, 100, 1, 1], readonly=2)
            with self.subTest(reason), self.assertRaisesRegex(PrestateRefused, reason):
                self.run_archive(target_tx=tx, chain={key: state})

    def token_window(self, data, amount=500, final_amount=400, owner=None, pre=True, inner=True, signer=5,
                     program=TOKEN, extensions=None):
        """The target moves tokens out of an account the anchor capture did not include.

        The wallet that owns the token account signs the transfer as its authority.
        """
        holder, mint, wallet = address(60), address(61), address(62)
        keys = [self.payer, self.counter, holder, self.target, program, wallet]
        tx = self.transaction(keys, 3, self.signature, balances=[5_000_000, 100, 2_039_280, 1, 1, 1], readonly=3)
        tx["transaction"]["message"]["instructions"][0]["accounts"] = [1, 2, 4, 5]
        tx["meta"]["innerInstructions"] = [dict(index=0, instructions=[
            dict(programIdIndex=4, accounts=[2, 1, signer], data=to_base58(data))])] if inner else None
        if pre:
            tx["meta"]["preTokenBalances"] = [dict(accountIndex=2, mint=mint, owner=owner or wallet, programId=program,
                                                   uiTokenAmount=dict(amount=str(amount), decimals=6))]
        chain = {holder: token_account(mint, wallet, final_amount, program=program, extensions=extensions),
                 program: account(BPF_LOADER_2, b"\x7fELF token", True), wallet: account(SYSTEM, b"")}
        return holder, dict(target_tx=tx, chain=chain)

    def test_token_account_is_rebuilt_from_its_first_use(self):
        transfer = bytes([3]) + (100).to_bytes(8, "little")
        holder, window = self.token_window(transfer)
        archive = self.run_archive(**window)
        accounts = {to_base58(bytes(key)): value for key, value in archive["accounts"]}
        # The amount and lamports at first use, every other field from the state after the window.
        start = accounts[holder]
        self.assertEqual(int.from_bytes(bytes(start["data"][64:72]), "little"), 500)
        self.assertEqual(start["data"][:64], list(base64.b64decode(window["chain"][holder]["data"][0]))[:64])
        self.assertEqual((start["lamports"], bytes(start["owner"])), (2_039_280, from_base58(TOKEN)))
        # The token program comes from the cluster, not from LiteSVM's bundled copy.
        self.assertEqual(bytes(accounts[TOKEN]["data"]), b"\x7fELF token")
        # The runner must arrive at the token account the chain finalized.
        finals = {to_base58(bytes(key)): value for key, value in archive["final_accounts"]}
        self.assertEqual(int.from_bytes(bytes(finals[holder]["data"][64:72]), "little"), 400)

    def test_token_2022_account_with_static_extensions_is_rebuilt(self):
        checked = bytes([12]) + (100).to_bytes(8, "little") + b"\x06"
        holder, window = self.token_window(checked, program=TOKEN_2022, extensions=[(7, b"")])
        # TransferChecked: source, mint, destination, authority.
        window["target_tx"]["meta"]["innerInstructions"][0]["instructions"][0]["accounts"] = [2, 1, 1, 5]
        accounts = {to_base58(bytes(key)): value for key, value in self.run_archive(**window)["accounts"]}
        self.assertEqual(int.from_bytes(bytes(accounts[holder]["data"][64:72]), "little"), 500)
        self.assertEqual(bytes(accounts[holder]["data"][165:170]), b"\x02\x07\x00\x00\x00")
        # Tokens credited to the account need no authority check.
        _, window = self.token_window(bytes([3]) + (100).to_bytes(8, "little"))
        window["target_tx"]["meta"]["innerInstructions"][0]["instructions"][0]["accounts"] = [1, 2, 0]
        self.run_archive(**window)

    def test_token_account_changed_beyond_its_amount_is_refused(self):
        transfer = bytes([3]) + (100).to_bytes(8, "little")
        cases = [
            ("approve", dict(data=bytes([4]) + (5).to_bytes(8, "little")), "other than transfer"),
            ("no starting balance", dict(data=transfer, pre=False), "no observed starting balance"),
            ("owner changed", dict(data=transfer, owner=address(63)), "changed mint, owner"),
            ("no inner instructions", dict(data=transfer, inner=False), "inner instructions"),
            # A delegate's transfer also lowers delegated_amount, which no metadata records.
            ("delegate debit", dict(data=transfer, signer=0), "debited by a delegate"),
            ("transfer fee", dict(data=bytes([12]) + (100).to_bytes(8, "little") + b"\x06", program=TOKEN_2022,
                                  extensions=[(2, bytes(8))]), "extension 2"),
        ]
        for name, options, reason in cases:
            _, window = self.token_window(**options)
            with self.subTest(name), self.assertRaisesRegex(PrestateRefused, reason):
                self.run_archive(**window)
        # A written account that is not a token account cannot be rebuilt.
        _, window = self.token_window(transfer)
        holder = window["target_tx"]["transaction"]["message"]["accountKeys"][2]
        window["chain"][holder] = account(address(64), bytes(165), lamports=2_039_280)
        with self.assertRaisesRegex(PrestateRefused, "written by the window but was not captured"):
            self.run_archive(**window)

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
