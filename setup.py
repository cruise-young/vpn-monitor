"""One-shot installer for the local Mihomo traffic monitor.

Wires the monitor into Clash Verge (startup script + Web UI entry), prepares the
data directory and, on macOS, installs a LaunchAgent. Standard library only.
"""
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from xml.sax.saxutils import escape

APP_ID = 'io.github.clash-verge-rev.clash-verge-rev'
ROOT = Path(__file__).resolve().parent
IS_WINDOWS = os.name == 'nt'
MONITOR_URL = 'http://127.0.0.1:18769/'
LAUNCH_AGENT = 'io.github.clash-verge-rev.vpn-monitor'


def clash_config_dir():
    override = os.environ.get('VPN_MONITOR_CONFIG_DIR')
    if override:
        return Path(override)
    if IS_WINDOWS:
        base = Path(os.environ.get('APPDATA') or (Path.home() / 'AppData' / 'Roaming'))
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Application Support'
    else:
        base = Path(os.environ.get('XDG_DATA_HOME') or (Path.home() / '.local' / 'share'))
    return base / APP_ID


def yaml_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def set_scalar(text, key, value):
    line = f'{key}: {yaml_quote(value)}'
    pattern = re.compile(rf'^{re.escape(key)}:[^\n]*$', re.M)
    if pattern.search(text):
        return pattern.sub(lambda _match: line, text, count=1)
    if text and not text.endswith('\n'):
        text += '\n'
    return text + line + '\n'


def add_web_ui(text, url):
    lines = text.splitlines()
    key_pattern = re.compile(r'^web_ui_list:[ \t]*(.*)$')
    for index, line in enumerate(lines):
        match = key_pattern.match(line)
        if not match:
            continue
        value = match.group(1).strip()
        if value.startswith('['):
            if url in value:
                return text
            inner = value[1:-1].strip() if value.endswith(']') else ''
            items = inner if inner and inner not in ('null', '~') else ''
            lines[index] = 'web_ui_list: [' + (items + ', ' if items else '') + yaml_quote(url) + ']'
            return _join(lines, text)
        end = index + 1
        while end < len(lines):
            candidate = lines[end]
            if candidate.strip() == '' or candidate.startswith('-') or candidate[:1] in (' ', '\t'):
                end += 1
                continue
            break
        block = lines[index + 1:end]
        if any(url in item for item in block):
            return text
        lines[index] = 'web_ui_list:'
        insert_at = end
        while insert_at > index + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines.insert(insert_at, '- ' + yaml_quote(url))
        return _join(lines, text)
    if text and not text.endswith('\n'):
        text += '\n'
    return text + 'web_ui_list:\n- ' + yaml_quote(url) + '\n'


def _join(lines, original):
    result = '\n'.join(lines)
    return result + ('\n' if original.endswith('\n') or not original else '\n')


def patch_verge(text):
    script = ROOT / ('start.ps1' if IS_WINDOWS else 'start.sh')
    text = set_scalar(text, 'startup_script', str(script))
    text = set_scalar(text, 'env_type', 'powershell' if IS_WINDOWS else 'sh')
    return add_web_ui(text, MONITOR_URL)


def install_launch_agent(python_exe):
    agents = Path.home() / 'Library' / 'LaunchAgents'
    agents.mkdir(parents=True, exist_ok=True)
    plist = agents / f'{LAUNCH_AGENT}.plist'
    log = ROOT / 'data' / 'launchd.log'
    script = ROOT / 'vpn_monitor.py'
    plist.write_text(f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LAUNCH_AGENT}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{escape(str(python_exe))}</string>
    <string>{escape(str(script))}</string>
  </array>
  <key>WorkingDirectory</key><string>{escape(str(ROOT))}</string>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{escape(str(log))}</string>
  <key>StandardErrorPath</key><string>{escape(str(log))}</string>
</dict>
</plist>
''', encoding='utf-8')
    subprocess.run(['launchctl', 'unload', str(plist)], capture_output=True, check=False)
    result = subprocess.run(['launchctl', 'load', '-w', str(plist)], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(f'WARNING: launchctl load failed: {result.stderr.strip()}')
    return plist


def main():
    parser = argparse.ArgumentParser(description='Configure Clash Verge integration and autostart.')
    parser.add_argument('--no-autostart', action='store_true', help='skip macOS LaunchAgent installation')
    parser.add_argument('--dry-run', action='store_true', help='show the result without writing')
    args = parser.parse_args()

    (ROOT / 'data').mkdir(parents=True, exist_ok=True)
    config = clash_config_dir() / 'verge.yaml'
    print(f'Project: {ROOT}')
    print(f'Clash Verge config: {config}')
    if config.exists():
        original = config.read_text(encoding='utf-8')
        updated = patch_verge(original)
        if updated == original:
            print('Clash Verge config already up to date.')
        elif args.dry_run:
            print('Would update startup_script, env_type and web_ui_list in the Clash Verge config.')
        else:
            backup = config.with_name(f'verge.yaml.bak-{time.strftime("%Y%m%d-%H%M%S")}')
            shutil.copy2(config, backup)
            config.write_text(updated, encoding='utf-8')
            print(f'Updated Clash Verge config (backup: {backup.name}).')
        print('Restart Clash Verge to apply the startup script and Web UI entry.')
    else:
        print('WARNING: verge.yaml not found. Install and start Clash Verge once, then rerun this script.')

    if sys.platform == 'darwin' and not args.no_autostart:
        python_exe = shutil.which('python3') or '/usr/bin/python3'
        if args.dry_run:
            print(f'Would install LaunchAgent {LAUNCH_AGENT} using {python_exe}.')
        else:
            plist = install_launch_agent(python_exe)
            print(f'Installed LaunchAgent: {plist}')
    elif IS_WINDOWS:
        print('Windows login autostart: run install-autostart.ps1 (setup.ps1 does this automatically).')

    print(f'Next: start the monitor, then open {MONITOR_URL}')


if __name__ == '__main__':
    main()
