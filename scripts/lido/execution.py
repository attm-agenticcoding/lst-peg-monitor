"""Block-pinned, integer-only Lido v4 execution replay.

Only eth_getLogs, eth_getBlockByNumber and eth_getBalance are used. No eth_call,
explorer scraping, archive-provider fallback or credentials. A denied RPC call is
terminal. Source rules: lidofinance/core v4.0.1 contracts 0.4.24/Lido.sol,
0.8.9/WithdrawalQueue{,Base}.sol, oracle/HashConsensus.sol and PausableUntil.sol.

The historical audit's configuration getters were UNPINNED. That assumption is
retained as a visible conditional input; watching subsequent events does not
retroactively authenticate it. Queue, balance and share quantities have separate
block-pinned event evidence. Protocol/topology changes fail closed.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import time
from pathlib import Path
import urllib.error
import urllib.request

ADDRESSES = {
    "core": "0xae7ab96520de3a18e5e111b5eaab095312d7fe84",
    "queue": "0x889edc2edab5f40e902b864ad4d7ade8e412f9b1",
    "oracle": "0x852ded011285fe67063a08005c71a85690503cee",
    "consensus": "0xd624b08c83baecf0807dd2c6880c3154a5f0b288",
    "sanity": "0x147f8d3cf3004faf9bf94e88b54b6c06de507be9",
    "locator": "0xc1d0b3de6792bf6b4b37eccdcc24e45978cfd2eb",
    "kernel": "0xb8ffc3cd6e7cf5a098a1c92f48009765b24088dc",
    "el_vault": "0x388c818ca8b9251b393131c08a736a67ccb19297",
    "withdrawal_vault": "0xb9d7934878b5fb9610b3fe8a5e441e8fad7e293f",
}
UINT256_MAX = (1 << 256) - 1
ZERO_TOPIC = "0x" + "0" * 64
E27 = 10 ** 27
TOPICS = {'WithdrawalRequested': '0xf0cb471f23fb74ea44b8252eb1881a2dca546288d9f6e90d1a0e82fe0ed342ab',
 'WithdrawalsFinalized': '0x197874c72af6a06fb0aa4fab45fd39c7cb61ac0992159872dc3295207da7e9eb',
 'WithdrawalClaimed': '0x6ad26c5e238e7d002799f9a5db07e81ef14e37386ae03496d7a7ef04713e145b',
 'Submitted': '0x96a25c8ce0baabc1fdefd93e9ed25d8e092a3332f3aa9a41722b5697231d1d1a',
 'Unbuffered': '0x76a397bea5768d4fca97ef47792796e35f98dc81b16c1de84e28a818e1f97108',
 'ExternalEtherTransferredToBuffer': '0x4ee34277c93491eeca655ad5c42ae1c193a5719e1c8837df9058af7696817cce',
 'ETHDistributed': '0x92dd3cb149a1eebd51fd8c2a3653fd96f30c4ac01d4f850fc16d46abd6c3e92f',
 'TokenRebased': '0xff08c3ef606d198e316ef5b822193c489965899eb4e3c248cea1a4626c3eda50',
 'InternalShareRateUpdated': '0xaf00d86be4cd299db16aa59803992e174fa88b67d81a0c7dd0148f9a75606a8d',
 'DepositsReserveSet': '0x257937ce49d8cbbe1d68a2be7297a18a6e2830528d9154a0f131e5094e894613',
 'DepositsReserveTargetSet': '0x72cc060f0ebe226352283dbe16fdcdb0d674f306ad980fbfa3d4b6932fee0b1e',
 'TransferShares': '0x9d9c909296d9c674451c0c24f02cb64981eb3b727f99865939192f880a755dcb',
 'SharesBurnt': '0x8b2a1e1ad5e0578c3dd82494156e985dade827a87c573b5c1c7716a32162ad64',
 'ExternalSharesMinted': '0xee473f96486a2f4b93ccb6729f121223e96975db6e0d6a5ef01f56477e3eab3b',
 'ExternalSharesBurnt': '0xad21467656c56eb8c99f7916faa12f7a657a04d8802100acec62e92451ac5606',
 'ExternalBadDebtInternalized': '0x4e80196ef1285462b2c4ee20f88e18e58c59e405eec6fd51f5fd1d614bc98a7f',
 'DepositedPostReportUpdated': '0xd87d7ff193e5d1560bdbe21e4e14d13dde0de4e49a03812517640af0a2d5c42c',
 'CLBalancesUpdated': '0xc091cf4d34e62bb075bf8dadd00496528c94b8f3fdec3affd7414840847017d2',
 'Paused': '0x32fb7c9891bc4f963c7de9f1186d2a7755c7d6e9f4604dabe1d8bb3027c2f49e',
 'Resumed': '0x62451d457bc659158be6e6247f56ec1df424a5c7597f71c20c2bc44e0965c8f9',
 'BunkerModeEnabled': '0x47f03b07e5b5377f871539bb2942f5ecb72733be9fc9d55a17b6d6a05d418345',
 'BunkerModeDisabled': '0xd1f8a2998c0caf73e09434aa93d273a599060d789407c6f70ccd4c9c9f32c8f4',
 'FrameConfigSet': '0xe343afa5219eaf28c50ce9cd658acd69cbe28b34fa773eb3a523e28007f64afc',
 'FastLaneConfigSet': '0xab8b22776606cc75c47792d32af7e63ed9ca74e85c9780a7fc7994fdbd6fde2b',
 'RequestTimestampMarginSet': '0x1ae32ca67bad0d65fa81ce18c6e37fe5e128141e1052f38e9bcd03ec61e0db6f',
 'Transfer': '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef',
 'Approval': '0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925',
 'ApprovalForAll': '0x17307eab39ab6107e8899845ad3d59bd9653f200f220920489ca2b5937696c31',
 'ELRewardsReceived': '0xd27f9b0c98bdee27044afa149eadcd2047d6399cb6613a45c5b87e6aca76e6b5',
 'WithdrawalsReceived': '0x6e5086f7e1ab04bd826e77faae35b1bcfe31bd144623361a40ea4af51670b1c3',
 'StakingPaused': '0x26d1807b479eaba249c1214b82e4b65bbb0cc73ee8a17901324b1ef1b5904e49',
 'StakingResumed': '0xedaeeae9aed70c4545d3ab0065713261c9cee8d6cf5c8b07f52f0a65fd91efda',
 'StakingLimitSet': '0xce9fddf6179affa1ea7bf36d80a6bf0284e0f3b91f4b2fa6eea2af923e7fac2d',
 'StakingLimitRemoved': '0x9b2a687c198898fcc32a33bbc610d478f177a73ab7352023e6cc1de5bf12a3df',
 'DepositedValidatorsChanged': '0xe0aacfc334457703148118055ec794ac17654c6f918d29638ba3b18003cee5ff',
 'MaxExternalRatioBPSet': '0x13c514ee70ee403f89bdf5ab83908edba92a77e3a61e19b774670c0a2cb2d7e6',
 'RoleGranted': '0x2f8788117e7eff1d82e926ec794901d17c78024a50270940304540a733656f0d',
 'RoleRevoked': '0xf6391f5c32d9c69d2a47ea670b442974b53935d1edc7fd64eb21e047a839171b',
 'RoleAdminChanged': '0xbd79b86ffe0ab8e8776151514217cd7cacd52c909f66475c3af44e129f0b00ff',
 'MemberAdded': '0xe17e0e2cd88e2144dd54f3d823c30d4569092bcac1aabaec1129883e9cc12d2e',
 'MemberRemoved': '0xa182730913550d27dc6c5813fad297cb0785871bec3d0152c5650e59c5d39d60',
 'QuorumSet': '0x9f40cfd22fe91777c78f252bd21a710f3fb007dc2f321876891e7644ba0ae175',
 'ReportReceived': '0x92f77576dabd7bad26f75c36abb3021b5bbb66a3e5688570a0355daddd417488',
 'ConsensusReached': '0x2b6bc782c916fa763822f1e50c6db0f95dade36d6541a8a4cbe070735b8b226d',
 'ConsensusLost': '0xde3f4ea5aa67881831e8fad2b0855d47e75aa63a2fae6ef657ffd5f856c4a613',
 'ReportSubmitted': '0xaed7d1a7a1831158dcda1e4214f5862f450bd3eb5721a5f322bf8c9fe1790b0a',
 'ProcessingStarted': '0xf73febded7d4502284718948a3e1d75406151c6326bde069424a584a4f6af87a',
 'ExtraDataSubmitted': '0x6d8abc91d336688c551c9bae92a74fa116852ac20bb9b2df4c12bd2fcf1cd46a',
 'WarnExtraDataIncompleteProcessing': '0x801a93267f699b033e11b662b16b36c41b6f9a59a5b5ad967d4cb84232e523c2',
 'NegativeCLRebaseConfirmed': '0x2eb398b1f77a0d380bde88f991aabff30a4a616b7b3b7961ab94d2b710ea3052',
 'Stopped': '0x7acc84e34091ae817647a4c49116f5cc07f319078ba80f8f5fde37ea7e25cbd6',
 'Upgraded': '0xbc7cd75a20ee27fd9adebab32041f755214dbc6bffa90cc0225b39da2e5c2d3b',
 'LidoLocatorSet': '0x61f9416d3c29deb4e424342445a2b132738430becd9fa275e11297c90668b22e',
 'SetApp': '0x2ec1ae0a449b7ae354b9dacfb3ade6b6332ba26b7fcbb935835fa39dd7263b23',
 'ContractVersionSet': '0xfddcded6b4f4730c226821172046b48372d3cd963c159701ae1b7c3bcac541bb',
 'ConsensusVersionSet': '0x68f33ba5b0b1993d5d640c62afd0a9aeffd6dea1829f666b0b8bfd4254e2fa26',
 'ReportProcessorSet': '0x3b59429457a41af89ea682ac9ed8abb8e99eb5c7d3363d5eedfc6bff6271a81e'}
TOPICS["BatchMetadataUpdate"] = "0x6bd5c950a8d8df17f772f5af37cb3655737899cbf903264b9795592da439661c"
TOPICS["BaseURISet"] = "0xf9c7803e94e0d3c02900d8a90893a6d5e90dd04d32a4cfe825520f82bf9f32f6"
TOPICS["NftDescriptorAddressSet"] = "0x4ec04ac71c49eea0a94dc5967b493412a8cdb2934b367713019d3b110e9f0ba8"
EVENT_NAMES = {value: name for name, value in TOPICS.items()}
MONITORED = tuple(k for k in ADDRESSES if not k.endswith("_vault"))


class ExecutionError(RuntimeError):
    """No new state may be published or persisted after this exception."""


class RpcError(ExecutionError):
    def __init__(self, message, *, code=None, denied=False):
        super().__init__(message)
        self.code, self.denied = code, denied


class HttpRpc:
    """Small read-only JSON-RPC transport; no retries/fallback on authorization errors."""
    METHODS = {"eth_getLogs", "eth_getBlockByNumber", "eth_getBalance"}

    def __init__(self, url="https://rpc.flashbots.net", timeout=45):
        self.url, self.timeout, self.counter = url, timeout, 0
        self.evidence = []

    def __call__(self, method, params):
        if method not in self.METHODS:
            raise ExecutionError(f"RPC method not authorized by this collector: {method}")
        self.counter += 1
        payload = {"jsonrpc": "2.0", "id": self.counter, "method": method, "params": params}
        request = urllib.request.Request(self.url, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.load(response)
        except urllib.error.HTTPError as error:
            raise RpcError(f"{method}: HTTP {error.code}; no alternate provider or retry",
                           code=error.code, denied=error.code in (401, 403)) from error
        except (OSError, ValueError) as error:
            raise RpcError(f"{method}: {error}") from error
        self.evidence.append({"request": payload, "response_sha256": hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()})
        if body.get("id") != self.counter or body.get("jsonrpc") != "2.0":
            raise RpcError("Mismatched JSON-RPC envelope")
        if "error" in body:
            error = body["error"]
            raise RpcError(f"{method}: {error}", code=error.get("code"),
                           denied=error.get("code") in (401, 403))
        if "result" not in body:
            raise RpcError(f"{method}: missing result")
        return body["result"]


def integer(value):
    """Reject floats, booleans, malformed integers and negatives."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ExecutionError(f"Expected exact unsigned integer, got {value!r}")
    try:
        result = int(value, 16 if value.startswith("0x") else 10) if isinstance(value, str) else value
    except ValueError as error:
        raise ExecutionError(f"Invalid integer: {value!r}") from error
    if result < 0 or result > UINT256_MAX:
        raise ExecutionError("Unsigned integer out of range")
    return result


