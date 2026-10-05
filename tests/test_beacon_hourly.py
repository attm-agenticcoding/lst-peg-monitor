"""Offline SSZ/authentication and withdrawal mechanics; no network in tests.

Full historical regression (optional fixture kept outside the repository):
LIDO_BEACON_REGRESSION_DIR=/path/to/lido_cutoff_completion_20261004 \
  python -m unittest discover -s tests -p test_beacon_hourly.py -v
"""
import csv
import datetime
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest import mock
import io

MODULE = Path(__file__).resolve().parents[1]/'scripts/lido/beacon_hourly.py'
spec = importlib.util.spec_from_file_location('beacon_hourly',MODULE)
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
G=10**9
FAR=2**64-1

def naive_merkle(nodes,limit):
    nodes=list(nodes)+[bytes(32)]*((1<<(limit-1).bit_length())-len(nodes))
    while len(nodes)>1:
        nodes=[b.sha(nodes[i]+nodes[i+1]) for i in range(0,len(nodes),2)]
    return nodes[0]

def make_validator(balance=32*G,effective=32*G,prefix=1,withdrawable=FAR,exit_epoch=FAR,
                   activation=0,vault=True,slashed=0,pubkey=None):
    return {'balance':balance,'effective':effective,'prefix':prefix,'withdrawable':withdrawable,
            'exit':exit_epoch,'activation':activation,'vault':vault,'slashed':slashed,'pubkey':pubkey}

def validator_bytes(v,index):
    cred=bytes([v['prefix']])+bytes(11)+(bytes.fromhex(b.VAULT[2:]) if v['vault'] else bytes(20))
    pubkey=v['pubkey'] or index.to_bytes(48,'little')
    return pubkey+cred+struct.pack('<QBQQQQ',v['effective'],v['slashed'],0,v['activation'],v['exit'],v['withdrawable'])

