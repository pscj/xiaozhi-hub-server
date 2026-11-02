"""Seed persistent configuration once, then run the upstream application."""
import ipaddress
import os
from pathlib import Path

os.chdir('/app')
Path('data').mkdir(exist_ok=True)
Path('tmp').mkdir(exist_ok=True)
config = Path('data/.config.yaml')
if not config.exists():
    host = str(ipaddress.IPv4Address(os.environ['HUB_PUBLIC_HOST']))
    text = Path('/opt/router/config.router.yaml').read_text(encoding='utf-8')
    # Exclusive creation protects existing keys and passwords on restarts.
    fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        output.write(text.replace('ROUTER_LAN_IP', host))
    print('已生成 data/.config.yaml。请打开 /admin 设置密码及 API。', flush=True)

os.execvp('python', ['python', 'app.py'])