def words(log, count=None):
    data = log.get("data", "")
    if not isinstance(data, str) or not data.startswith("0x") or len(data[2:]) % 64:
        raise ExecutionError("Malformed event ABI data")
    try:
        result = [int(data[i:i + 64], 16) for i in range(2, len(data), 64)]
    except ValueError as error:
        raise ExecutionError("Malformed event ABI word") from error
    if count is not None and len(result) != count:
        raise ExecutionError(f"Event expected {count} ABI words, got {len(result)}")
    return result


def key(log):
    return integer(log["blockNumber"]), integer(log["transactionIndex"]), integer(log["logIndex"])


def _utc(timestamp):
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat()


def _hash(value):
    if not isinstance(value, str) or len(value) != 66 or not value.startswith("0x"):
        raise ExecutionError("Invalid block/transaction hash")
    try:
        bytes.fromhex(value[2:])
    except ValueError as error:
        raise ExecutionError("Invalid hash hex") from error
    return value.lower()


def _header(rpc, number):
    h = rpc("eth_getBlockByNumber", [hex(number), False])
    if not isinstance(h, dict) or integer(h.get("number")) != number:
        raise ExecutionError(f"Missing/mismatched header at {number}")
    _hash(h.get("hash"))
    integer(h.get("timestamp"))
    return h


