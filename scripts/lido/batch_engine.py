"""Integer-only Lido FIFO scenario engine, source parity: core v4.0.1 / oracle 8.1.0.

Reference: batch_rules.md; WithdrawalQueueBase.sol lines 215-327, 534-542.
This is local scenario analysis, NOT an on-chain report simulator or validation.
Every report budget must already be eligible after accounting, rebase, reserve,
and nonzero-internal-share guards. No inflow model or automatic carryover exists.
"""
from dataclasses import dataclass, field, replace
from typing import Mapping, Sequence

E27 = 10**27
MAX_BATCHES_LENGTH = 36
UINT256_MAX = 2**256 - 1
UINT128_MAX = 2**128 - 1


def _uint(value: int, name: str, *, positive: bool = False) -> int:
    if type(value) is not int or not (int(positive) <= value <= UINT256_MAX):
        raise ValueError(f"{name} must be a {'positive ' if positive else ''}uint256 integer")
    return value


def _mul_div(x: int, y: int, denominator: int) -> int:
    """Solidity uint256 checked multiplication followed by floor division."""
    return _uint(x * y, 'multiplication result') // denominator


def _integer_field(value, name):
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    raise ValueError(f'{name} must be an integer or exact unsigned decimal string')


@dataclass(frozen=True)
class Request:
    request_id: int
    amount_steth_wei: int
    amount_shares_wei: int
    timestamp: int
    report_timestamp: int

    def __post_init__(self):
        for name in ('request_id', 'amount_steth_wei', 'amount_shares_wei'):
            _uint(getattr(self, name), name, positive=True)
        if max(self.amount_steth_wei, self.amount_shares_wei) > UINT128_MAX:
            raise ValueError('request amount cannot exceed uint128 cumulative queue storage')
        for name in ('timestamp', 'report_timestamp'):
            _uint(getattr(self, name), name)
        if self.report_timestamp > self.timestamp:
            raise ValueError('request report_timestamp cannot be later than its creation timestamp')

    @classmethod
    def from_mapping(cls, row: Mapping):
        """Load exact integer CSV/JSON ledger fields; report_timestamp is mandatory."""
        return cls(**{key: _integer_field(row[key], key) for key in cls.__dataclass_fields__})

    @property
    def rate(self):
        return _mul_div(self.amount_steth_wei, E27, self.amount_shares_wei)

    def cost(self, max_share_rate: int):
        # Do NOT use min(stETH, shares * rate // E27): rounded comparison matters.
        return (_mul_div(self.amount_shares_wei, max_share_rate, E27)
                if self.rate > max_share_rate else self.amount_steth_wei)


@dataclass
class BatchState:
    remaining_eth_budget: int
    finished: bool = False
    batches: list[int] = field(default_factory=lambda: [0] * MAX_BATCHES_LENGTH)
    batches_length: int = 0
    stop_reason: str | None = None  # diagnostic only, not part of the ABI state

    def as_tuple(self):
        return (self.remaining_eth_budget, self.finished, list(self.batches), self.batches_length)


