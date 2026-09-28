"""Audit tracked files only; print names, never potential secret values."""
from pathlib import Path
import re
import subprocess

root = Path(__file__).resolve().parents[1]
files = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')
allowed = {'.py', '.md', '.txt', '.json', '.ps1', '.yml'}
special = {'.gitignore', 'LICENSE'}
patterns = ['-' * 5 + 'BEGIN ' + r'.*PRIVATE KEY', r'gh[pousr]_[A-Za-z0-9]{20,}', r'sk-[A-Za-z0-9_-]{24,}', r'(?i)[A-Z]:[\\/]+Users[\\/]', r'(?i)(?:api_key|password|secret|token)\s*[=:]\s*["\'][A-Za-z0-9_+/=-]{16,}["\']']
problems = []
total = 0
for name in filter(None, files):
    p = root / name
    data = p.read_bytes()
    total += len(data)
    if p.suffix not in allowed and p.name not in special:
        problems.append((name, 'not an approved source/text format'))
    if len(data) > 600_000 or b'\0' in data:
        problems.append((name, 'binary or unexpectedly large file'))
    if name.split('/')[0] in {'00_INBOX','01_PROJECTS','02_ASSETS','03_EXPORTS','models','output','tools','.env'}:
        problems.append((name, 'runtime/private path'))
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        problems.append((name, 'not UTF-8 text'))
        continue
    if any(re.search(pattern, text) for pattern in patterns):
        problems.append((name, 'credential or personal path pattern'))
if problems:
    for name, reason in problems:
        print(f'FAIL {name}: {reason}')
    raise SystemExit(1)
print(f'PASS: {len(list(filter(None, files)))} tracked text files, {total} bytes; no media, runtime paths or detected secrets.')
