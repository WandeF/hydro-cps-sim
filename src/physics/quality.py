"""Control-boundary quality decisions, separate from solver validation.

Numerical validity, source physical state and transport freshness are distinct.
Only successfully delivered values enter the per-destination hold cache.
"""
from __future__ import annotations
import copy
import math
from pathlib import Path
import threading
from src.io.csv import append_jsonl

ACTIONS = {'pass_through','hold_last','fail_fast','skip_write'}
QUALITIES = {'good','initial','unsupplied','invalid','missing','stale'}


def _audit_value(value):
    if type(value) is float and not math.isfinite(value): return repr(value)
    if isinstance(value,dict): return {str(k):_audit_value(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)): return [_audit_value(v) for v in value]
    if value is None or type(value) in (str,bool,int,float): return value
    return repr(value)


class QualityError(RuntimeError):
    pass


def validate_policy(raw):
    policy = raw.get('quality_policy',raw.get('physics',{}).get('quality_policy',{}))
    if 'quality_policy' in raw and 'quality_policy' in raw.get('physics',{}) and raw['quality_policy'] != raw['physics']['quality_policy']:
        raise ValueError('Conflicting quality_policy declarations')
    if not isinstance(policy,dict) or set(policy)-{'default','overrides'}:
        raise ValueError('quality_policy requires default and overrides')
    default,overrides = policy.get('default','pass_through'), policy.get('overrides',{})
    if not isinstance(default,str) or default not in ACTIONS or not isinstance(overrides,dict) or set(overrides)-QUALITIES:
        raise ValueError('Invalid quality_policy action or quality')
    if any(not isinstance(action,str) or action not in ACTIONS for action in overrides.values()): raise ValueError('Invalid quality override action')
    return {'default':default,'overrides':dict(overrides)}


def source_metadata(snapshot, name):
    """Legacy flat snapshots default to finite/fresh; explicit UPSI metadata wins."""
    quality = snapshot.get('quality',{}).get(name,'good')
    return {'quality':quality,
            'valid':snapshot.get('validity',{}).get(name,quality!='initial'),
            'source_time_s':snapshot.get('measurement_times',{}).get(name,snapshot.get('sim_time')),
            'observation_time_s':snapshot.get('sim_time'),
            'freshness':snapshot.get('freshness',{}).get(name,'initial' if quality=='initial' else
                ('unknown' if snapshot.get('sim_time') is None else 'fresh'))}


class QualityGate:
    def __init__(self, raw=None, audit_path=None):
        self.policy = validate_policy(raw or {})
        self.accepted = {}
        self.audit_path = Path(audit_path) if audit_path else None
        self.lock = threading.RLock()

    def _emit(self, record):
        if self.audit_path is not None:
            with self.lock: append_jsonl(self.audit_path,_audit_value(record))

    def decide(self, signal, value, metadata, *, destination, iteration, stage, source):
        meta = copy.deepcopy(metadata)
        quality = meta.get('quality','good')
        network = meta.get('network_freshness','fresh')
        finite = type(value) in (float,int) and math.isfinite(value)
        stamp,now=meta.get('source_time_s'),meta.get('observation_time_s')
        bad_clock = now is not None and (type(now) not in (int,float) or not math.isfinite(now))
        bad_time = bad_clock or stamp is not None and (type(stamp) not in (int,float) or not math.isfinite(stamp)
                                         or stamp < 0 or (now is not None and stamp>now))
        bad_meta = type(meta.get('valid',True)) is not bool or not isinstance(meta.get('freshness','fresh'),str) or meta.get('freshness','fresh') not in {'fresh','initial','delayed','stale','unknown'}
        bad_meta = bad_meta or not isinstance(network,str) or network not in {'fresh','stale','unknown'}
        if value is None: effective='missing'
        elif bad_time or bad_meta: effective='invalid'
        elif not finite or not isinstance(quality,str) or quality not in QUALITIES or quality=='invalid': effective='invalid'
        elif network in {'stale','unknown'} or meta.get('freshness') in {'stale','unknown'} or quality=='stale' or (stamp is not None and now is not None and stamp<now and meta.get('freshness')!='delayed'): effective='stale'
        else: effective=quality
        action=self.policy['overrides'].get(effective,self.policy['default'])
        record={'signal':signal,'quality':effective,'source_quality':quality,
                'validity':meta.get('valid',True), 'freshness':meta.get('freshness','fresh'),
                'network_freshness':network,'source_time_s':meta.get('source_time_s'),
                'decision':action,'reason':effective,'iteration':iteration,'stage':stage,
                'destination':destination,'source':source,'raw_value':value if finite or value is None else repr(value),
                'selected_value':None,'delivered_value':None,'delivered_source_time_s':None,
                'delivered_quality':None,'delivered_freshness':None,'write_status':'skipped',
                'source_metadata':meta}
        key=(destination,signal)
        # good/fresh values are accepted normally even under a hold-last policy.
        problem=effective!='good' or not meta.get('valid',True)
        if action=='fail_fast' and problem:
            record['write_status']='rejected';self._emit(record)
            raise QualityError(f'{destination}.{signal}: {effective} rejected by fail_fast')
        if action=='skip_write':
            record['reason']='explicit skip_write policy'
        elif action=='hold_last' and problem:
            with self.lock: previous=copy.deepcopy(self.accepted.get(key))
            if previous is not None:
                record['selected_value'],record['held_metadata']=previous
                record['reason']='holding last successfully delivered value'
            else: record['reason']='no previously accepted value to hold'
        elif finite and effective not in {'invalid','missing'} and (meta.get('valid',True) or quality=='initial'):
            record['selected_value']=float(value)
            record['reason']='finite configured initialization permitted' if effective=='initial' else 'explicit pass-through of source state/freshness'
        else: record['reason']='no usable numerical value; no zero substitution'
        return record['selected_value'],_audit_value(record)

    def finish(self, records, written=(), error=None):
        written=set(written)
        for record in records:
            if record['signal'] in written and record['selected_value'] is not None:
                record['delivered_value']=record['selected_value']
                record['write_status']='written'
                delivered=record.get('held_metadata',record['source_metadata'])
                record['delivered_source_time_s']=delivered.get('source_time_s')
                record['delivered_quality']=delivered.get('quality','good')
                record['delivered_freshness']='stale' if 'held_metadata' in record or record['quality']=='stale' else delivered.get('freshness','fresh')
                with self.lock:
                    self.accepted[(record['destination'],record['signal'])] = (
                        record['selected_value'],copy.deepcopy(record.get('held_metadata',record['source_metadata'])))
            elif record['selected_value'] is not None:
                record['write_status']='write_failed'
            if error: record['write_error']=str(error)
            self._emit(record)
