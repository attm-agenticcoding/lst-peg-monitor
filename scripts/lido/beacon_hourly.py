"""Authenticated mainnet Fulu known-state vault-cash scenarios (stdlib + C++17).

Public workflow: download_finalized_state -> inspect_state -> RPC anchor supplied
by the collector -> simulate_reports. No future balance/withdrawal observations
enter the model; the immediate child header is used only as an integrity anchor.
Source rules: ethereum/consensus-specs v1.6.0, Fulu/Electra/Capella/phase0.
This is a cash-arrival sensitivity, not a full consensus client or an ETA bound.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import mmap
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import time
import urllib.request

SOURCE_URL = 'https://beaconstate.ethstaker.cc/eth/v2/debug/beacon/states/finalized'
VAULT = '0xb9d7934878b5fb9610b3fe8a5e441e8fad7e293f'
GENESIS_TIME = 1606824023
GENESIS_VALIDATORS_ROOT = '4b363db94e286120d76eb905340fdd4e54bfe9f06bf33ff6cf5ad27f511bfe95'
FAR = 2**64 - 1
FIELDS = [
 ('genesis_time',8),('genesis_validators_root',32),('slot',8),('fork',16),
 ('latest_block_header',112),('block_roots',8192*32),('state_roots',8192*32),
 ('historical_roots',None),('eth1_data',72),('eth1_data_votes',None),
 ('eth1_deposit_index',8),('validators',None),('balances',None),
 ('randao_mixes',65536*32),('slashings',8192*8),
 ('previous_epoch_participation',None),('current_epoch_participation',None),
 ('justification_bits',1),('previous_justified_checkpoint',40),
 ('current_justified_checkpoint',40),('finalized_checkpoint',40),
 ('inactivity_scores',None),('current_sync_committee',512*48+48),
 ('next_sync_committee',512*48+48),('latest_execution_payload_header',None),
 ('next_withdrawal_index',8),('next_withdrawal_validator_index',8),
 ('historical_summaries',None),('deposit_requests_start_index',8),
 ('deposit_balance_to_consume',8),('exit_balance_to_consume',8),
 ('earliest_exit_epoch',8),('consolidation_balance_to_consume',8),
 ('earliest_consolidation_epoch',8),('pending_deposits',None),
 ('pending_partial_withdrawals',None),('pending_consolidations',None),
 ('proposer_lookahead',64*8),
]
LISTS = {'historical_roots':(32,2**24),'eth1_data_votes':(72,2048),
 'validators':(121,2**40),'balances':(8,2**40),
 'previous_epoch_participation':(1,2**40),'current_epoch_participation':(1,2**40),
 'inactivity_scores':(8,2**40),'historical_summaries':(64,2**24),
 'pending_deposits':(192,2**27),'pending_partial_withdrawals':(24,2**27),
 'pending_consolidations':(16,2**18)}
ASSUMPTIONS = [
 'One execution-bearing block every 12-second slot, healthy two-epoch-lag finality, unchanged Fulu rules.',
 'Only snapshot balances, existing pending deposits, scheduled exits and pending consolidations are modeled; future rewards, penalties, exits, requests and credential changes are not observed or forecast.',
 'New public keys in the snapshot pending-deposit queue are assumed to have valid deposit signatures; BLS signatures and finality are not locally verified.',
 'Future activations of newly deposited validators are not modeled; the 256-ETH activation/exit churn cap is checked every epoch from the remaining known active registry.',
 'Main scenario adds recurring active legacy-validator partial-withdrawal competition; stress additionally reserves eight competing pending-partial positions per block. Synthetic workload adds no target cash.',
 'These capacity-demand sensitivities are neither probabilities nor guaranteed earliest/latest payment bounds.',
 'Recipient selection uses withdrawal credentials, not an independent attestation of Lido validator membership.',
]

class BeaconError(ValueError):
    """Unsupported, invalid, unauthenticated or unsafe model input."""

def require(condition, message):
    if not condition:
        raise BeaconError(message)

def sha(data):
    return hashlib.sha256(data).digest()

ZERO = [bytes(32)]
for _ in range(64):
    ZERO.append(sha(ZERO[-1] + ZERO[-1]))

def pad(data):
    require(len(data) <= 32, 'SSZ scalar exceeds 32 bytes')
    return bytes(data) + bytes(32-len(data))

def merkle(nodes, limit=None):
    """Streaming SSZ merkleization, O(log N) auxiliary memory."""
    stack = []
    count = 0
    for node in nodes:
        require(len(node) == 32, 'SSZ node must be 32 bytes')
        node = bytes(node)
        level = 0
        count += 1
        n = count - 1
        while n & 1:
            node = sha(stack[level] + node)
            stack[level] = None
            n >>= 1
            level += 1
        if level == len(stack):
            stack.append(node)
        else:
            stack[level] = node
    limit = max(1, count) if limit is None else limit
    require(limit >= count and limit >= 1, 'SSZ list exceeds limit')
    depth = (limit-1).bit_length()
    if count == 0:
        return ZERO[depth]
    if count == 1 << depth:
        return stack[depth]
    root = ZERO[0]
    for level in range(depth):
        root = sha(stack[level] + root) if (count >> level) & 1 else sha(root + ZERO[level])
    return root

def bytes_root(data, limit=None, mix=False):
    n = len(data)
    require(limit is None or n <= limit, 'SSZ byte sequence exceeds limit')
    root = merkle((pad(data[i:i+32]) for i in range(0,n,32)),
                  (limit+31)//32 if limit is not None else None)
    return sha(root+n.to_bytes(32,'little')) if mix else root

def vector(data, size, rootfunc, limit=None, mix=False):
    require(len(data)%size == 0, 'SSZ list has truncated element')
    root = merkle((rootfunc(data[i:i+size]) for i in range(0,len(data),size)), limit)
    return sha(root+(len(data)//size).to_bytes(32,'little')) if mix else root

def basic(data, limit, size, mix):
    require(len(data)%size == 0, 'SSZ basic list has truncated element')
    root = bytes_root(data, limit*size)
    return sha(root+(len(data)//size).to_bytes(32,'little')) if mix else root

def header_root(v):
    require(len(v) == 112, 'Invalid BeaconBlockHeader size')
    return merkle([pad(v[:8]),pad(v[8:16]),v[16:48],v[48:80],v[80:112]])

def validator_root(v):
    require(v[88] in (0,1), 'Invalid SSZ boolean in validator')
    return merkle([bytes_root(v[:48]),v[48:80],pad(v[80:88]),pad(v[88:89]),
                   pad(v[89:97]),pad(v[97:105]),pad(v[105:113]),pad(v[113:121])])

def payload_root(v):
    require(584 <= len(v) <= 616, 'Invalid Fulu execution payload header length')
    offset = int.from_bytes(v[436:440], 'little')
    require(offset == 584, 'Invalid payload extra_data offset')
    return merkle([v[:32],pad(v[32:52]),v[52:84],v[84:116],bytes_root(v[116:372]),
     v[372:404],pad(v[404:412]),pad(v[412:420]),pad(v[420:428]),pad(v[428:436]),
     bytes_root(v[offset:],32,True),v[440:472],v[472:504],v[504:536],v[536:568],
     pad(v[568:576]),pad(v[576:584])])

def field_root(name, v):
    eth1 = lambda x: merkle([x[:32],pad(x[32:40]),x[40:72]])
    if name == 'fork':
        return merkle([pad(v[:4]),pad(v[4:8]),pad(v[8:16])])
    if name == 'latest_block_header': return header_root(v)
    if name in ('block_roots','state_roots'): return bytes_root(v,8192*32)
    if name == 'historical_roots': return basic(v,2**24,32,True)
    if name == 'eth1_data': return eth1(v)
    if name == 'eth1_data_votes': return vector(v,72,eth1,2048,True)
    if name == 'validators': return vector(v,121,validator_root,2**40,True)
    if name in ('balances','inactivity_scores'): return basic(v,2**40,8,True)
    if name == 'randao_mixes': return bytes_root(v,65536*32)
    if name == 'slashings': return basic(v,8192,8,False)
    if name in ('previous_epoch_participation','current_epoch_participation'):
        return basic(v,2**40,1,True)
    if name.endswith('_checkpoint'): return sha(pad(v[:8])+bytes(v[8:40]))
    if name in ('current_sync_committee','next_sync_committee'):
        return sha(vector(v[:512*48],48,bytes_root)+bytes_root(v[512*48:]))
    if name == 'latest_execution_payload_header': return payload_root(v)
    if name == 'historical_summaries': return vector(v,64,bytes_root,2**24,True)
    if name == 'pending_deposits':
        return vector(v,192,lambda x:merkle([bytes_root(x[:48]),x[48:80],pad(x[80:88]),
                                            bytes_root(x[88:184]),pad(x[184:192])]),2**27,True)
    if name in ('pending_partial_withdrawals','pending_consolidations'):
        size, limit = LISTS[name]
        return vector(v,size,lambda x:merkle(pad(x[i:i+8]) for i in range(0,len(x),8)),limit,True)
    if name == 'proposer_lookahead': return basic(v,64,8,False)
    if name == 'justification_bits': require(v[0] < 16, 'Invalid SSZ justification bitvector')
    return pad(v)

def parse_field_ranges(data):
    fixed_size = sum(size if size is not None else 4 for _,size in FIELDS)
    require(len(data) >= fixed_size, 'Truncated Fulu BeaconState fixed section')
    ranges, dynamic, offset = {}, [], 0
    for name,size in FIELDS:
        if size is None:
            dynamic.append((name,struct.unpack_from('<I',data,offset)[0]))
            offset += 4
        else:
            ranges[name] = (offset,offset+size)
            offset += size
    require(dynamic[0][1] == fixed_size, 'Noncanonical first SSZ offset')
    offsets = [x[1] for x in dynamic]
    require(offsets == sorted(offsets) and offsets[-1] <= len(data), 'Invalid SSZ offsets')
    for (name,start),(_,end) in zip(dynamic,dynamic[1:]+[('',len(data))]):
        ranges[name] = (start,end)
        if name in LISTS:
            size,limit = LISTS[name]
            require((end-start)%size == 0 and (end-start)//size <= limit,
                    'Invalid SSZ list size: '+name)
    n = (ranges['validators'][1]-ranges['validators'][0])//121
    require(n > 0, 'Empty validator registry')
    for name,size in [('balances',8),('inactivity_scores',8),
                      ('previous_epoch_participation',1),('current_epoch_participation',1)]:
        require(ranges[name][1]-ranges[name][0] == n*size, 'Validator/list length mismatch: '+name)
    return ranges

def inspect_state(path, expected_state_root=None):
    """Fully parse/root-check local SSZ. External chain authentication is separate.

    Returned summary is small; no validator JSON or per-block/event CSV is emitted.
    This supported anchoring method requires a same-slot post-block state. Empty-slot
    states fail closed because the latest header would not commit to this state root.
    """
    path = Path(path)
    require(path.is_file() and path.stat().st_size > 0, 'Missing or empty BeaconState')
    with path.open('rb') as f, mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ) as data:
        r = parse_field_ranges(data)
        get = lambda name: data[slice(*r[name])]
        u = lambda name: int.from_bytes(get(name),'little')
        require(u('genesis_time') == GENESIS_TIME and get('genesis_validators_root').hex() == GENESIS_VALIDATORS_ROOT,
                'Unsupported network: expected Ethereum mainnet')
        fork = get('fork')
        require(fork[4:8] == bytes.fromhex('06000000'), 'Unsupported fork: only mainnet Fulu is modeled')
        slot = u('slot')
        require(slot//32 >= int.from_bytes(fork[8:16],'little'), 'Fork epoch after state epoch')
        header = bytearray(get('latest_block_header'))
        require(int.from_bytes(header[:8],'little') == slot and header[48:80] == bytes(32),
                'Unsupported state: requires same-slot post-block header with zero state_root')
        payload = get('latest_execution_payload_header')
        payload_root(payload)
        timestamp = int.from_bytes(payload[428:436],'little')
        require(timestamp == GENESIS_TIME+slot*12, 'State slot/execution timestamp mismatch')
        n = (r['validators'][1]-r['validators'][0])//121
        require(u('next_withdrawal_validator_index') < n, 'Invalid withdrawal sweep pointer')
        require(u('eth1_deposit_index') == u('deposit_requests_start_index'),
                'Unsupported pending old-deposit bridge gate')
        finalized_epoch = int.from_bytes(get('finalized_checkpoint')[:8],'little')
        require(finalized_epoch <= slot//32 and slot//32-finalized_epoch <= 3,
                'Finality outside supported healthy-finality assumption')
        roots = {}
        for name,_ in FIELDS:
            # memoryview avoids duplicating the 288MB serialized validator list.
            view = memoryview(data)[slice(*r[name])]
            try: roots[name] = field_root(name,view).hex()
            finally: view.release()
        state_root = '0x'+merkle(bytes.fromhex(roots[name]) for name,_ in FIELDS).hex()
        if expected_state_root is not None:
            require(state_root == str(expected_state_root).lower(), 'SSZ state root mismatch')
        header[48:80] = bytes.fromhex(state_root[2:])
        execution = {'block_number':int.from_bytes(payload[404:412],'little'),
                     'block_hash':'0x'+payload[472:504].hex(),
                     'parent_hash':'0x'+payload[:32].hex(),'timestamp':timestamp}
        summary = {'schema':1,'network':'mainnet','fork':'fulu','fork_version':'0x'+fork[4:8].hex(),
            'state_slot':slot,'epoch':slot//32,'genesis_time':GENESIS_TIME,
            'state_timestamp':GENESIS_TIME+slot*12,'state_root':state_root,
            'derived_beacon_block_header_root':'0x'+header_root(header).hex(),
            'state_sha256':hashlib.sha256(data).hexdigest(),'state_bytes':len(data),
            'validator_count':n,'latest_execution_payload_header':execution,
            'block_number':execution['block_number'],'block_hash':execution['block_hash'],
            'block_timestamp':timestamp,'timestamp':timestamp,'finalized_checkpoint_epoch':finalized_epoch,
            'ssz_field_ranges':r,'field_roots':roots,
            'pending_deposit_count':(r['pending_deposits'][1]-r['pending_deposits'][0])//192,
            'pending_partial_withdrawal_count':(r['pending_partial_withdrawals'][1]-r['pending_partial_withdrawals'][0])//24,
            'pending_consolidation_count':(r['pending_consolidations'][1]-r['pending_consolidations'][0])//16,
            'next_withdrawal_validator_index':u('next_withdrawal_validator_index'),
            'next_withdrawal_index':u('next_withdrawal_index'),
            'deposit_balance_to_consume':u('deposit_balance_to_consume'),
            'chain_authenticated':False}
    return summary

def _rpc_int(value):
    return int(value,16) if isinstance(value,str) and value.startswith('0x') else int(value)

def authenticate_execution_anchor(summary, execution_anchor):
    """Check parent-collected canonical EL headers; no predictor data is consumed."""
    require(isinstance(execution_anchor,dict), 'Execution anchor required')
    try:
        block = execution_anchor['state_block']
        child = execution_anchor['child_block']
        finalized = execution_anchor['finalized_block']
        checks = {
          'state_execution_number':_rpc_int(block['number']) == summary['block_number'],
          'state_execution_hash':block['hash'].lower() == summary['block_hash'],
          'state_execution_timestamp':_rpc_int(block['timestamp']) == summary['block_timestamp'],
          'child_immediate_successor':_rpc_int(child['number']) == summary['block_number']+1,
          'child_parent_execution_hash':child['parentHash'].lower() == summary['block_hash'],
          'child_parent_beacon_root':child['parentBeaconBlockRoot'].lower() == summary['derived_beacon_block_header_root'],
          'child_timestamp_after_state':_rpc_int(child['timestamp']) > summary['block_timestamp'],
          'finalized_head_covers_child':_rpc_int(finalized['number']) >= _rpc_int(child['number']),
        }
        if _rpc_int(finalized['number']) == _rpc_int(child['number']):
            checks['same_height_finalized_hash'] = finalized['hash'].lower() == child['hash'].lower()
        if execution_anchor.get('expected_state_root'):
            checks['expected_state_root'] = execution_anchor['expected_state_root'].lower() == summary['state_root']
    except (KeyError,TypeError,ValueError,AttributeError) as error:
        raise BeaconError('Malformed execution anchor') from error
    require(all(checks.values()), 'Execution/SSZ anchor mismatch: '+','.join(k for k,v in checks.items() if not v))
    return {'all_checks_passed':True,'checks':checks,
            'child_execution_block_number':_rpc_int(child['number']),
            'child_execution_block_hash':child['hash'],
            'finalized_execution_block_number':_rpc_int(finalized['number']),
            'method':'Recomputed SSZ state and same-slot BeaconBlockHeader anchored in immediate EL child EIP-4788 parentBeaconBlockRoot.',
            'limitations':'Provider-attested canonical/finalized EL headers; no local BLS finality or full chain-ancestry verification. Child header is integrity-only, never a future cash predictor.'}

def download_state(path, max_bytes=450*1024**2, timeout=60, deadline_seconds=600):
    """One bounded public read; no stale-state fallback or route switching."""
    require(timeout > 0 and deadline_seconds > 0 and max_bytes > 0, 'Invalid download limits')
    path = Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    started = int(time.time())
    deadline = time.monotonic()+deadline_seconds
    fd,tmp = tempfile.mkstemp(prefix='beacon-download-',suffix='.ssz.part',dir=path.parent)
    try:
        req = urllib.request.Request(SOURCE_URL,headers={'Accept':'application/octet-stream','User-Agent':'lst-peg-monitor/1'})
        with os.fdopen(fd,'wb') as out, urllib.request.urlopen(req,timeout=min(timeout,deadline_seconds)) as response:
            require(response.status == 200, 'BeaconState download did not return HTTP 200')
            version = response.headers.get('Eth-Consensus-Version','').lower()
            require(version == 'fulu', 'Missing or unsupported download consensus version')
            content_type = response.headers.get('Content-Type','').split(';')[0].strip().lower()
            require(content_type == 'application/octet-stream', 'Unexpected BeaconState content type')
            size = 0
            reader = getattr(response,'read1',response.read)
            while True:
                require(time.monotonic() <= deadline, 'BeaconState download deadline exceeded')
                chunk = reader(1024*1024)
                require(time.monotonic() <= deadline, 'BeaconState download deadline exceeded')
                if not chunk: break
                size += len(chunk)
                require(size <= max_bytes, 'BeaconState download exceeds size limit')
                out.write(chunk)
            require(size >= sum(s if s is not None else 4 for _,s in FIELDS), 'Truncated BeaconState download')
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp,path)
        return {'source_url':SOURCE_URL,'retrieval_started_timestamp':started,
                'retrieved_timestamp':int(time.time()),'consensus_version':version,'bytes':size,
                'http_status':200,'http_content_type':content_type}

    except BaseException:
        if os.path.exists(tmp): os.unlink(tmp)
        raise

# Backward-compatible descriptive alias; only one public source is used.
download_finalized_state = download_state

def build_engine(workdir):
    workdir = Path(workdir)
    workdir.mkdir(parents=True,exist_ok=True)
    source = Path(__file__).with_name('beacon_simulate.cpp')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    binary = workdir/('beacon-simulate-'+digest)
    if binary.is_file(): return binary
    compiler = shutil.which('g++') or shutil.which('clang++')
    require(compiler is not None, 'C++17 compiler unavailable')
    temporary = binary.with_suffix('.tmp-'+str(os.getpid()))
    try:
        subprocess.run([compiler,'-O3','-std=c++17','-Wall','-Wextra','-Werror',str(source),'-o',str(temporary)],
                       check=True,capture_output=True,text=True,timeout=120)
        os.replace(temporary,binary)
    except subprocess.CalledProcessError as error:
        raise BeaconError('Beacon engine build failed: '+error.stderr) from error
    finally:
        temporary.unlink(missing_ok=True)
    return binary

def simulate_reports(path, workdir, reference_timestamps, execution_anchor, *, summary=None):
    """Return main/stress cumulative incoming cash in integer gwei at report times.

    summary, if supplied, must be the inspect_state result for the identical bytes;
    the file SHA256 is rechecked before model execution. No physical cash or queue
    quantity belongs here: the collector aligns those independently to this block.
    """
    path,workdir = Path(path).resolve(),Path(workdir).resolve()
    refs = list(reference_timestamps)
    require(refs and all(isinstance(t,int) and not isinstance(t,bool) for t in refs),
            'Report timestamps must be nonempty integer Unix seconds')
    require(refs == sorted(set(refs)), 'Report timestamps must be strictly increasing')
    require(isinstance(execution_anchor,dict), 'Execution anchor required')
    if summary is None:
        summary = inspect_state(path,execution_anchor.get('expected_state_root'))
    else:
        with path.open('rb') as f:
            actual_hash = hashlib.file_digest(f,'sha256').hexdigest()
        require(actual_hash == summary['state_sha256'], 'BeaconState changed after authentication')
    require(refs[0] >= summary['state_timestamp'], 'Report reference precedes model snapshot')
    require(refs[-1] <= summary['state_timestamp']+31*86400, 'Model horizon exceeds 31-day safety limit')
    anchor = authenticate_execution_anchor(summary,execution_anchor)
    binary = build_engine(workdir)
    workdir.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='beacon-run-',dir=workdir) as temp:
        config = Path(temp)/'config.txt'
        r = summary['ssz_field_ranges']
        values = [summary[k] for k in ['state_slot','genesis_time','validator_count',
                  'next_withdrawal_validator_index','next_withdrawal_index','deposit_balance_to_consume']]
        values += [r['validators'][0],r['balances'][0],r['pending_deposits'][0],
                   summary['pending_deposit_count'],r['pending_partial_withdrawals'][0],
                   summary['pending_partial_withdrawal_count'],r['pending_consolidations'][0],
                   summary['pending_consolidation_count']]
        config.write_text(' '.join(map(str,values))+'\n'+str(len(refs))+'\n'+'\n'.join(map(str,refs))+'\n')
        scenarios = {}
        for label,mode in [('main','legacy'),('stress','legacy_reserved8')]:
            try:
                result = subprocess.run([str(binary),str(path),str(config),mode],capture_output=True,
                                        text=True,check=True,timeout=600)
            except subprocess.CalledProcessError as error:
                raise BeaconError('Beacon simulation rejected state: '+error.stderr.strip()) from error
            reports = []
            for row in csv.DictReader(result.stdout.splitlines()):
                record = {k:(v if k.endswith('_gwei') else int(v)) for k,v in row.items()}
                require(int(record['cumulative_cash_gwei']) == sum(int(record[k]) for k in
                        ['full_cash_gwei','partial_cash_gwei','pending_cash_gwei']), 'Cash categories do not reconcile')
                require(0 <= record['pointer'] < record['registry_count'], 'Invalid model sweep pointer')
                reports.append(record)
            require([x['reference_timestamp'] for x in reports] == refs, 'Missing or misaligned simulation reports')
            require(all(int(b['cumulative_cash_gwei']) >= int(a['cumulative_cash_gwei'])
                        for a,b in zip(reports,reports[1:])), 'Nonmonotonic cumulative cash')
            scenarios[label] = {'mode':mode,'reports':reports}
    clean_summary = {k:v for k,v in summary.items() if k not in ('ssz_field_ranges','field_roots')}
    clean_summary['chain_authenticated'] = True
    anchor['model_checks'] = {
        'cash_categories_reconciled':True,'monotonic_cumulative_cash':True,
        'withdrawal_capacity_and_pointer_bounds':True,'churn_cap_checked_every_epoch':True,
        'no_unmodeled_ejection_or_future_slashed_exposure':True,
        'report_references_complete':True,
    }
    return {'schema':1,'summary':clean_summary,'validation':anchor,'vault_recipient':VAULT,
            'main':[int(row['cumulative_cash_gwei']) for row in scenarios['main']['reports']],
            'stress':[int(row['cumulative_cash_gwei']) for row in scenarios['stress']['reports']],
            'reference_timestamps':refs,'scenarios':scenarios,'assumptions':ASSUMPTIONS,'cash_unit':'gwei',
            'prediction_inputs':'Only authenticated snapshot state; future EL child header used solely for integrity.'}

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('state',type=Path)
    parser.add_argument('--expected-state-root')
    parser.add_argument('--anchor',type=Path)
    parser.add_argument('--reference',type=int,action='append')
    parser.add_argument('--workdir',type=Path,default=Path('.beacon-work'))
    args = parser.parse_args()
    summary = inspect_state(args.state,args.expected_state_root)
    if args.anchor:
        output = simulate_reports(args.state,args.workdir,args.reference,json.loads(args.anchor.read_text()),summary=summary)
    else: output = summary
    print(json.dumps(output,indent=2))