def synthetic_state(v=None,slot=320):
    v=v or make_validator()
    parts={name:(bytes(size) if size is not None else b'') for name,size in b.FIELDS}
    parts.update(genesis_time=struct.pack('<Q',b.GENESIS_TIME),
        genesis_validators_root=bytes.fromhex(b.GENESIS_VALIDATORS_ROOT),slot=struct.pack('<Q',slot),
        fork=bytes.fromhex('0500000006000000')+struct.pack('<Q',1),
        latest_block_header=struct.pack('<QQ',slot,0)+bytes(96),
        validators=validator_bytes(v,0),balances=struct.pack('<Q',v['balance']),
        previous_epoch_participation=bytes(1),current_epoch_participation=bytes(1),inactivity_scores=bytes(8),
        finalized_checkpoint=struct.pack('<Q',slot//32-2)+bytes(32))
    payload=bytearray(584)
    payload[404:412]=struct.pack('<Q',100)
    payload[428:436]=struct.pack('<Q',b.GENESIS_TIME+slot*12)
    payload[436:440]=struct.pack('<I',584)
    payload[472:504]=bytes.fromhex('aa'*32)
    parts['latest_execution_payload_header']=bytes(payload)
    fixed=bytearray();dynamic=bytearray()
    fixed_size=sum(size if size is not None else 4 for _,size in b.FIELDS)
    for name,size in b.FIELDS:
        if size is None:
            fixed.extend(struct.pack('<I',fixed_size+len(dynamic)));dynamic.extend(parts[name])
        else: fixed.extend(parts[name])
    return bytes(fixed+dynamic)

class SSZTests(unittest.TestCase):
    def test_streaming_merkle_matches_independent_batch(self):
        for n in range(66):
            nodes=[b.sha(str(i).encode()) for i in range(n)]
            for limit in [max(1,n),128]:
                self.assertEqual(b.merkle(iter(nodes),limit),naive_merkle(nodes,limit))
        with self.assertRaises(b.BeaconError): b.merkle([bytes(32)]*3,2)
    def test_ssz_boolean_and_limits(self):
        record=bytearray(validator_bytes(make_validator(),0));record[88]=2
        with self.assertRaises(b.BeaconError): b.validator_root(record)
        with self.assertRaises(b.BeaconError): b.bytes_root(bytes(33),32)
        with self.assertRaises(b.BeaconError): b.payload_root(bytes(584))
    def test_invalid_offsets_and_lengths(self):
        data=bytearray(synthetic_state());ranges=b.parse_field_ranges(data)
        self.assertEqual(ranges['validators'][1]-ranges['validators'][0],121)
        # First dynamic offset follows 7 fixed fields.
        offset=sum(size for _,size in b.FIELDS[:7])
        data[offset:offset+4]=struct.pack('<I',len(data)+1)
        with self.assertRaises(b.BeaconError): b.parse_field_ranges(data)
        with self.assertRaises(b.BeaconError): b.parse_field_ranges(bytes(100))
    def test_inspect_root_and_anchor(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'fixture.ssz';path.write_bytes(synthetic_state())
            summary=b.inspect_state(path)
            self.assertFalse(summary['chain_authenticated'])
            anchor={'state_block':{'number':'0x64','hash':summary['block_hash'],'timestamp':hex(summary['block_timestamp'])},
                    'child_block':{'number':'0x65','hash':'0x'+'bb'*32,'parentHash':summary['block_hash'],
                        'timestamp':hex(summary['block_timestamp']+12),'parentBeaconBlockRoot':summary['derived_beacon_block_header_root']},
                    'finalized_block':{'number':'0x66'}}
            self.assertTrue(b.authenticate_execution_anchor(summary,anchor)['all_checks_passed'])
            anchor['child_block']['parentBeaconBlockRoot']='0x'+'cc'*32
            with self.assertRaises(b.BeaconError): b.authenticate_execution_anchor(summary,anchor)
            with self.assertRaises(b.BeaconError): b.inspect_state(path,'0x'+'dd'*32)
    def test_unsupported_fork_and_skipped_slot_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'fixture.ssz';data=bytearray(synthetic_state());r=b.parse_field_ranges(data)
            data[r['fork'][0]+4]=7;path.write_bytes(data)
            with self.assertRaisesRegex(b.BeaconError,'Unsupported fork'): b.inspect_state(path)
            data=bytearray(synthetic_state());data[r['latest_block_header'][0]:r['latest_block_header'][0]+8]=struct.pack('<Q',319);path.write_bytes(data)
            with self.assertRaisesRegex(b.BeaconError,'same-slot'): b.inspect_state(path)
    def test_malformed_anchor_fails_closed(self):
        with self.assertRaises(b.BeaconError): b.authenticate_execution_anchor({}, {})
    def test_changed_cached_state_and_invalid_references_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            state=Path(temp)/'state.ssz';state.write_bytes(b'changed')
            with self.assertRaisesRegex(b.BeaconError,'changed after authentication'):
                b.simulate_reports(state,temp,[1],{},summary={'state_sha256':'not the file hash'})
            with self.assertRaisesRegex(b.BeaconError,'strictly increasing'):
                b.simulate_reports(state,temp,[2,1],{})


class DownloadTests(unittest.TestCase):
    class Response(io.BytesIO):
        status=200
        headers={'Eth-Consensus-Version':'fulu','Content-Type':'application/octet-stream'}
    def test_download_retains_http_provenance(self):
        data=synthetic_state()
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'state.ssz'
            with mock.patch.object(b.urllib.request,'urlopen',return_value=self.Response(data)):
                provenance=b.download_state(target)
            self.assertEqual(target.read_bytes(),data)
            self.assertEqual(provenance['bytes'],len(data))
            self.assertEqual(provenance['http_status'],200)
            self.assertNotIn('asOf',provenance)
    def test_bad_version_or_size_does_not_replace_previous_state(self):
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'state.ssz';target.write_bytes(b'previous')
            response=self.Response(b'x'*20)
            response.headers={'Eth-Consensus-Version':'gloas','Content-Type':'application/octet-stream'}
            with mock.patch.object(b.urllib.request,'urlopen',return_value=response):
                with self.assertRaises(b.BeaconError): b.download_state(target)
            self.assertEqual(target.read_bytes(),b'previous')
            with mock.patch.object(b.urllib.request,'urlopen',return_value=self.Response(b'x'*20)):
                with self.assertRaises(b.BeaconError): b.download_state(target,max_bytes=10)
            self.assertEqual(target.read_bytes(),b'previous')
            self.assertEqual(list(Path(temp).glob('*.part')),[])
    def test_deadline_failure_is_atomic(self):
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'state.ssz';target.write_bytes(b'previous')
            with mock.patch.object(b.urllib.request,'urlopen',return_value=self.Response(b'x')), \
                 mock.patch.object(b.time,'monotonic',side_effect=[0,2]):
                with self.assertRaisesRegex(b.BeaconError,'deadline exceeded'):
                    b.download_state(target,deadline_seconds=1)
            self.assertEqual(target.read_bytes(),b'previous')
            self.assertEqual(list(Path(temp).glob('*.part')),[])
    def test_http_denial_is_not_retried(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(b.urllib.request,'urlopen',side_effect=PermissionError('HTTP 403')) as request:
                with self.assertRaises(PermissionError): b.download_state(Path(temp)/'state.ssz')
            request.assert_called_once()

class MechanismTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.workdir=Path(cls.temp.name)
        cls.engine=b.build_engine(cls.workdir)
    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()
    def run_model(self,validators,*,pending=(),consolidations=(),deposits=(),slot=320,pointer=0,
                  mode='quiescent',blocks=1,fail=None):
        # Minimal binary fields for direct engine mechanisms, not an authenticated fixture.
        payload=bytearray();vs=len(payload)
        payload.extend(b''.join(validator_bytes(v,i) for i,v in enumerate(validators)))
        bs=len(payload);payload.extend(b''.join(struct.pack('<Q',v['balance']) for v in validators))
        ds=len(payload);payload.extend(b''.join(deposits))
        ps=len(payload);payload.extend(b''.join(struct.pack('<QQQ',*p) for p in pending))
        cs=len(payload);payload.extend(b''.join(struct.pack('<QQ',*c) for c in consolidations))
        state=self.workdir/'fixture.bin';state.write_bytes(payload)
        config=self.workdir/'fixture.txt'
        timestamp=b.GENESIS_TIME+(slot+blocks)*12
        vals=[slot,b.GENESIS_TIME,len(validators),pointer,0,0,vs,bs,ds,len(deposits),ps,len(pending),cs,len(consolidations)]
        config.write_text(' '.join(map(str,vals))+f'\n1\n{timestamp}\n')
        result=subprocess.run([str(self.engine),str(state),str(config),mode],capture_output=True,text=True)
        if fail:
            self.assertNotEqual(result.returncode,0);self.assertIn(fail,result.stderr);return None
        self.assertEqual(result.returncode,0,result.stderr)
        rows=list(csv.DictReader(result.stdout.splitlines()));self.assertEqual(len(rows),1)
        return {k:int(v) for k,v in rows[0].items()}
    def test_full_precedes_partial(self):
        row=self.run_model([make_validator(balance=33*G,withdrawable=10,exit_epoch=9)])
        self.assertEqual((row['full_cash_gwei'],row['partial_cash_gwei']),(33*G,0))
    def test_non_execution_credential_prefix_does_not_sweep(self):
        row=self.run_model([make_validator(balance=33*G,prefix=255)])
        self.assertEqual(row['cumulative_cash_gwei'],0)
    def test_compounding_threshold(self):
        row=self.run_model([make_validator(balance=33*G,effective=33*G,prefix=2)])
        self.assertEqual(row['cumulative_cash_gwei'],0)
    def test_pending_reduces_same_block_sweep(self):
        row=self.run_model([make_validator(balance=2100*G,effective=2048*G,prefix=2)],pending=[(0,60*G,10)])
        self.assertEqual((row['pending_cash_gwei'],row['partial_cash_gwei']),(60*G,0))
    def test_invalid_pending_does_not_consume_capacity(self):
        row=self.run_model([make_validator(balance=33*G,exit_epoch=11)],pending=[(0,G,10)])
        self.assertEqual((row['pending_cash_gwei'],row['partial_cash_gwei']),(0,G))
    def test_immature_fifo_partial_blocks_followers(self):
        row=self.run_model([make_validator(balance=33*G,effective=33*G,prefix=2)],pending=[(0,G,11),(0,G,10)])
        self.assertEqual(row['cumulative_cash_gwei'],0)
    def test_eight_pending_leaves_eight_sweep_positions(self):
        validators=[make_validator(balance=100*G,effective=100*G,prefix=2)]+[make_validator(balance=33*G) for _ in range(20)]
        row=self.run_model(validators,pending=[(0,G,10)]*20)
        self.assertEqual((row['pending_cash_gwei'],row['partial_cash_gwei'],row['pointer']),(8*G,8*G,9))
    def test_underfilled_pointer_uses_max_sweep_not_count(self):
        row=self.run_model([make_validator() for _ in range(20)],pointer=19)
        self.assertEqual(row['pointer'],(19+16384)%20)
    def test_workload_not_added_to_cash(self):
        vals=[make_validator() for _ in range(100)]
        row=self.run_model(vals,mode='legacy_reserved8')
        self.assertEqual((row['cumulative_cash_gwei'],row['synthetic_workload'],row['pointer']),(0,16,8))
    def test_consolidation_leaves_excess_before_new_epoch_sweep(self):
        vals=[make_validator(balance=33*G,effective=32*G,prefix=2,withdrawable=10,exit_epoch=9),
              make_validator(prefix=2)]+[make_validator(balance=2048*G,effective=2048*G,prefix=2,vault=False) for _ in range(8192)]
        row=self.run_model(vals,slot=319,consolidations=[(0,1)])
        self.assertEqual((row['full_cash_gwei'],row['consolidated_gwei']),(G,32*G))
    def test_known_topup_at_epoch_boundary(self):
        vals=[make_validator()]+[make_validator(balance=2048*G,effective=2048*G,prefix=2,vault=False) for _ in range(8192)]
        deposit=validator_bytes(vals[0],0)[:80]+struct.pack('<Q',G)+bytes(96)+struct.pack('<Q',0)
        row=self.run_model(vals,slot=319,deposits=[deposit])
        self.assertEqual((row['partial_cash_gwei'],row['applied_deposits_gwei']),(G,G))
        # Existing-validator top-ups ignore deposit credentials.
        unusual=deposit[:48]+bytes([255])+deposit[49:]
        row=self.run_model(vals,slot=319,deposits=[unusual])
        self.assertEqual((row['partial_cash_gwei'],row['applied_deposits_gwei']),(G,G))
    def test_duplicate_new_key_deposits_create_one_validator(self):
        vals=[make_validator()]+[make_validator(balance=2048*G,effective=2048*G,prefix=2,vault=False) for _ in range(8192)]
        new=make_validator(pubkey=bytes.fromhex('ab'*48))
        deposit=validator_bytes(new,99999)[:80]+struct.pack('<Q',32*G)+bytes(96)+struct.pack('<Q',0)
        row=self.run_model(vals,slot=319,deposits=[deposit,deposit])
        self.assertEqual((row['registry_count'],row['applied_deposits_gwei'],row['partial_cash_gwei']),
                         (len(vals)+1,64*G,32*G))
    def test_exiting_deposit_postpones_until_after_withdrawable_epoch(self):
        vals=[make_validator(exit_epoch=11,withdrawable=12)]+[make_validator(balance=2048*G,effective=2048*G,prefix=2,vault=False) for _ in range(8192)]
        deposit=validator_bytes(vals[0],0)[:80]+struct.pack('<Q',G)+bytes(96)+struct.pack('<Q',0)
        first=self.run_model(vals,slot=319,deposits=[deposit])
        self.assertEqual((first['pending_deposits'],first['applied_deposits_gwei']),(1,0))
        later=self.run_model(vals,slot=319,deposits=[deposit],blocks=97)
        self.assertEqual((later['pending_deposits'],later['applied_deposits_gwei'],later['full_cash_gwei']),
                         (0,G,33*G))
    def test_deposit_churn_balance_carries_across_epochs(self):
        vals=[make_validator()]+[make_validator(balance=2048*G,effective=2048*G,prefix=2,vault=False) for _ in range(8192)]
        new=make_validator(prefix=2,pubkey=bytes.fromhex('ab'*48))
        deposit=validator_bytes(new,99999)[:80]+struct.pack('<Q',512*G)+bytes(96)+struct.pack('<Q',0)
        first=self.run_model(vals,slot=319,deposits=[deposit])
        self.assertEqual((first['pending_deposits'],first['applied_deposits_gwei']),(1,0))
        later=self.run_model(vals,slot=319,deposits=[deposit],blocks=33)
        self.assertEqual((later['pending_deposits'],later['applied_deposits_gwei']),(0,512*G))
    def test_ejection_and_future_slashed_exposure_fail_closed(self):
        self.run_model([make_validator(effective=16*G,balance=16*G)],fail='ejection')
        self.run_model([make_validator(slashed=1,exit_epoch=11,withdrawable=100)],fail='slashed')
        self.run_model([make_validator(balance=0,slashed=1,exit_epoch=11,withdrawable=100)],fail='slashed')
    def test_churn_assumption_fails_closed(self):
        self.run_model([make_validator()],slot=319,fail='churn cap')
    def test_invalid_pending_index_fails_closed(self):
        self.run_model([make_validator()],pending=[(2,G,10)],fail='out of range')

REGRESSION=os.environ.get('LIDO_BEACON_REGRESSION_DIR')
@unittest.skipUnless(REGRESSION,'Set LIDO_BEACON_REGRESSION_DIR for authenticated large-state regression')
class HistoricalRegression(unittest.TestCase):
    def test_all_original_main_and_stress_report_amounts_exact(self):
        root=Path(REGRESSION);source=root/'inflow_source';old=root/'beacon_scenario'
        expected=json.loads((old/'report_scenarios.json').read_text())
        state=source/'ethstaker_finalized_state.ssz'
        oldsummary=json.loads((source/'state_summary.json').read_text())
        rootcheck=json.loads((source/'ssz_root_verification.json').read_text())
        summary=b.inspect_state(state,rootcheck['ssz_hash_tree_root'])
        self.assertEqual(summary['state_sha256'],oldsummary['state_sha256'])
        def header(name): return json.loads((root/'raw/rpc'/name).read_text())['response']['result']
        execution=summary['latest_execution_payload_header']
        anchor={'state_block':{'number':hex(execution['block_number']),'hash':execution['block_hash'],
                              'timestamp':hex(execution['timestamp'])},
                'child_block':header('state_anchor_child_26120129.json'),
                'finalized_block':header('state_anchor_finalized_head.json')}
        # Union of the original 8-day main and 12-day stress daily reference grid.
        refs=sorted({int(datetime.datetime.fromisoformat(row['report_reference_utc'].replace('Z','+00:00')).timestamp())
                     for mode in ['legacy_valid','legacy_reserved8_valid'] for row in expected[mode]['reports']})
        with tempfile.TemporaryDirectory() as workdir:
            actual=b.simulate_reports(state,workdir,refs,anchor,summary=summary)
        self.assertTrue(actual['validation']['all_checks_passed'])
        from decimal import Decimal
        checked=0
        for label,old_mode in [('main','legacy_valid'),('stress','legacy_reserved8_valid')]:
            rows={row['reference_timestamp']:row for row in actual['scenarios'][label]['reports']}
            for oldrow in expected[old_mode]['reports']:
                ts=int(datetime.datetime.fromisoformat(oldrow['report_reference_utc'].replace('Z','+00:00')).timestamp())
                row=rows[ts]
                for key,oldkey in [('cumulative_cash_gwei','known_state_arrivals_eth'),('full_cash_gwei','full_arrivals_eth'),
                                   ('partial_cash_gwei','partial_arrivals_eth'),('pending_cash_gwei','pending_partial_arrivals_eth')]:
                    self.assertEqual(int(row[key]),int(Decimal(oldrow[oldkey])*G),(label,ts,key))
                self.assertEqual(row['reference_slot'],int(oldrow['reference_slot']))
                checked+=1
        self.assertGreaterEqual(checked,20)

if __name__=='__main__': unittest.main()
