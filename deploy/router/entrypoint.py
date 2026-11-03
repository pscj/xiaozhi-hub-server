"""Preserve upstream settings, initialize persistent state, then drop to a normal user."""
import ipaddress
import os
from pathlib import Path
import pwd
import sys
import yaml


def initialize_config(server, root, router_config, host, default_llm='ClaudeAPI'):
    config = server / 'data/.config.yaml'
    if config.exists():
        return
    host = str(ipaddress.IPv4Address(host))
    original = yaml.safe_load((root / 'scripts/config.template.yaml').read_text(encoding='utf-8'))
    overrides = yaml.safe_load(router_config.read_text(encoding='utf-8').replace('ROUTER_LAN_IP', host))
    original.setdefault('server', {}).update(overrides['server'])
    original['selected_module']['LLM'] = default_llm
    fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        yaml.safe_dump(original, output, allow_unicode=True, sort_keys=False)
    print('已按原安装模板初始化配置；现有配置在重启时不会被覆盖。', flush=True)


def prepare_writable_directories(server, home, uid, gid):
    writable = [server / 'data', server / 'tmp', server / 'models', home,
                server / 'config/assets/wakeup_words']
    for directory in writable:
        directory.mkdir(parents=True, exist_ok=True)
        if os.geteuid() == 0:
            os.chown(directory, uid, gid)
            for current, dirs, files in os.walk(directory, followlinks=False):
                for name in dirs + files:
                    os.chown(Path(current) / name, uid, gid, follow_symlinks=False)


def main():
    server = Path(os.environ.get('HUB_SERVER_DIR', '/opt/xiaozhi-hub/main/xiaozhi-server'))
    root = server.parents[1]
    home = Path('/home/xiaozhi')
    os.chdir(server)
    user = pwd.getpwnam('xiaozhi')
    prepare_writable_directories(server, home, user.pw_uid, user.pw_gid)

    # API is an initial preference, not a restriction on available providers.
    initialize_config(server, root, Path('/opt/router/config.router.yaml'),
                      os.environ['HUB_PUBLIC_HOST'], os.environ.get('HUB_DEFAULT_LLM', 'ClaudeAPI'))

    if os.geteuid() == 0:
        # initialize_config creates a root-owned 0600 file after directory setup.
        os.chown(server / 'data/.config.yaml', user.pw_uid, user.pw_gid)
        os.setgroups([])
        os.setgid(user.pw_gid)
        os.setuid(user.pw_uid)
    os.environ['HOME'] = str(home)
    os.environ['USER'] = os.environ['LOGNAME'] = user.pw_name
    (home / 'xiaozhi-workspace').mkdir(exist_ok=True)
    os.execv(sys.executable, [sys.executable, 'app.py'])


if __name__ == '__main__':
    main()
