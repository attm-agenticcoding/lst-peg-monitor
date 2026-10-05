"""Exact, conditional FIFO scenarios. No future observed data enters this module."""
from datetime import datetime, timezone
from batch_engine import Request, Queue

WEI = 10**18
TIERS = (100, 200, 300, 500, 1000, 1500)


def utc(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace('+00:00', 'Z')


def eth(wei):
    return f'{wei // WEI}.{wei % WEI:018d}'


def report_references(timestamp, timing, count=14):
    """All upcoming frame reference slots, including those too young for a request."""
    genesis = int(timing['genesis_time'])
    slot_seconds = int(timing['seconds_per_slot'])
    epoch_slots = int(timing['slots_per_epoch'])
    frame_epochs = int(timing['epochs_per_frame'])
    initial_epoch = int(timing['initial_epoch'])
    margin = int(timing['request_timestamp_margin'])
    if slot_seconds != 12 or epoch_slots != 32 or frame_epochs <= 0 or margin < 0:
        raise ValueError('unsupported report timing configuration')
    epoch_seconds = slot_seconds * epoch_slots
    base = genesis + (initial_epoch * epoch_slots - 1) * slot_seconds
    cadence = frame_epochs * epoch_seconds
    index = max(0, (timestamp - base) // cadence + 1)
    shift = (margin + epoch_seconds - 1) // epoch_seconds
    result = []
    for i in range(count):
        ref = base + (index + i) * cadence
        ref_slot = (ref - genesis) // slot_seconds
        safe = genesis + ((ref_slot // epoch_slots) - shift) * epoch_seconds
        result.append({'timestamp': ref, 'reference_time_utc': utc(ref),
                       'reference_slot': ref_slot, 'safe_timestamp': safe})
    return result


def build_scenarios(state, snapshot, references, flows):
    """Cash is assumed eligible; nominal rates intentionally impose no haircut.

    flows maps main/stress to cumulative arrivals in integer gwei at every
    reference. Each tier independently joins the same queue suffix.
    """
    ledger = [Request.from_mapping(row) for row in state['pending_requests']]
    front, last = int(state['last_finalized_id']), int(state['last_request_id'])
    Queue(ledger, front)
    if last != front + len(ledger):
        raise ValueError('non-contiguous queue frontier')
    joined = int(state['timestamp'])
    # Lido v4 getSharesByPooledEth uses the internal rate to avoid a second
    # external-ether flooring operation. Pooled/total is not wei-exact in general.
    shares, pooled = int(snapshot['internal_shares_wei']), int(snapshot['internal_ether_wei'])
    if shares <= 0 or pooled <= 0:
        raise ValueError('missing share conversion basis')
    initial = int(snapshot['scenario_available_cash_wei'])
    pending = sum(r.amount_steth_wei for r in ledger)
    if initial < 0 or not references or any(r['timestamp'] <= joined for r in references):
        raise ValueError('invalid scenario cash or reference times')
    outputs = {}
    for name in ('main', 'stress'):
        amounts = flows[name]
        if len(amounts) != len(references) or any(type(a) is not int or a < 0 for a in amounts):
            raise ValueError('incomplete or inexact consensus cashflow')
        if any(a > b for a, b in zip(amounts, amounts[1:])):
            raise ValueError('cumulative cashflow decreases')
        cutoffs = [max(0, initial + a * 10**9 - pending)
                   if ref['safe_timestamp'] >= joined else 0
                   for a, ref in zip(amounts, references)]
        tiers = {}
        for amount in TIERS:
            split = [amount] if amount <= 1000 else [1000, 500]
            hypothetical = [Request(last + i, a * WEI, a * WEI * shares // pooled,
                                    joined, int(state['last_report_timestamp']))
                            for i, a in enumerate(split, 1)]
            requests = ledger + hypothetical
            rate = max(r.rate for r in requests)
            budget, previous_arrival, current = initial, 0, front
            completion, outcomes = None, []
            for i, (arrival, ref) in enumerate(zip(amounts, references), 1):
                budget += (arrival - previous_arrival) * 10**9
                previous_arrival = arrival
                queue = Queue([r for r in requests if r.request_id > current], current)
                result = queue.calculate_batches(budget, ref['safe_timestamp'], rate)
                if not result['prefinalize_fits_budget']:
                    raise ValueError('nominal FIFO budget reconciliation failed')
                current = result['candidate_last_request_id']
                budget -= result['eth_to_lock_wei']
                outcomes.append({'referenceTime': ref['reference_time_utc'],
                                 'lastFinalizedId': current, 'budgetRemainderWei': str(budget)})
                if completion is None and current >= hypothetical[-1].request_id:
                    completion = {'eligible_report_index': i,
                                  'reference_time_utc': ref['reference_time_utc'],
                                  'elapsed_to_reference_seconds': ref['timestamp'] - joined}
            tiers[amount] = {'completion': completion, 'reports': outcomes}
        outputs[name] = {'tiers': tiers, 'cutoffs': cutoffs}
    return {
        'tiers': [{'steth': a, 'split': [a] if a <= 1000 else [1000, 500],
                   **{name: outputs[name]['tiers'][a]['completion'] for name in outputs}}
                  for a in TIERS],
        'dailyCutoffs': [{'referenceTime': ref['reference_time_utc'],
                          'mainSteth': eth(outputs['main']['cutoffs'][i]),
                          'stressSteth': eth(outputs['stress']['cutoffs'][i])}
                         for i, ref in enumerate(references)],
        'horizonEnd': references[-1]['reference_time_utc'],
        'scenarioAudit': outputs,
    }
