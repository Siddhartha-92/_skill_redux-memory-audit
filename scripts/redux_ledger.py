#!/usr/bin/env python3
"""Deterministic, local Markdown coverage ledger. No Notion/network access.

This proves byte coverage and operator-recorded state transitions, NOT the quality of
an AI's historical interpretation or the truth of any external fact.
"""
import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

STATES = ("indexed", "reviewed", "reconciled", "verified")
VERSION = "1.0"


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for data in iter(lambda: f.read(1024 * 1024), b''):
            h.update(data)
    return h.hexdigest()


def atomic_json(path, data):
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('w', encoding='utf-8', newline='\n') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load(path):
    with path.open(encoding='utf-8') as f:
        return json.load(f)


def prepare(args):
    source = args.source.resolve(strict=True)
    output = args.out.resolve()
    if not source.is_file():
        raise ValueError('input must be a regular file')
    if args.max_lines < 1 or args.max_bytes < 1:
        raise ValueError('max-lines and max-bytes must be positive')
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / 'manifest.json'
    source_sha = digest(source)
    if manifest_path.exists():
        old = load(manifest_path)
        if old['source_sha256'] != source_sha or old['source_path'] != str(source):
            raise ValueError('source changed or different source under existing output; use a NEW output directory to preserve history')
        if old['max_lines'] != args.max_lines or old['max_bytes'] != args.max_bytes:
            raise ValueError('chunk-size configuration differs from existing manifest; resume with prior settings')
        errors = check_integrity(old, manifest_path)
        if errors:
            raise ValueError('existing manifest has integrity errors: ' + '; '.join(errors[:3]))
        print(f'RESUMED unchanged manifest: {manifest_path} ({len(old["chunks"])} chunks; progress preserved)')
        return 0

    chunks = []
    buffer = []
    count = 0
    start_line = 1
    start_byte = 0
    oversized = False
    current_line = 0
    consumed_bytes = 0

    def flush():
        nonlocal buffer, count, start_line, start_byte, oversized
        if not buffer:
            return
        n = len(chunks) + 1
        name = f'chunks/C{n:04d}-{start_line:07d}-{current_line:07d}.md'
        data = b''.join(buffer)
        p = output / name
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(data)
        chunks.append({
            'id': f'C{n:04d}', 'file': name, 'start_line': start_line,
            'end_line': current_line, 'byte_start': start_byte,
            'byte_end': start_byte + len(data), 'size_bytes': len(data),
            'sha256': hashlib.sha256(data).hexdigest(),
            'oversized_line': oversized, 'status': 'indexed',
            'proof_reconciled': '', 'proof_verified': '', 'notes': ''
        })
        buffer = []
        count = 0
        oversized = False
        start_line = current_line + 1
        start_byte = consumed_bytes

    with source.open('rb') as f:
        for line in f:
            current_line += 1
            if count and (len(buffer) >= args.max_lines or count + len(line) > args.max_bytes):
                current_line -= 1
                flush()
                current_line += 1
            buffer.append(line)
            count += len(line)
            consumed_bytes += len(line)
            oversized = oversized or len(line) > args.max_bytes
        flush()

    manifest = {
        'schema': 'redux-source-ledger-v1', 'tool_version': VERSION,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'source_path': str(source), 'source_filename': source.name,
        'source_sha256': source_sha, 'source_size_bytes': source.stat().st_size,
        'line_count': current_line, 'max_lines': args.max_lines,
        'max_bytes': args.max_bytes, 'chunks': chunks,
        'warning': 'Completeness is byte-coverage + operator-recorded review/reconciliation; it does not verify semantic quality.'
    }
    atomic_json(manifest_path, manifest)
    print(f'PREPARED: {current_line} lines -> {len(chunks)} chunks; sha256 {source_sha}; manifest {manifest_path}')
    return 0


def mark(args):
    m = load(args.manifest)
    errors = check_integrity(m, args.manifest)
    if errors:
        raise ValueError('integrity check failed: ' + '; '.join(errors[:3]))
    c = next((c for c in m['chunks'] if c['id'] == args.chunk_id), None)
    if c is None:
        raise ValueError('unknown chunk ID')
    old = c['status']
    target = args.state
    if target == old:
        print(f'UNCHANGED {args.chunk_id} {target}')
        return 0
    if STATES.index(target) != STATES.index(old) + 1:
        raise ValueError(f'unsupported transition {old} -> {target}; process stages sequentially')
    if target in ('reconciled', 'verified') and not (args.proof or '').strip():
        raise ValueError(f'{target} requires --proof (link/record ID, or explicit no-change evidence)')
    c['status'] = target
    if target == 'reconciled':
        c['proof_reconciled'] = args.proof.strip()
    if target == 'verified':
        c['proof_verified'] = args.proof.strip()
    if args.notes:
        c['notes'] = (c['notes'] + '\n' + args.notes).strip()
    atomic_json(args.manifest, m)
    print(f'MARKED {args.chunk_id} {old} -> {target}')
    return 0


