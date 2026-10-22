"""Read the call cap from one durable lease, never a process-wide override."""
import re


DEFAULT_RPC_CALLS = 256
MANUAL_CATCHUP_RPC_CALLS = 320
OUTAGE_CATCHUP_RPC_CALLS = 1024

def run_seconds(budget):
    return 2700 if budget == OUTAGE_CATCHUP_RPC_CALLS else 1500

def lease_seconds(budget):
    return run_seconds(budget) + 600



def validate_budget(value, trigger):
    if type(value) is not int or value not in (DEFAULT_RPC_CALLS, MANUAL_CATCHUP_RPC_CALLS, OUTAGE_CATCHUP_RPC_CALLS):
        raise ValueError('RPC call budget must be exactly 256, 320 or 1024')
    if value != DEFAULT_RPC_CALLS and trigger != 'manual':
        raise ValueError('expanded calls require an explicitly chosen manual catch-up lease')
    return value


def lease_rpc_budget(snapshot):
    """Absent legacy fields mean 256; 320 needs the original manual identity.

    The controller must obtain approval before publishing this lease. Its CAS
    readback and the journal pin the entire snapshot bytes, including this cap
    and request identity. This field is not a permission or fresh-run grant.
    """
    refresh = snapshot.get('refresh', {})
    lease = refresh.get('lease') or {}
    budget = validate_budget(lease.get('rpcCallBudget', DEFAULT_RPC_CALLS), lease.get('trigger'))
    if budget != DEFAULT_RPC_CALLS:
        key = lease.get('requestKey')
        attempts = refresh.get('manualAttemptIds')
        if (refresh.get('trigger') != 'manual'
                or not isinstance(lease.get('runId'), str) or not lease['runId']
                or not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,159}', key)
                or key != refresh.get('lastManualRequestId')
                or not isinstance(attempts, list) or key not in attempts):
            raise ValueError('expanded lease is not bound to its original manual request and run')
    return budget
