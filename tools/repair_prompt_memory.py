"""Targeted legacy QQ prompt repair. Dry-run by default; backups precede edits."""
import argparse
import copy
import importlib.util
import json
import re
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('turn_guard', SOURCE / 'plugin_tool_turn_guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
HEADER = re.compile(
    r'^\[系统\] (?:对方连续发了(?: \d+ 条消息|以下消息)，请用一两句话自然总结回复：|'
    r'下面是你和(?:群友|对方的)的对话，(?:请自然接话总结回复（不要复述，用一两句话）：|'
    r'如果你觉得需要接话就自然回复（不要复述，用一两句话）；如果没必要回可以不回，不回复是正常的。))\n'
)


def clean_text(text):
    return HEADER.sub('', guard.strip_tool_instruction_suffix(text), count=1)


def clean_row(row):
    result = copy.deepcopy(row)
    if result.get('type') != 'human':
        return result
    data = result.get('data', {})
    content = data.get('content')
    if isinstance(content, str):
        data['content'] = clean_text(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get('type') == 'text':
                part['text'] = clean_text(part['text'])
    return result


def request(base, route, payload=None):
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(base + route, data=body, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--memory-dir', type=Path, required=True)
    parser.add_argument('--character', required=True)
    parser.add_argument('--base-url', default='http://127.0.0.1:48911')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    folder = (args.memory_dir / args.character).resolve()
    if folder.parent != args.memory_dir.resolve():
        raise ValueError('Character must be a direct child of memory-dir')
    path = folder / 'time_indexed.db'
    conn = sqlite3.connect(path.as_uri() + ('?mode=rw' if args.apply else '?mode=ro'), uri=True)
    updates = []
    for ident, raw in conn.execute('SELECT id,message FROM time_indexed_original'):
        try:
            row = json.loads(raw)
        except (ValueError, TypeError):
            continue
        cleaned = clean_row(row)
        if cleaned != row:
            updates.append((json.dumps(cleaned, ensure_ascii=False), ident, raw))
    filename = f'recent_{args.character}.json'
    route = '/api/memory/recent_file?' + urllib.parse.urlencode({'filename': filename})
    snapshot = request(args.base_url, route)
    rows = json.loads(snapshot['content'])
    cleaned_rows = [clean_row(row) for row in rows]
    count = sum(a != b for a, b in zip(rows, cleaned_rows))
    report = {'original_rows': len(updates), 'recent_rows': count, 'applied': False}
    if args.apply:
        backup = folder / ('prompt-repair-backup-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
        backup.mkdir()
        with sqlite3.connect(backup / 'time_indexed.db') as target:
            conn.backup(target)
        (backup / 'recent.snapshot.json').write_text(snapshot['content'], encoding='utf8')
        # SQLite CAS updates preserve concurrent writes and all IDs/timestamps.
        with conn:
            for update in updates:
                if conn.execute('UPDATE time_indexed_original SET message=? WHERE id=? AND message=?', update).rowcount != 1:
                    raise RuntimeError('History changed concurrently; transaction rolled back')
        if count:
            chat = []
            for index, row in enumerate(cleaned_rows):
                content = row.get('data', {}).get('content', '')
                if isinstance(content, list):
                    if any(not isinstance(p, dict) or p.get('type') != 'text' for p in content):
                        raise RuntimeError('Non-text recent row: refuse lossy browser save')
                    content = ''.join(p['text'] for p in content)
                chat.append({'role': row['type'], 'text': content, 'source_index': index})
            result = request(args.base_url, '/api/memory/recent_file/save', {
                'filename': filename, 'chat': chat,
                'fingerprint': snapshot['fingerprint'], 'identity_token': snapshot['identity_token'],
            })
            if not result.get('success'):
                raise RuntimeError(f'Recent save failed: {result}')
        report.update(applied=True, backup=str(backup), changed_ids=[row[1] for row in updates])
        (backup / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    conn.close()
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