def fetch_logs(rpc, start, end, *, chunk_blocks=1000, max_calls=256, result_cap=10000):
    """Bounded inclusive ranges, with split only for explicit result-size errors.

    Monitored addresses use one OR-address query, no topics filter. A response at
    the provider cap is ambiguous and must be split, never silently truncated.
    """
    if end < start:
        return [], []
    if chunk_blocks <= 0 or max_calls <= 0 or result_cap <= 0:
        raise ExecutionError("Invalid log acquisition bounds")
    pending = [(n, min(n + chunk_blocks - 1, end)) for n in range(start, end + 1, chunk_blocks)]
    if len(pending) > max_calls:
        raise ExecutionError("Replay interval exceeds bounded acquisition window; backfill required")
    answer, coverage, calls = [], [], 0
    while pending:
        lo, hi = pending.pop(0)
        calls += 1
        if calls > max_calls:
            raise ExecutionError("Log acquisition call budget exceeded; no checkpoint advancement")
        split = False
        try:
            rows = rpc("eth_getLogs", [{"address": [ADDRESSES[k] for k in MONITORED],
                                      "fromBlock": hex(lo), "toBlock": hex(hi)}])
        except RpcError as error:
            # -32005 is the documented limit-exceeded code. Authorization errors
            # must NEVER trigger smaller ranges or another route.
            if error.denied or error.code != -32005:
                raise
            split, rows = True, []
        if not isinstance(rows, list):
            raise ExecutionError("eth_getLogs did not return a list")
        if split or len(rows) >= result_cap:
            if lo == hi:
                raise ExecutionError("A single block exceeds the unambiguous RPC log limit")
            mid = (lo + hi) // 2
            pending[0:0] = [(lo, mid), (mid + 1, hi)]
            continue
        for log in rows:
            if not lo <= key(log)[0] <= hi or log.get("address", "").lower() not in {ADDRESSES[k] for k in MONITORED}:
                raise ExecutionError("RPC returned a log outside the requested range/address")
            if log.get("removed", False):
                raise ExecutionError("Removed/reorganized log returned")
            _hash(log.get("blockHash"))
            _hash(log.get("transactionHash"))
        answer.extend(rows)
        coverage.append({"from_block": lo, "to_block": hi, "count": len(rows)})
    answer.sort(key=key)
    seen = set()
    for log in answer:
        identity = (integer(log["blockNumber"]), integer(log["logIndex"]))
        if identity in seen:
            raise ExecutionError("Duplicate log position; refusing ambiguous replay")
        seen.add(identity)
    return answer, coverage


def _numeric_state(state):
    s = copy.deepcopy(state)
    for field in ("block", "timestamp", "last_request_id", "last_finalized_id", "last_report_timestamp",
                  "queue_resume_since", "queue_bunker_since"):
        s[field] = integer(s[field])
    for field in list(s):
        if field.endswith("_wei"):
            s[field] = integer(s[field])
    return s


def _serial_state(s):
    s = copy.deepcopy(s)
    for field in list(s):
        if field.endswith("_wei"):
            s[field] = str(s[field])
    return s


def _totals(s):
    internal_shares = s["total_shares_wei"] - s["external_shares_wei"]
    internal_ether = s["core_buffer_wei"] + s["cl_balance_wei"] + s["deposited_post_report_wei"]
    if internal_shares <= 0 or internal_ether <= 0:
        raise ExecutionError("Invalid internal share-rate denominator/numerator")
    total_ether = internal_ether + s["external_shares_wei"] * internal_ether // internal_shares
    return internal_shares, internal_ether, total_ether