class Queue:
    """A contiguous ordered queue suffix beginning at last_finalized_id + 1.

    Earlier finalized request records aren't needed: all supplied amounts are
    differences of cumulative values. report_timestamp is needed for merging.
    """
    def __init__(self, requests: Sequence[Request], last_finalized_id: int = 0):
        self.last_finalized_id = _uint(last_finalized_id, 'last_finalized_id')
        self.requests = tuple(requests)
        for offset, request in enumerate(self.requests, 1):
            if not isinstance(request, Request):
                raise TypeError('requests must contain Request instances')
            if request.request_id != self.last_finalized_id + offset:
                raise ValueError('queue must be ordered, contiguous, and start at last_finalized_id + 1')
            if offset > 1 and request.timestamp < self.requests[offset - 2].timestamp:
                raise ValueError('request timestamps must be nondecreasing')
        if max(sum(r.amount_steth_wei for r in self.requests),
               sum(r.amount_shares_wei for r in self.requests)) > UINT128_MAX:
            raise ValueError('queue suffix cannot exceed uint128 cumulative queue storage')
        self.last_request_id = _uint(self.last_finalized_id + len(self.requests), 'last_request_id')
        self.by_id = {r.request_id: r for r in self.requests}

    def calculate_finalization_batches(self, max_share_rate: int, max_timestamp: int,
                                       max_requests_per_call: int, state: BatchState) -> BatchState:
        """One canonical view-call-equivalent step, including its 37th-segment debit.

        A zero rate is accepted here because the Solidity view accepts it;
        prefinalize and high-level report analysis reject zero-rate finalization.
        """
        _uint(max_share_rate, 'max_share_rate')
        _uint(max_timestamp, 'max_timestamp')
        _uint(max_requests_per_call, 'max_requests_per_call', positive=True)
        _uint(state.remaining_eth_budget, 'remaining_eth_budget')
        if state.finished or state.remaining_eth_budget == 0:
            raise ValueError('InvalidState: finished or zero remaining budget')
        if len(state.batches) != MAX_BATCHES_LENGTH or not 0 <= state.batches_length <= MAX_BATCHES_LENGTH:
            raise ValueError('invalid fixed-size batch state')
        state = replace(state, batches=list(state.batches), stop_reason=None)
        if state.batches_length == 0:
            current_id = self.last_finalized_id + 1
            previous = None
            previous_rate = 0
        else:
            previous = self.by_id[state.batches[state.batches_length - 1]]
            current_id = previous.request_id + 1
            previous_rate = previous.rate
        next_call_request_id = current_id + max_requests_per_call
        queue_length = self.last_request_id + 1
        while current_id < queue_length and current_id < next_call_request_id:
            request = self.by_id[current_id]
            if request.timestamp > max_timestamp:
                state.stop_reason = 'timestamp'
                break
            cost = request.cost(max_share_rate)
            if cost > state.remaining_eth_budget:
                state.stop_reason = 'budget'
                break
            state.remaining_eth_budget -= cost
            merge = state.batches_length != 0 and (
                previous.report_timestamp == request.report_timestamp
                or previous_rate <= max_share_rate and request.rate <= max_share_rate
                or previous_rate > max_share_rate and request.rate > max_share_rate
            )
            if merge:
                state.batches[state.batches_length - 1] = current_id
            else:
                # Canonical source checks cap AFTER the request's cost was debited.
                if state.batches_length == MAX_BATCHES_LENGTH:
                    state.stop_reason = 'max_batches'
                    break
                state.batches[state.batches_length] = current_id
                state.batches_length += 1
            previous_rate, previous = request.rate, request
            current_id += 1
        state.finished = current_id == queue_length or current_id < next_call_request_id
        if state.stop_reason is None:
            state.stop_reason = 'queue_end' if current_id == queue_length else 'chunk_boundary'
        return state

    def prefinalize(self, batches: Sequence[int], max_share_rate: int) -> tuple[int, int]:
        """Canonical aggregate-batch branch and rounding: returns ETH and shares."""
        _uint(max_share_rate, 'max_share_rate', positive=True)
        if not batches:
            raise ValueError('EmptyBatches')
        previous_id = self.last_finalized_id
        eth_to_lock = shares_to_move = 0
        for endpoint in batches:
            _uint(endpoint, 'batch endpoint', positive=True)
            if endpoint <= previous_id or endpoint > self.last_request_id:
                raise ValueError('invalid or unsorted batch endpoint')
            batch = (self.by_id[i] for i in range(previous_id + 1, endpoint + 1))
            steth = shares = 0
            for request in batch:
                steth += request.amount_steth_wei
                shares += request.amount_shares_wei
            batch_rate = _mul_div(steth, E27, shares)
            eth_to_lock += _mul_div(shares, max_share_rate, E27) if batch_rate > max_share_rate else steth
            shares_to_move += shares
            previous_id = endpoint
        return eth_to_lock, shares_to_move

    def calculate_batches(self, eligible_budget_wei: int, safe_timestamp: int,
                          max_share_rate: int, *, max_requests_per_call: int = 1000,
                          paused: bool = False) -> dict:
        """Plan a report using an already-guarded budget; no external state reads."""
        _uint(eligible_budget_wei, 'eligible_budget_wei')
        _uint(safe_timestamp, 'safe_timestamp')
        _uint(max_share_rate, 'max_share_rate', positive=True)
        _uint(max_requests_per_call, 'max_requests_per_call', positive=True)
        state = BatchState(eligible_budget_wei)
        calls = 0
        if paused:
            state.finished, state.stop_reason = True, 'paused'
        elif not self.requests:
            state.finished, state.stop_reason = True, 'queue_end'
        elif eligible_budget_wei == 0:
            state.finished, state.stop_reason = True, 'zero_budget'
        else:
            # Oracle 8.1.0 prevents a zero-budget re-call at a chunk boundary.
            while not state.finished and state.remaining_eth_budget > 0:
                state = self.calculate_finalization_batches(max_share_rate, safe_timestamp,
                                                            max_requests_per_call, state)
                calls += 1
        endpoints = state.batches[:state.batches_length]
        eth_to_lock, shares = self.prefinalize(endpoints, max_share_rate) if endpoints else (0, 0)
        return {
            'batches': endpoints,
            'candidate_last_request_id': endpoints[-1] if endpoints else self.last_finalized_id,
            'eth_to_lock_wei': eth_to_lock,
            'new_queue_shares_wei': shares,
            'eligible_budget_wei': eligible_budget_wei,
            'prefinalize_fits_budget': eth_to_lock <= eligible_budget_wei,
            'remaining_batch_state_budget_wei': state.remaining_eth_budget,
            'actual_budget_remainder_wei': eligible_budget_wei - eth_to_lock,
            'canonical_state_finished': state.finished,
            'stop_reason': state.stop_reason,
            'stopped_with_zero_budget': state.remaining_eth_budget == 0,
            'view_calls': calls,
        }


