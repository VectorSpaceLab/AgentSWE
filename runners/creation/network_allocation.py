"""Allocate small per-job Docker subnets without pruning/restarting Docker."""
from __future__ import annotations
import fcntl
import hashlib
import ipaddress
import json
import os
import subprocess
from pathlib import Path


def occupied_networks():
    ids=subprocess.check_output(['docker','network','ls','-q'],text=True).split()
    # As in the paper's final Creation evaluation: inspect networks one by one and skip ids that vanished between `ls` and
    # `inspect` (concurrent Harbor jobs tear networks down); a batch inspect fails outright.
    rows=[]
    for _id in ids:
        try: rows.extend(json.loads(subprocess.check_output(['docker','network','inspect',_id],text=True,stderr=subprocess.DEVNULL)))
        except subprocess.CalledProcessError: continue
    occupied=[]
    for row in rows:
        for record in ((row.get('IPAM') or {}).get('Config') or []):
            if record.get('Subnet'):
                occupied.append(ipaddress.ip_network(record['Subnet']))
    routes=json.loads(subprocess.check_output(['ip','-j','route','show'],text=True))
    for route in routes:
        if route.get('dst') and route['dst']!='default':
            occupied.append(ipaddress.ip_network(route['dst']))
    return occupied,rows


def allocate(compose: Path, pool: str, state_dir: Path) -> tuple[str,str]:
    block=ipaddress.ip_network(pool)
    if block.version!=4 or not block.is_private or not 16<=block.prefixlen<=24:
        raise ValueError('Create network pool must be an explicit private IPv4 /16 through /24')
    owner=hashlib.sha256(str(compose.resolve()).encode()).hexdigest()[:20]
    state_dir.mkdir(parents=True,exist_ok=True)
    state_path=state_dir/'allocations.json'
    with (state_dir/'allocation.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        state=json.loads(state_path.read_text()) if state_path.exists() else {}
        occupied,networks=occupied_networks()
        if owner in state:
            chosen=ipaddress.ip_network(state[owner]['subnet'])
            matching=[n for n in networks if any(x.get('Subnet')==str(chosen) for x in ((n.get('IPAM') or {}).get('Config') or []))]
            if matching and all(n.get('Labels',{}).get('agentswe.infra.owner')==owner for n in matching):
                return str(chosen),owner
            if not any(chosen.overlaps(net) for net in occupied if net.version==4):
                return str(chosen),owner
            raise RuntimeError('Previously reserved Create subnet is now occupied by another network')
        reserved=[ipaddress.ip_network(v['subnet']) for v in state.values()]
        for chosen in block.subnets(new_prefix=28):
            if any(chosen.overlaps(net) for net in [*occupied,*reserved] if net.version==4):
                continue
            state[owner]={'subnet':str(chosen),'compose':str(compose.resolve())}
            temporary=state_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(state,indent=2)+'\n')
            os.replace(temporary,state_path)
            return str(chosen),owner
        raise RuntimeError('Explicit Create subnet pool exhausted; no existing networks were removed')