def _validate(s):
    if s.get("schema") != 1:
        raise ExecutionError("Unknown execution state schema")
    if s["last_finalized_id"] > s["last_request_id"]:
        raise ExecutionError("Finalization frontier exceeds requests")
    expected = s["last_finalized_id"] + 1
    for request in s["pending_requests"]:
        if integer(request["request_id"]) != expected:
            raise ExecutionError(f"Pending queue is not contiguous at ID {expected}")
        expected += 1
        for field in ("amount_steth_wei", "amount_shares_wei", "timestamp", "report_timestamp"):
            integer(request[field])
        if integer(request["timestamp"]) > s["timestamp"]:
            raise ExecutionError("Pending request newer than state")
    if expected != s["last_request_id"] + 1:
        raise ExecutionError("Pending queue missing tail IDs")
    for field, value in s.items():
        if field.endswith("_wei"):
            integer(value)
    _totals(s)


_PASSIVE_COMMON = {"Approval", "Transfer", "ApprovalForAll"}
_PASSIVE_CORE = {"ELRewardsReceived", "WithdrawalsReceived", "StakingPaused", "StakingResumed",
                 "StakingLimitSet", "StakingLimitRemoved", "DepositedValidatorsChanged", "MaxExternalRatioBPSet",
                 "Resumed", "ExternalBadDebtInternalized"}
_PASSIVE_CONSENSUS = {"MemberAdded", "MemberRemoved", "QuorumSet", "ReportReceived", "ConsensusReached", "ConsensusLost"}
_PASSIVE_ORACLE = {"ReportSubmitted", "ProcessingStarted", "ExtraDataSubmitted", "WarnExtraDataIncompleteProcessing"}


def replay_events(state, logs, *, validate_reports=True):
    """Pure event replay. Caller authenticates log headers and complete coverage."""
    s = _numeric_state(state)
    addresses = {v: k for k, v in ADDRESSES.items()}
    ordered = sorted(logs, key=key)
    report_txs = {l["transactionHash"].lower() for l in ordered
                  if l["address"].lower() == ADDRESSES["core"] and l["topics"][0] == TOPICS["ETHDistributed"]}
    finalizations = {}
    for log in ordered:
        event = EVENT_NAMES.get(log["topics"][0]) if log.get("topics") else None
        source = addresses.get(log["address"].lower())
        # Passive token/governance observations do not need a per-block header.
        if (event in _PASSIVE_COMMON or
            source == "core" and event in _PASSIVE_CORE or
            source == "consensus" and event in _PASSIVE_CONSENSUS or
            source == "oracle" and event in _PASSIVE_ORACLE):
            continue
        timestamp = integer(log.get("blockTimestamp", 0))
        tx = log["transactionHash"].lower()
        if event in {"Upgraded", "LidoLocatorSet", "SetApp", "ContractVersionSet", "ConsensusVersionSet", "ReportProcessorSet", "RoleGranted", "RoleRevoked", "RoleAdminChanged"}:
            raise ExecutionError(f"Unsupported topology/version change: {source}.{event}; re-audit required")
        if source == "core":
            if event in ("Submitted", "ExternalEtherTransferredToBuffer"):
                s["core_buffer_wei"] += words(log, 2 if event == "Submitted" else 1)[0]
            elif event == "Unbuffered":
                s["core_buffer_wei"] -= words(log, 1)[0]
            elif event == "DepositedPostReportUpdated":
                s["deposited_post_report_wei"] = words(log, 1)[0]
            elif event == "CLBalancesUpdated":
                s["cl_balance_wei"] = sum(words(log, 2))
            elif event in ("DepositsReserveSet", "DepositsReserveTargetSet"):
                s["reserve_stored_wei" if event == "DepositsReserveSet" else "reserve_target_wei"] = words(log, 1)[0]
            elif event == "TransferShares":
                value = words(log, 1)[0]
                if len(log["topics"]) != 3:
                    raise ExecutionError("Malformed TransferShares topics")
                if log["topics"][1] == ZERO_TOPIC:
                    s["total_shares_wei"] += value
                if log["topics"][2] == ZERO_TOPIC:
                    # v4 uses SharesBurnt instead; a new burn representation is unsafe.
                    raise ExecutionError("Unexpected TransferShares burn; protocol review required")
            elif event == "SharesBurnt":
                s["total_shares_wei"] -= words(log, 3)[2]
            elif event == "ExternalSharesMinted":
                s["external_shares_wei"] += words(log, 1)[0]
            elif event == "ExternalSharesBurnt":
                # Rebalance/debt internalization do NOT burn total shares; ordinary
                # external burning already emitted SharesBurnt. Never double count.
                s["external_shares_wei"] -= words(log, 1)[0]
            elif event == "ETHDistributed":
                d = words(log, 5)
                if s["core_buffer_wei"] + d[2] + d[3] - finalizations.get(tx, 0) != d[4]:
                    raise ExecutionError("ETHDistributed buffer transition does not reconcile")
                s["core_buffer_wei"] = d[4]
                ref = integer(log["topics"][1])
                if ref <= s["last_report_timestamp"] or ref >= timestamp:
                    raise ExecutionError("Nonmonotonic/invalid accounting report timestamp")
                s["last_report_timestamp"] = ref
                s["last_report_transaction"] = tx
            elif event == "TokenRebased":
                d = words(log, 6)
                if validate_reports and (s["total_shares_wei"] != d[3] or _totals(s)[2] != d[4]):
                    raise ExecutionError("TokenRebased totals disagree with integer replay")
            elif event == "InternalShareRateUpdated":
                d = words(log, 3)
                if validate_reports and _totals(s)[:2] != tuple(d[:2]):
                    raise ExecutionError("InternalShareRateUpdated disagrees with integer replay")
            elif event not in _PASSIVE_CORE | _PASSIVE_COMMON:
                raise ExecutionError(f"Unsupported core event {event or log.get('topics')}; re-audit required")
        elif source == "queue":
            if event == "WithdrawalRequested":
                if tx in report_txs:
                    raise ExecutionError("Request in report transaction has an unobservable reportTimestamp boundary")
                d = words(log, 2)
                if len(log["topics"]) != 4:
                    raise ExecutionError("Malformed WithdrawalRequested topics")
                request_id = integer(log["topics"][1])
                if request_id != s["last_request_id"] + 1:
                    raise ExecutionError("Noncontiguous WithdrawalRequested ID")
                s["pending_requests"].append({"request_id": request_id, "amount_steth_wei": str(d[0]),
                    "amount_shares_wei": str(d[1]), "timestamp": timestamp, "report_timestamp": s["last_report_timestamp"],
                    "block": key(log)[0], "block_hash": log["blockHash"], "transaction_hash": tx,
                    "transaction_index": key(log)[1], "log_index": key(log)[2]})
                s["last_request_id"] = request_id
            elif event == "WithdrawalsFinalized":
                d = words(log, 3)
                if len(log["topics"]) != 3:
                    raise ExecutionError("Malformed WithdrawalsFinalized topics")
                first, last = map(integer, log["topics"][1:])
                if first != s["last_finalized_id"] + 1 or not first <= last <= s["last_request_id"]:
                    raise ExecutionError("Invalid/noncontiguous finalization frontier")
                count = last - first + 1
                rows = s["pending_requests"][:count]
                if len(rows) != count or integer(rows[-1]["request_id"]) != last:
                    raise ExecutionError("Finalization refers to missing queue rows")
                nominal = sum(integer(r["amount_steth_wei"]) for r in rows)
                shares = sum(integer(r["amount_shares_wei"]) for r in rows)
                if d[0] > nominal or d[1] != shares or d[2] != timestamp:
                    raise ExecutionError("Finalization amount/shares/timestamp mismatch")
                del s["pending_requests"][:count]
                s["last_finalized_id"] = last
                finalizations[tx] = finalizations.get(tx, 0) + d[0]
            elif event == "Paused":
                duration = words(log, 1)[0]
                s["queue_resume_since"] = UINT256_MAX if duration == UINT256_MAX else timestamp + duration
            elif event == "Resumed":
                words(log, 0)
                s["queue_resume_since"] = timestamp
            elif event == "BunkerModeEnabled":
                s["queue_bunker_since"] = words(log, 1)[0]
            elif event == "BunkerModeDisabled":
                words(log, 0)
                s["queue_bunker_since"] = UINT256_MAX
            elif event not in _PASSIVE_COMMON | {"WithdrawalClaimed", "BatchMetadataUpdate", "BaseURISet", "NftDescriptorAddressSet"}:
                raise ExecutionError(f"Unsupported queue event {event or log.get('topics')}; re-audit required")
        elif source == "consensus":
            if event == "FrameConfigSet":
                initial, epochs = words(log, 2)
                if epochs == 0:
                    raise ExecutionError("Zero epochs per frame")
                s["config"].update(initial_epoch=initial, epochs_per_frame=epochs)
                s["config"]["frame_evidence"] = {"quality": "event_replayed", "block": key(log)[0], "tx": tx}
            elif event == "FastLaneConfigSet":
                s["config"]["fast_lane_length_slots"] = words(log, 1)[0]
            elif event not in _PASSIVE_COMMON | _PASSIVE_CONSENSUS:
                raise ExecutionError(f"Unsupported HashConsensus event {event or log.get('topics')}")
        elif source == "sanity":
            if event == "RequestTimestampMarginSet":
                s["config"]["request_timestamp_margin"] = words(log, 1)[0]
                s["config"]["margin_evidence"] = {"quality": "event_replayed", "block": key(log)[0], "tx": tx}
            elif event not in _PASSIVE_COMMON | {"NegativeCLRebaseConfirmed"}:
                raise ExecutionError(f"Sanity limits changed or unknown event {event or log.get('topics')}; re-audit required")
        elif source == "oracle":
            if event not in _PASSIVE_COMMON | _PASSIVE_ORACLE:
                raise ExecutionError(f"Unsupported AccountingOracle event {event or log.get('topics')}")
        elif source in ("kernel", "locator"):
            if event not in _PASSIVE_COMMON:
                raise ExecutionError(f"Governance/locator event {event or log.get('topics')}; re-audit required")
        else:
            raise ExecutionError("Unrecognized monitored event source")
        if any(value < 0 for field, value in s.items() if field.endswith("_wei")):
            raise ExecutionError("Negative accounting value during replay")
    return _serial_state(s)