@dataclass(frozen=True)
class ReportScenario:
    """Inputs are explicit already-guarded per-report available budgets, not inflows.

    Repeated budgets are independent user-supplied scenarios; unused ETH is NOT
    carried forward automatically. visible_last_request_id optionally specifies
    the queue at that reference block; omitted means the whole supplied ledger.
    """
    label: str
    eligible_budget_wei: int
    safe_timestamp: int
    max_share_rate: int
    paused: bool = False
    visible_last_request_id: int | None = None
    max_requests_per_call: int = 1000


def simulate_reports(requests: Sequence[Request], reports: Sequence[ReportScenario], *,
                     last_finalized_id: int = 0,
                     tiers: Mapping[str, Sequence[int]] | None = None) -> dict:
    """Run explicit ordered scenarios; full tier completion uses its LAST request.

    A tier is a named nonempty list of constituent request IDs (e.g. 1000+500 ETH).
    A prefinalize aggregate-rounding funding failure is reported and does not
    advance the frontier. No future report is described as on-chain validated.
    """
    ledger = Queue(requests, last_finalized_id)
    frontier = last_finalized_id
    tier_results = {}
    for name, ids in (tiers or {}).items():
        ids = list(ids)
        if not ids or len(set(ids)) != len(ids):
            raise ValueError('tier request IDs must be nonempty and unique')
        if any(type(i) is not int or not 0 < i <= ledger.last_request_id for i in ids):
            raise ValueError('tier references invalid or unknown future IDs')
        tier_results[name] = {
            'request_ids': sorted(ids), 'last_request_id': max(ids),
            'already_complete_at_start': max(ids) <= frontier,
            'first_finalization_report_index': None,
            'full_completion_report_index': None,
            'full_completion_report_label': None,
        }
    outcomes = []
    labels = set()
    previous_visible = last_finalized_id
    for index, report in enumerate(reports):
        if not isinstance(report, ReportScenario):
            raise TypeError('reports must contain ReportScenario instances')
        if not report.label or report.label in labels:
            raise ValueError('report labels must be nonempty and unique')
        labels.add(report.label)
        visible = ledger.last_request_id if report.visible_last_request_id is None else report.visible_last_request_id
        _uint(visible, 'visible_last_request_id')
        if not max(frontier, previous_visible) <= visible <= ledger.last_request_id:
            raise ValueError('visible queue frontier must be nondecreasing and cover prior finalizations')
        previous_visible = visible
        queue = Queue([r for r in ledger.requests if frontier < r.request_id <= visible], frontier)
        outcome = queue.calculate_batches(report.eligible_budget_wei, report.safe_timestamp,
                                          report.max_share_rate, paused=report.paused,
                                          max_requests_per_call=report.max_requests_per_call)
        previous_frontier = frontier
        funding_valid = outcome['prefinalize_fits_budget']
        if funding_valid:
            frontier = outcome['candidate_last_request_id']
        outcome.update(report_label=report.label, report_index=index,
                       previous_last_finalized_id=previous_frontier,
                       last_finalized_id=frontier,
                       finalization_accepted_in_scenario=funding_valid,
                       guard_failure=None if funding_valid else 'aggregate_prefinalize_exceeds_eligible_budget')
        outcomes.append(outcome)
        for tier in tier_results.values():
            if tier['first_finalization_report_index'] is None and any(
                    previous_frontier < i <= frontier for i in tier['request_ids']):
                tier['first_finalization_report_index'] = index
            if (not tier['already_complete_at_start']
                    and tier['full_completion_report_index'] is None
                    and tier['last_request_id'] <= frontier):
                tier['full_completion_report_index'] = index
                tier['full_completion_report_label'] = report.label
    for tier in tier_results.values():
        tier['complete_by_end'] = tier['last_request_id'] <= frontier
    return {
        'status': 'explicit local scenario; not future on-chain validation',
        'budget_semantics': 'supplied eligible budgets already after accounting/rebase/reserve/internal-share guards; no automatic inflow or carryover',
        'last_finalized_id': frontier, 'reports': outcomes, 'tiers': tier_results,
    }
