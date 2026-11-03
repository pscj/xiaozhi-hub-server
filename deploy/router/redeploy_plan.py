"""Generate shell-quoted Docker arguments from the existing router deployment.

Runs inside the freshly built image; no host Python or Docker socket mount is needed.
"""
import json
import os
import shlex
import sys

SERVER = '/opt/xiaozhi-hub/main/xiaozhi-server'


def plan(inspected, image, task_dir, backup_dir, lan_ip=''):
    old = inspected[0] if inspected else None
    args = ['docker', 'create', '--name', 'xiaozhi-hub']
    backups = []
    muse_state = None
    if old:
        host = old['HostConfig']
        if host.get('NetworkMode', 'default') not in ('default', 'bridge'):
            raise ValueError('仅支持默认 bridge 网络；自定义网络请沿用原部署工具更新')
        for field in ('Privileged', 'Devices', 'DeviceRequests', 'VolumesFrom', 'Links',
                      'AutoRemove', 'PublishAllPorts'):
            if host.get(field):
                raise ValueError('检测到自定义 Docker 参数 ' + field + '，请沿用原部署工具更新')
        networks = (old.get('NetworkSettings') or {}).get('Networks') or {}
        if len(networks) > 1 or any(n.get('IPAMConfig') for n in networks.values()):
            raise ValueError('检测到自定义网络或固定容器 IP，请沿用原部署工具更新')
        cfg = old['Config']
        if cfg.get('Entrypoint') != ['python', '/opt/router/entrypoint.py'] or cfg.get('Cmd'):
            raise ValueError('不是标准 router 镜像入口，请沿用原部署工具更新')
        if cfg.get('User') not in ('', 'root', None):
            raise ValueError('检测到自定义运行用户，请沿用原部署工具更新')
        policy = host.get('RestartPolicy') or {}
        restart = policy.get('Name') or 'unless-stopped'
        if restart == 'on-failure' and policy.get('MaximumRetryCount'):
            restart += ':' + str(policy['MaximumRetryCount'])
        args += ['--restart', restart]
        for env in cfg.get('Env') or []:
            if env.startswith('MUSEGADGET_STATE_DIR='):
                muse_state = env.split('=', 1)[1]
            args += ['--env', env]  # shlex.quote also preserves multiline values
        log = host.get('LogConfig') or {}
        if log.get('Type'):
            args += ['--log-driver', log['Type']]
        for key, value in (log.get('Config') or {}).items():
            args += ['--log-opt', key + '=' + value]
        for key, flag in [('Memory', '--memory'), ('MemorySwap', '--memory-swap'),
                          ('MemoryReservation', '--memory-reservation'), ('NanoCpus', '--cpus'),
                          ('CpuShares', '--cpu-shares'), ('CpuPeriod', '--cpu-period'),
                          ('CpuQuota', '--cpu-quota'), ('CpusetCpus', '--cpuset-cpus'),
                          ('CpusetMems', '--cpuset-mems'), ('PidsLimit', '--pids-limit'),
                          ('ShmSize', '--shm-size')]:
            value = host.get(key)
            if value:
                args += [flag, str(value / 1e9) if key == 'NanoCpus' else str(value)]
        for key, flag in [('SecurityOpt', '--security-opt'), ('CapAdd', '--cap-add'),
                          ('CapDrop', '--cap-drop'), ('Dns', '--dns'),
                          ('DnsSearch', '--dns-search'), ('ExtraHosts', '--add-host')]:
            for value in host.get(key) or []:
                args += [flag, value]
        for item in host.get('Ulimits') or []:
            args += ['--ulimit', f"{item['Name']}={item['Soft']}:{item['Hard']}"]
        for port, bindings in (host.get('PortBindings') or {}).items():
            for binding in bindings or []:
                address = binding.get('HostIp') or '0.0.0.0'
                if ':' in address:
                    address = '[' + address + ']'
                args += ['--publish', f"{address}:{binding['HostPort']}:{port}"]
        mounts = old.get('Mounts') or []
        by_target = {m['Destination']: m for m in mounts}
        for name in ('data', 'tmp', 'models'):
            if SERVER + '/' + name not in by_target:
                raise ValueError('旧容器没有持久化 ' + name + '；请先迁移容器内数据再更新')
        if '/home/xiaozhi' not in by_target:
            raise ValueError('旧容器没有持久化 home；请先迁移 Claude 登录和用户目录再更新')
        for mount in mounts:
            target = mount['Destination']
            if target == SERVER + '/core':
                continue  # The rebuilt image owns this code, making image updates reproducible.
            if target in (SERVER, '/opt/xiaozhi-hub', '/'):
                raise ValueError('检测到覆盖服务端源码的挂载，请沿用原部署工具更新')
            kind = mount['Type']
            if kind not in ('bind', 'volume'):
                raise ValueError('不支持的挂载类型：' + kind)
            source = mount['Name'] if kind == 'volume' else mount['Source']
            if ',' in source or ',' in target:
                raise ValueError('挂载路径包含逗号，请沿用原部署工具更新')
            value = f'type={kind},source={source},target={target}'
            if not mount.get('RW', True):
                value += ',readonly'
            if kind == 'bind' and mount.get('Propagation') not in (None, '', 'rprivate'):
                value += ',bind-propagation=' + mount['Propagation']
            args += ['--mount', value]
            if target in (SERVER + '/data', '/home/xiaozhi'):
                if kind != 'bind':
                    raise ValueError('data/home 使用命名卷时无法自动备份，请沿用原部署工具更新')
                if os.path.commonpath([source, backup_dir]) == os.path.normpath(source):
                    raise ValueError('备份目录位于数据挂载内，请使用标准的独立 data/home 目录')
                filename = 'data.tar' if target.endswith('/data') else 'home.tar'
                backups.append(['tar', '-C', source, '-cpf', os.path.join(backup_dir, filename), '.'])
    else:
        import ipaddress
        ip = ipaddress.IPv4Address(lan_ip)
        if ip.is_loopback or ip.is_unspecified or ip.is_multicast:
            raise ValueError('请指定可用的 LAN IPv4 地址')
        args += ['--restart', 'unless-stopped', '--security-opt', 'no-new-privileges',
                 '--log-driver', 'json-file', '--log-opt', 'max-size=5m', '--log-opt', 'max-file=2',
                 '--env', 'HUB_PUBLIC_HOST=' + str(ip), '--env', 'HUB_DEFAULT_LLM=ClaudeAPI']
        for port in (16688, 16689):
            args += ['--publish', f'{ip}:{port}:{port}']
        for name, target in [('data', SERVER + '/data'), ('tmp', SERVER + '/tmp'),
                             ('home', '/home/xiaozhi')]:
            source = os.path.join(task_dir, name)
            args += ['--volume', source + ':' + target]
            if name != 'tmp':
                backups.append(['tar', '-C', source, '-cpf', os.path.join(backup_dir, name + '.tar'), '.'])
        args += ['--volume', 'xiaozhi-hub-models:' + SERVER + '/models']
    if muse_state:
        if not any(m.get('RW', True) and (muse_state == m['Destination'] or muse_state.startswith(m['Destination'].rstrip('/') + '/')) for m in mounts):
            raise ValueError('现有 Muse 状态目录没有持久化挂载，请先迁移配对数据')
    else:
        args += ['--env', 'MUSEGADGET_STATE_DIR=' + SERVER + '/data/musegadget']
    args.append(image)
    return args, backups


if __name__ == '__main__':
    try:
        args, backups = plan(json.load(sys.stdin), *sys.argv[1:])
        # Shell commands are written to private files by the host shell, never printed to its terminal.
        print('create_container() {')
        print('  ' + shlex.join(args))
        print('}')
        print('backup_data() {')
        for command in backups:
            print('  ' + shlex.join(command))
        print('}')
    except (ValueError, KeyError) as exc:
        sys.exit('无法自动部署：' + str(exc))