def report_timing(config, timestamp, last_report_timestamp):
    """HashConsensus frame reference slot is start_slot - 1 (not wall-clock noon)."""
    c = {k: integer(config[k]) for k in ("genesis_time", "seconds_per_slot", "slots_per_epoch",
         "epochs_per_frame", "initial_epoch", "request_timestamp_margin", "fast_lane_length_slots")}
    if not c["epochs_per_frame"] or not c["seconds_per_slot"] or not c["slots_per_epoch"]:
        raise ExecutionError("Invalid frame chain configuration")
    epoch = (timestamp - c["genesis_time"]) // (c["seconds_per_slot"] * c["slots_per_epoch"])
    if epoch < c["initial_epoch"]:
        raise ExecutionError("HashConsensus initial epoch is still in the future")
    index = (epoch - c["initial_epoch"]) // c["epochs_per_frame"]
    refslot = (c["initial_epoch"] + index * c["epochs_per_frame"]) * c["slots_per_epoch"] - 1
    frame_duration = c["epochs_per_frame"] * c["slots_per_epoch"] * c["seconds_per_slot"]
    ref = c["genesis_time"] + refslot * c["seconds_per_slot"]
    # If the current frame's report has not arrived, it remains a possible next
    # publication even though its reference timestamp is in the past.
    next_unreported_ref = ref if last_report_timestamp < ref else ref + frame_duration
    return dict(c, frame_duration_seconds=frame_duration, current_ref_slot=refslot,
                current_ref_timestamp=ref, next_ref_timestamp=next_unreported_ref,
                next_calendar_ref_timestamp=ref + frame_duration,
                request_timestamp_margin_seconds=c["request_timestamp_margin"],
                quality=config.get("quality", "unknown"), evidence=copy.deepcopy(config.get("evidence", {})),
                frame_evidence=copy.deepcopy(config.get("frame_evidence", {})),
                margin_evidence=copy.deepcopy(config.get("margin_evidence", {})))


