"""Offline orchestration tests. Docker is simulated; no daemon or real data is used."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tarfile

import pytest

ROUTER = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('redeploy_plan', ROUTER / 'redeploy_plan.py')
planner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(planner)


def original(root):
    server = planner.SERVER
    return {
        'Config': {'Env': ['HUB_PUBLIC_HOST=192.168.1.1', 'TOKEN=synthetic $x `echo bad`\nsecond line'],
                   'Entrypoint': ['python', '/opt/router/entrypoint.py'], 'Cmd': None, 'User': ''},
        'HostConfig': {'NetworkMode': 'default', 'RestartPolicy': {'Name': 'unless-stopped'},
                       'Memory': 512 * 1024 * 1024, 'SecurityOpt': ['no-new-privileges'],
                       'PortBindings': {'16688/tcp': [{'HostIp': '192.168.1.1', 'HostPort': '16688'}]}},
        'State': {'Running': True},
        'Mounts': [{'Type': 'bind', 'Source': str(root / name), 'Destination': target, 'RW': True}
                   for name, target in [('data', server + '/data'), ('tmp', server + '/tmp'),
                                        ('home', '/home/xiaozhi'), ('core', server + '/core')]] +
                  [{'Type': 'volume', 'Name': 'xiaozhi-hub-models', 'Source': '/unused',
                    'Destination': server + '/models', 'RW': True}],
    }


def test_plan_keeps_credentials_limits_and_state_but_uses_image_code(tmp_path):
    info = original(tmp_path)
    args, backups = planner.plan([info], 'new:image', str(tmp_path), '/backup')
    assert shlex.split(shlex.join(args)) == args
    assert info['Config']['Env'][1] in args
    assert str(512 * 1024 * 1024) in args
    assert any('source=xiaozhi-hub-models' in arg for arg in args)
    assert not any('target=' + planner.SERVER + '/core' in arg for arg in args)
    assert 'MUSEGADGET_STATE_DIR=' + planner.SERVER + '/data/musegadget' in args
    assert len(backups) == 2


@pytest.mark.parametrize('kind', ['network', 'missing_data', 'volatile_muse', 'entrypoint'])
def test_unsafe_or_unmanaged_deployment_is_rejected(tmp_path, kind):
    info = original(tmp_path)
    if kind == 'network':
        info['HostConfig']['NetworkMode'] = 'host'
    elif kind == 'missing_data':
        info['Mounts'] = info['Mounts'][1:]
    elif kind == 'volatile_muse':
        info['Config']['Env'].append('MUSEGADGET_STATE_DIR=/var/lib/musegadget')
    else:
        info['Config']['Entrypoint'] = ['sh', 'custom.sh']
    with pytest.raises(ValueError):
        planner.plan([info], 'new:image', str(tmp_path), '/backup')


def test_existing_persistent_muse_directory_is_preserved(tmp_path):
    info = original(tmp_path)
    custom = 'MUSEGADGET_STATE_DIR=/home/xiaozhi/private-muse'
    info['Config']['Env'].append(custom)
    args, _ = planner.plan([info], 'new:image', str(tmp_path), '/backup')
    assert custom in args
    assert sum(a.startswith('MUSEGADGET_STATE_DIR=') for a in args) == 1


FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
state_path = Path(os.environ['MOCK_STATE'])
state = json.loads(state_path.read_text())
a = sys.argv[1:]
with open(os.environ['MOCK_LOG'], 'a') as f: f.write(json.dumps(a) + '\n')
def save(): state_path.write_text(json.dumps(state))
if a[0] == 'info': pass
elif a[0] == 'inspect':
    name = a[-1]
    if name not in state: sys.exit(1)
    if '--format' in a: print(str(state[name]['State']['Running']).lower())
    else: print(json.dumps([state[name]]))
elif a[0] == 'build': sys.exit(1 if os.environ.get('FAIL_BUILD') else 0)
elif a[0] == 'run':
    i = a.index('/opt/router/redeploy_plan.py')
    sys.exit(subprocess.call([sys.executable, os.environ['MOCK_PLAN']] + a[i+1:], stdin=sys.stdin))
elif a[0] == 'rename': state[a[2]] = state.pop(a[1]); save()
elif a[0] == 'stop': state[a[-1]]['State']['Running'] = False; save()
elif a[0] == 'create':
    import copy
    if state:
        item = copy.deepcopy(next(iter(state.values())))
        item['Mounts'] = [m for m in item['Mounts'] if not m['Destination'].endswith('/core')]
    else:
        item = {'Config': {'Env': ['HUB_PUBLIC_HOST=192.168.1.1'], 'Entrypoint': ['python', '/opt/router/entrypoint.py'], 'Cmd': None}, 'HostConfig': {'NetworkMode': 'default'}, 'Mounts': []}
    item.update({'State': {'Running': False}, 'new': True})
    state['xiaozhi-hub'] = item
    save(); print('synthetic-id')
elif a[0] == 'start':
    item = state[a[-1]]
    item['State']['Running'] = not (item.get('new') and os.environ.get('FAIL_HEALTH'))
    save()
elif a[0] == 'exec': sys.exit(1 if os.environ.get('FAIL_HEALTH') else 0)
elif a[0] == 'rm': state.pop(a[-1], None); save()
elif a[0] in ('tag', 'logs'): pass
else: sys.exit('unexpected docker command: ' + a[0])
'''


@pytest.fixture
def deployment(tmp_path):
    repo = tmp_path / 'repo'
    task = repo / 'deploy/router'
    task.mkdir(parents=True)
    for name in ('redeploy.sh', 'redeploy_plan.py'):
        shutil.copy(ROUTER / name, task / name)
    for name in ('data', 'tmp', 'home', 'core'):
        (task / name).mkdir()
    (task / 'data/settings').write_text('synthetic-settings')
    (task / 'data/musegadget').mkdir()
    (task / 'data/musegadget/pairing.json').write_text('synthetic-pairing')
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'xiaozhi-hub': original(task)}))
    log = tmp_path / 'commands.log'
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    fake = bindir / 'docker'
    fake.write_text(FAKE_DOCKER)
    fake.chmod(0o755)
    env = {**os.environ, 'PATH': str(bindir) + os.pathsep + os.environ['PATH'],
           'MOCK_STATE': str(state), 'MOCK_LOG': str(log), 'MOCK_PLAN': str(task / 'redeploy_plan.py')}
    return task, state, log, env


def run(deployment, **flags):
    task, state, log, env = deployment
    result = subprocess.run(['sh', str(task / 'redeploy.sh')], env={**env, **flags},
                            text=True, capture_output=True, timeout=15)
    return result, json.loads(state.read_text()), [json.loads(line) for line in log.read_text().splitlines()]


def test_success_backs_up_pairing_and_preserves_previous_container(deployment):
    task, _, _, _ = deployment
    result, state, commands = run(deployment)
    assert result.returncode == 0, result.stderr
    assert state['xiaozhi-hub']['State']['Running']
    assert len(state) == 2
    assert not next(v for k, v in state.items() if k != 'xiaozhi-hub')['State']['Running']
    with tarfile.open(next((task / 'backups').glob('*/data.tar'))) as archive:
        assert archive.extractfile('./musegadget/pairing.json').read() == b'synthetic-pairing'
    assert not (task / '.redeploy.lock').exists()
    assert 'synthetic $x' not in result.stdout + result.stderr
    assert next(i for i, a in enumerate(commands) if a[0] == 'build') < next(i for i, a in enumerate(commands) if a[0] == 'stop')


def test_build_failure_never_stops_existing_service(deployment):
    result, state, commands = run(deployment, FAIL_BUILD='1')
    assert result.returncode != 0
    assert state['xiaozhi-hub']['State']['Running']
    assert not any(a[0] in ('stop', 'rename', 'rm') for a in commands)


def test_failed_start_restores_previous_container(deployment):
    result, state, commands = run(deployment, FAIL_HEALTH='1')
    assert result.returncode != 0
    assert list(state) == ['xiaozhi-hub']
    assert state['xiaozhi-hub']['State']['Running'] and not state['xiaozhi-hub'].get('new')


def test_backup_failure_restores_previous_container(deployment):
    task, state_path, _, _ = deployment
    info = json.loads(state_path.read_text())
    info['xiaozhi-hub']['Mounts'][0]['Source'] = str(task / 'missing-data')
    state_path.write_text(json.dumps(info))
    result, state, _ = run(deployment)
    assert result.returncode != 0
    assert state['xiaozhi-hub']['State']['Running']



def test_repeated_deployments_preserve_data(deployment):
    task, _, _, _ = deployment
    first, _, _ = run(deployment)
    assert first.returncode == 0, first.stderr
    second, state, _ = run(deployment)
    assert second.returncode == 0, second.stderr
    assert state["xiaozhi-hub"]["State"]["Running"]
    assert (task / "data/settings").read_text() == "synthetic-settings"
    assert len(list((task / "backups").glob("*/data.tar"))) == 2


def test_initial_plan_requires_ip_and_persists_muse(tmp_path):
    with pytest.raises(ValueError):
        planner.plan([], "new:image", str(tmp_path), "/backup", "")
    args, backups = planner.plan([], "new:image", str(tmp_path), "/backup", "192.168.1.1")
    assert "192.168.1.1:16689:16689" in args
    assert "MUSEGADGET_STATE_DIR=" + planner.SERVER + "/data/musegadget" in args
    assert len(backups) == 2


def test_backup_inside_source_is_rejected(tmp_path):
    info = original(tmp_path)
    with pytest.raises(ValueError):
        planner.plan([info], "new:image", str(tmp_path), str(tmp_path / "data/backups"))