def check_integrity(m, path):
    errors = []
    source = Path(m['source_path'])
    if not source.is_file():
        errors.append('source file absent: reattach file to validate or preserve original with manifest')
    elif digest(source) != m['source_sha256']:
        errors.append('source SHA256 changed')
    pieces = hashlib.sha256()
    prev_end_line = 0
    prev_end_byte = 0
    for ix, c in enumerate(m['chunks'], 1):
        if c['id'] != f'C{ix:04d}':
            errors.append(f'nonsequential chunk ID {c["id"]}')
        if c['start_line'] != prev_end_line + 1:
            errors.append(f'line gap/overlap before {c["id"]}')
        if c['byte_start'] != prev_end_byte:
            errors.append(f'byte gap/overlap before {c["id"]}')
        p = (path.parent / c['file']).resolve()
        if not p.is_relative_to(path.parent.resolve()):
            errors.append(f'unsafe path for {c["id"]}')
            continue
        if not p.is_file():
            errors.append(f'missing {c["id"]}')
            continue
        data = p.read_bytes()
        pieces.update(data)
        if len(data) != c['size_bytes'] or len(data) != c['byte_end'] - c['byte_start']:
            errors.append(f'size mismatch {c["id"]}')
        if hashlib.sha256(data).hexdigest() != c['sha256']:
            errors.append(f'SHA256 mismatch {c["id"]}')
        expected_count = len(data.splitlines(keepends=True))
        if expected_count != c['end_line'] - c['start_line'] + 1:
            errors.append(f'line count mismatch {c["id"]}')
        prev_end_line = c['end_line']
        prev_end_byte = c['byte_end']
    if prev_end_line != m['line_count']:
        errors.append('total line count mismatch')
    if prev_end_byte != m['source_size_bytes']:
        errors.append('total byte count mismatch')
    if pieces.hexdigest() != m['source_sha256']:
        errors.append('reconstructed SHA256 mismatch')
    return errors


def status(args, strict=False):
    m = load(args.manifest)
    errors = check_integrity(m, args.manifest)
    counts = Counter(c['status'] for c in m['chunks'])
    total = len(m['chunks'])
    verified = counts['verified']
    iscomplete = not errors and total == verified
    print('COMPLETE' if iscomplete else 'INVALID' if errors else 'PARTIAL')
    print(f'Source: {m["source_filename"]}  SHA256: {m["source_sha256"]}')
    print(f'Coverage: {m["line_count"]} lines; {total} chunks; verified {verified}/{total}')
    print('Stages: ' + ', '.join(f'{state}={counts[state]}' for state in STATES))
    if errors:
        print('Errors:\n  ' + '\n  '.join(errors))
    if not iscomplete:
        pending = [f'{c["id"]} L{c["start_line"]}-{c["end_line"]} [{c["status"]}]' for c in m['chunks'] if c['status'] != 'verified']
        print('To do: ' + (', '.join(pending[:12]) or 'none') + (' ...' if len(pending) > 12 else ''))
    if errors:
        return 1
    return (2 if strict and not iscomplete else 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare', help='Index a complete source without truncating or resetting existing review state')
    p.add_argument('source', type=Path)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--max-lines', type=int, default=600)
    p.add_argument('--max-bytes', type=int, default=40000)
    p = sub.add_parser('mark', help='Advance a chunk one review stage')
    p.add_argument('manifest', type=Path)
    p.add_argument('chunk_id')
    p.add_argument('state', choices=STATES[1:])
    p.add_argument('--proof', default='')
    p.add_argument('--notes', default='')
    for name in ('status', 'verify'):
        p = sub.add_parser(name)
        p.add_argument('manifest', type=Path)
    args = ap.parse_args()
    try:
        return {'prepare': lambda: prepare(args), 'mark': lambda: mark(args),
                'status': lambda: status(args), 'verify': lambda: status(args, strict=True)}[args.command]()
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