def _await_finality(rpc, child, *, wait_seconds, poll_seconds, sleep, monotonic):
    """Wait only on successful lagging finalized-head reads, never on RPC errors."""
    if wait_seconds < 0 or poll_seconds <= 0:
        raise ExecutionError("Invalid finality wait bounds")
    start = monotonic()
    deadline = min(start + wait_seconds, getattr(rpc, "deadline", float("inf")) - 120)
    polls = 0
    while True:
        finalized = rpc("eth_getBlockByNumber", ["finalized", False])
        polls += 1
        if not isinstance(finalized, dict):
            raise ExecutionError("Missing finalized execution header")
        height = integer(finalized.get("number"))
        _hash(finalized.get("hash"))
        if height >= integer(child["number"]):
            if height == integer(child["number"]) and finalized["hash"].lower() != child["hash"].lower():
                raise ExecutionError("Finalized child hash disagrees with canonical child")
            return finalized, {"polls": polls, "wait_seconds": max(0, monotonic() - start)}
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise ExecutionError("Execution state and authentication child are not finalized after bounded same-source wait")
        sleep(min(poll_seconds, remaining))


def collect_at(state, block, block_hash, rpc, *, chunk_blocks=1000, max_calls=256, result_cap=10000,
               finality_wait_seconds=768, finality_poll_seconds=30, sleep=time.sleep, monotonic=time.monotonic):
    """Collect all EL quantities at the orchestrator's BeaconState execution block.

    Returns a NEW durable state only after target/seed rechecks. Exceptions leave
    caller's input untouched. Configuration assumptions remain in validation.
    """
    s = _numeric_state(state)
    _validate(s)
    block, block_hash = integer(block), _hash(block_hash)
    if block < s["block"]:
        raise ExecutionError("BeaconState execution block is older than durable checkpoint")
    seed_header = _header(rpc, s["block"])
    if seed_header["hash"].lower() != _hash(s["hash"]):
        raise ExecutionError("Durable execution checkpoint is no longer canonical")
    target = _header(rpc, block)
    if target["hash"].lower() != block_hash:
        raise ExecutionError("BeaconState execution block hash does not match RPC")
    child = _header(rpc, block + 1)
    if integer(child["timestamp"]) <= integer(target["timestamp"]):
        raise ExecutionError("Execution child timestamp must follow the target block")
    if child.get("parentHash", "").lower() != block_hash:
        raise ExecutionError("Execution child does not extend the BeaconState execution block")
    finalized, finality_wait = _await_finality(rpc, child, wait_seconds=finality_wait_seconds,
                                               poll_seconds=finality_poll_seconds, sleep=sleep, monotonic=monotonic)
    if finality_wait["polls"] > 1:
        # Nothing mutable has been replayed yet. Reauthenticate the exact chosen
        # target and child after waiting before reading any logs/balances.
        for height, expected in ((block, block_hash), (block + 1, child["hash"])):
            if _header(rpc, height)["hash"].lower() != expected.lower():
                raise ExecutionError("Execution anchor changed during finality wait")
    if integer(target["timestamp"]) < s["timestamp"]:
        raise ExecutionError("Nonmonotonic execution timestamp")
    logs, coverage = fetch_logs(rpc, s["block"] + 1, block, chunk_blocks=chunk_blocks,
                                max_calls=max_calls, result_cap=result_cap)
    headers = {s["block"]: seed_header, block: target}
    observed_hashes = {}
    timestamp_events = {TOPICS[n] for n in ("WithdrawalRequested", "WithdrawalsFinalized", "Paused", "Resumed", "ETHDistributed")}
    for log in logs:
        height = key(log)[0]
        old_hash = observed_hashes.setdefault(height, log["blockHash"].lower())
        if old_hash != log["blockHash"].lower():
            raise ExecutionError("Conflicting hashes for logs from the same block")
        # Logs are provider-attested, not receipt-trie proofs. Authenticate headers
        # where timestamps affect queue/report state; token transfers do not need
        # hundreds of extra header calls per hour. Seed/target hash sandwiches
        # reject reorgs across the collected finalized interval.
        needs_header = log.get("topics", [None])[0] in timestamp_events
        if needs_header and height not in headers:
            headers[height] = _header(rpc, height)
        if height in headers:
            h = headers[height]
            if log["blockHash"].lower() != h["hash"].lower():
                raise ExecutionError("Log block hash mismatches canonical header")
            ts = integer(h["timestamp"])
            if "blockTimestamp" in log and integer(log["blockTimestamp"]) != ts:
                raise ExecutionError("Log timestamp mismatches canonical header")
            log["blockTimestamp"] = hex(ts)
        elif "blockTimestamp" in log:
            ts = integer(log["blockTimestamp"])
            if not s["timestamp"] <= ts <= integer(target["timestamp"]):
                raise ExecutionError("Log timestamp outside the checkpoint interval")
    new = _numeric_state(replay_events(s, logs))
    new.update(block=block, hash=block_hash, timestamp=integer(target["timestamp"]))
    _validate(new)
    if new["timestamp"] < new["queue_resume_since"]:
        raise ExecutionError("Withdrawal queue is paused; normal-mode scenarios are disabled")
    if new["queue_bunker_since"] != UINT256_MAX:
        raise ExecutionError("Withdrawal queue bunker mode is active; normal-mode scenarios are disabled")
    balance = {k: integer(rpc("eth_getBalance", [ADDRESSES[k], hex(block)]))
               for k in ("core", "el_vault", "withdrawal_vault")}
    if balance["core"] < new["core_buffer_wei"]:
        raise ExecutionError("Physical core balance is below the replayed accounting buffer")
    for height, expected in ((s["block"], s["hash"]), (block, block_hash), (block + 1, child["hash"])):
        if _header(rpc, height)["hash"].lower() != expected.lower():
            raise ExecutionError("Execution chain changed while collecting pinned inputs")
    internal_shares, internal_ether, total_ether = _totals(new)
    pending_steth = sum(integer(r["amount_steth_wei"]) for r in new["pending_requests"])
    pending_shares = sum(integer(r["amount_shares_wei"]) for r in new["pending_requests"])
    reserve = min(new["core_buffer_wei"], new["reserve_stored_wei"])
    available = new["core_buffer_wei"] - reserve + balance["el_vault"] + balance["withdrawal_vault"]
    timing = report_timing(new["config"], new["timestamp"], new["last_report_timestamp"])
    config_quality = new["config"].get("quality", "unknown")
    if config_quality not in ("conditional_unpinned_seed", "block_pinned"):
        raise ExecutionError("Missing configuration provenance; provide an authorized pinned configuration seed")
    snapshot = {
        "snapshot_block": block, "snapshot_hash": block_hash, "snapshot_time_utc": _utc(new["timestamp"]),
        "last_request_id": new["last_request_id"], "last_finalized_id": new["last_finalized_id"],
        "pending_request_count": len(new["pending_requests"]), "missing_pending_ids": [],
        "pending_original_steth_wei": str(pending_steth), "pending_shares_wei": str(pending_shares),
        "core_buffer_replayed_wei": str(new["core_buffer_wei"]), "core_physical_balance_wei": str(balance["core"]),
        "replay_minus_physical_wei": str(new["core_buffer_wei"] - balance["core"]),
        "unaccounted_physical_core_wei": str(balance["core"] - new["core_buffer_wei"]),
        "deposit_reserve_stored_wei": str(new["reserve_stored_wei"]), "deposit_reserve_target_wei": str(new["reserve_target_wei"]),
        "effective_deposit_reserve_wei": str(reserve), "core_after_deposit_reserve_wei": str(new["core_buffer_wei"] - reserve),
        "el_vault_balance_wei": str(balance["el_vault"]), "withdrawal_vault_balance_wei": str(balance["withdrawal_vault"]),
        "sum_current_physical_cash_wei": str(sum(balance.values())), "scenario_available_cash_wei": str(available),
        "total_shares_wei": str(new["total_shares_wei"]), "total_pooled_ether_wei": str(total_ether),
        "internal_shares_wei": str(internal_shares), "internal_ether_wei": str(internal_ether),
        "external_shares_wei": str(new["external_shares_wei"]), "internal_share_rate_e27": str(internal_ether * E27 // internal_shares),
        "queue_paused": new["timestamp"] < new["queue_resume_since"],
        "queue_bunker_active": new["queue_bunker_since"] != UINT256_MAX,
        "configuration_quality": config_quality,
        "limitations": ["Physical vault cash is not guaranteed next-report admissible funding; scenario assumes all listed vault cash is admitted.",
                        "Forced/unaccounted ETH at core is excluded from the accounting buffer and scenario budget.",
                        "Configuration seed was historically unpinned unless configuration_quality says block_pinned; subsequent event replay does not remove that condition."],
    }
    validation = {"complete": True, "execution_complete": True, "same_block_inputs": True,
                  "queue_contiguous": True, "core_buffer_reconciles": balance["core"] == new["core_buffer_wei"],
                  "configuration_quality": config_quality, "conditional": config_quality != "block_pinned",
                  "event_count": len(logs), "header_count": len(headers) + 4 + finality_wait["polls"] + (2 if finality_wait["polls"] > 1 else 0),
                  "finality_wait": finality_wait, "log_integrity": "provider-attested logs with seed/target/child header rechecks; no receipt-trie proof", "coverage": coverage, "blockers": []}
    return {"state": _serial_state(new), "snapshot": snapshot, "report_timing": timing,
            "execution_anchor": {"block": block, "hash": block_hash, "timestamp": new["timestamp"],
                                 "state_block": target, "child_block": child, "finalized_block": finalized},
            "validation": validation}


def build_seed(audit_dir):
    """Reproduce owner-free 26120128 checkpoint from the provided immutable audit.

    Rate state starts from the exact report receipt, then replays EVERY later
    core event through the fixed snapshot, including same-block later txs.
    It never substitutes the historical explorer's totalShares/totalPooledEther.
    """
    root = Path(audit_dir)
    read = lambda name: json.loads((root / name).read_text())
    snapshot = read("fresh_fixed_snapshot.json")
    pending = read("fresh_pending_requests.json")
    report = read("current_report_decoded.json")
    receipt = read("raw/rpc/latest_report_receipt.json")["response"]["result"]
    ui = read("explorer/findings.json")
    if integer(receipt["status"]) != 1 or receipt["transactionHash"].lower() != report["transaction_hash"].lower():
        raise ExecutionError("Invalid audited report receipt")
    if integer(receipt["blockNumber"]) != report["block"] or receipt["blockHash"].lower() != report["block_hash"].lower():
        raise ExecutionError("Report receipt/header mismatch")
    core = [l for l in receipt["logs"] if l["address"].lower() == ADDRESSES["core"]]
    def last(name):
        return [l for l in core if l["topics"][0] == TOPICS[name]][-1]
    rebased, internal = words(last("TokenRebased"), 6), words(last("InternalShareRateUpdated"), 3)
    buffer = words(last("ETHDistributed"), 5)[4]
    config = {"genesis_time": ui["hashConsensus"]["getChainConfig"]["genesisTime"],
              "seconds_per_slot": ui["hashConsensus"]["getChainConfig"]["secondsPerSlot"],
              "slots_per_epoch": ui["hashConsensus"]["getChainConfig"]["slotsPerEpoch"],
              "initial_epoch": ui["hashConsensus"]["getFrameConfig"]["initialEpoch"],
              "epochs_per_frame": ui["hashConsensus"]["getFrameConfig"]["epochsPerFrame"],
              "fast_lane_length_slots": ui["hashConsensus"]["getFrameConfig"]["fastLaneLengthSlots"],
              "request_timestamp_margin": ui["sanityChecker"]["getOracleReportLimits"]["requestTimestampMargin"],
              "quality": "conditional_unpinned_seed",
              "evidence": {"source": "historical public explorer UI audit; not block pinned",
                           "observations": [o for o in ui["observations"] if o["name"] in ("consensus", "sanity", "queue", "locator")]}}
    # The fixed ledger is already after the report. It is installed only after
    # reconstructing core quantities, so no queue finalization can be applied twice.
    s = {"schema": 1, "block": report["block"], "hash": report["block_hash"],
         "timestamp": report["publication_timestamp"], "pending_requests": [],
         "last_request_id": 0, "last_finalized_id": 0, "last_report_timestamp": report["reference_timestamp"],
         "last_report_transaction": report["transaction_hash"], "core_buffer_wei": buffer,
         "cl_balance_wei": sum(words(last("CLBalancesUpdated"), 2)),
         "deposited_post_report_wei": words(last("DepositedPostReportUpdated"), 1)[0],
         "total_shares_wei": rebased[3], "external_shares_wei": rebased[3] - internal[0],
         "reserve_stored_wei": 0, "reserve_target_wei": 0,
         "queue_resume_since": ui["queue"]["getResumeSinceTimestamp"],
         "queue_bunker_since": UINT256_MAX if not report["report_data"]["isBunkerMode"] else integer(ui["queue"]["bunkerModeSinceTimestamp"]),
         "config": config}
    if _totals(s) != (internal[0], internal[1], rebased[4]):
        raise ExecutionError("Audited report internal/total balances do not reconcile")
    # Reserve values are restored from actual events, not hardcoded/UI estimates.
    reserve_events = []
    for path in sorted((root / "raw/aws").glob("*_lido_logs.jsonl")):
        for line in path.read_text().splitlines():
            l = json.loads(line)
            if (l["address"].lower() == ADDRESSES["core"] and l.get("topics") and
                l["topics"][0] in (TOPICS["DepositsReserveSet"], TOPICS["DepositsReserveTargetSet"]) and
                l["block_number"] <= report["block"]):
                reserve_events.append(l)
    found = set()
    for l in sorted(reserve_events, key=lambda r: (r["block_number"], r["log_index"])):
        field = "reserve_stored_wei" if l["topics"][0] == TOPICS["DepositsReserveSet"] else "reserve_target_wei"
        s[field] = words(l, 1)[0]
        found.add(field)
    if found != {"reserve_stored_wei", "reserve_target_wei"}:
        raise ExecutionError("Audited reserve event anchors are missing")
    core_name = ADDRESSES["core"] + "_26109778_26119777.json"
    historical = read("raw/rpc/" + core_name)
    fresh = read("raw/rpc/fresh_core_logs.json")
    if integer(historical["request"]["params"][0]["toBlock"]) + 1 != integer(fresh["request"]["params"][0]["fromBlock"]):
        raise ExecutionError("Gap between audited core log ranges")
    if integer(fresh["request"]["params"][0]["toBlock"]) != snapshot["snapshot_block"]:
        raise ExecutionError("Audited logs do not reach fixed snapshot")
    cutoff = max(key(l) for l in core)
    logs = [l for l in historical["response"]["result"] + fresh["response"]["result"] if key(l) > cutoff]
    if any(integer(l["blockNumber"]) > snapshot["snapshot_block"] for l in logs):
        raise ExecutionError("Audited core logs exceed seed block")
    s = _numeric_state(replay_events(s, logs))
    if s["core_buffer_wei"] != integer(snapshot["core_buffer_replayed_wei"]):
        raise ExecutionError("Rebuilt audit buffer differs from fixed snapshot")
    # Replay queue mode events after report. Do not replay already-seeded amounts.
    qlogs = read("raw/rpc/" + ADDRESSES["queue"] + "_26109778_26119777.json")["response"]["result"]
    qlogs += read("raw/rpc/fresh_queue_logs.json")["response"]["result"]
    mode_topics = {TOPICS[n] for n in ("Paused", "Resumed", "BunkerModeEnabled", "BunkerModeDisabled")}
    s = _numeric_state(replay_events(s, [l for l in qlogs if key(l) > cutoff and l["topics"][0] in mode_topics]))
    kept = ("request_id", "amount_steth_wei", "amount_shares_wei", "block", "block_hash", "timestamp",
            "transaction_hash", "transaction_index", "log_index", "report_timestamp")
    s["pending_requests"] = [{k: r[k] for k in kept if k in r} for r in pending]
    s.update(block=snapshot["snapshot_block"], hash=snapshot["snapshot_hash"],
             timestamp=int(dt.datetime.fromisoformat(snapshot["snapshot_time_utc"]).timestamp()),
             last_request_id=snapshot["last_request_id"], last_finalized_id=snapshot["last_finalized_id"])
    source_files = ["fresh_fixed_snapshot.json", "fresh_pending_requests.json", "current_report_decoded.json",
                    "raw/rpc/latest_report_receipt.json", "raw/rpc/" + core_name, "raw/rpc/fresh_core_logs.json",
                    "explorer/findings.json"]
    s["provenance"] = {"kind": "reproduced_public_audit", "seed_block": s["block"], "seed_hash": s["hash"],
                       "sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in source_files},
                       "rate_source": "TokenRebased/InternalShareRateUpdated receipt plus complete subsequent core event replay",
                       "configuration_condition": "Unpinned historical UI values remain conditional; subsequent monitoring does not pin the seed"}
    _validate(s)
    if sum(integer(r["amount_steth_wei"]) for r in pending) != integer(snapshot["pending_original_steth_wei"]):
        raise ExecutionError("Seed queue nominal sum differs from audit snapshot")
    if sum(integer(r["amount_shares_wei"]) for r in pending) != integer(snapshot["pending_shares_wei"]):
        raise ExecutionError("Seed queue share sum differs from audit snapshot")
    return _serial_state(s)
