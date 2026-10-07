"""Bounded, credential-free diagnostic log bundle; never a recovery input."""
import argparse
import base64
import gzip
import hashlib
import io
import json
from pathlib import Path
import re

MAX_RAW = 64 * 1024 * 1024
MAX_GZIP = 8 * 1024 * 1024
CHUNK_BYTES = 24000
FIXED = frozenset(('result-run.json', 'result-abort.json', 'work/audit.json',
                   'work/rpc-audit.json', 'work/recovery/ledger.jsonl', 'work/recovery.seal.json'))
RESPONSE = re.compile(r'work/recovery/responses/[1-9][0-9]*-[0-9a-f]{64}\.json')
SENSITIVE_KEYS = frozenset(('authorization', 'proxy-authorization', 'cookie', 'set-cookie',
                            'password', 'passwd', 'access_token', 'api_key', 'gh_token',
                            'github_token', 'httpheaders', 'requestheaders', 'environment', 'env'))


class AuditError(RuntimeError):
    pass


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def allowed(path):
    return path in FIXED or RESPONSE.fullmatch(path) is not None


def safe_json(raw, forbidden=()):
    if any(secret and secret.encode() in raw for secret in forbidden):
        raise AuditError('credential bytes found in diagnostic input')
    if re.search(rb'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})', raw):
        raise AuditError('credential pattern found in diagnostic input')
    value = json.loads(raw)
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if any(key.lower() in SENSITIVE_KEYS for key in item):
                raise AuditError('sensitive field found in diagnostic input')
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return value


def read_file(root, name):
    path = root / name
    if (not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve())
            or any(p.is_symlink() for p in path.parents if p != root and p.is_relative_to(root))
            or path.stat().st_size > MAX_RAW):
        raise AuditError('missing, unsafe or oversized diagnostic file')
    return path.read_bytes()


def make_bundle(root, metadata, forbidden=()):
    root = Path(root)
    files = {name: read_file(root, name) for name in FIXED if (root / name).exists()}
    if sum(map(len, files.values())) > MAX_RAW:
        raise AuditError('raw diagnostic byte limit exceeded')
    ledger = files.get('work/recovery/ledger.jsonl')
    if ledger is not None:
        from recovery import LocalSealStore, _read_ledger
        if 'work/recovery.seal.json' not in files:
            raise AuditError('journal lacks its independent seal')
        # Both files were bounded/symlink-checked above before journal parsing.
        seal = LocalSealStore(root / 'work/recovery.seal.json').read()
        records = _read_ledger(root / 'work/recovery', seal)
        for row in records:
            safe_json(json.dumps(row).encode(), forbidden)
            if row['kind'] == 'response':
                item = row['data']
                name = 'work/recovery/' + item['file']
                if not RESPONSE.fullmatch(name):
                    raise AuditError('invalid sealed response path')
                content = read_file(root, name)
                if len(content) != item['bytes'] or digest(content) != item['responseSha256']:
                    raise AuditError('sealed raw response digest or size changed')
                files[name] = content
                if sum(map(len, files.values())) > MAX_RAW:
                    raise AuditError('raw diagnostic byte limit exceeded')
    elif 'work/recovery.seal.json' in files:
        raise AuditError('independent seal lacks its journal')
    response_dir = root / 'work/recovery/responses'
    if response_dir.exists():
        actual = {path.relative_to(root).as_posix() for path in response_dir.rglob('*')
                  if path.is_file() or path.is_symlink()}
        if actual != {name for name in files if RESPONSE.fullmatch(name)}:
            raise AuditError('unsealed or unexpected raw response files exist')
    complete = any(json.loads(files[p]).get('stage') == 'complete'
                   for p in ('result-run.json', 'result-abort.json') if p in files)
    if complete and not {'work/audit.json', 'work/rpc-audit.json', 'work/recovery/ledger.jsonl',
                         'work/recovery.seal.json'} <= files.keys():
        raise AuditError('successful computation lacks complete diagnostic inputs')
    for name, content in files.items():
        if name != 'work/recovery/ledger.jsonl':
            safe_json(content, forbidden)
    safe_json(json.dumps(metadata).encode(), forbidden)
    archive = json.dumps({'schema': 1, 'metadata': metadata, 'files': {
        name: {'bytes': len(content), 'sha256': digest(content),
               'base64': base64.b64encode(content).decode()}
        for name, content in sorted(files.items())}}, separators=(',', ':'), sort_keys=True).encode()
    if len(archive) > MAX_RAW:
        raise AuditError('raw diagnostic archive limit exceeded')
    compressed = gzip.compress(archive, compresslevel=6, mtime=0)
    if len(compressed) > MAX_GZIP:
        raise AuditError('compressed diagnostic byte limit exceeded')
    encoded = base64.b64encode(compressed).decode()
    chunks = [encoded[i:i + CHUNK_BYTES] for i in range(0, len(encoded), CHUNK_BYTES)]
    header = {'schema': 1, 'chunks': len(chunks), 'files': len(files), 'rawBytes': len(archive),
              'gzipBytes': len(compressed), 'rawSha256': digest(archive), 'gzipSha256': digest(compressed)}
    return header, chunks


def emit_bundle(root, metadata, forbidden=(), emit=print):
    # Validate everything before printing the first chunk. The END record is
    # mandatory; partial/truncated Actions logs never count as a complete audit.
    header, chunks = make_bundle(root, metadata, forbidden)
    emit(json.dumps({'lidoAudit': 'begin', **header}))
    for index, chunk in enumerate(chunks):
        emit(json.dumps({'lidoAudit': 'chunk', 'index': index, 'data': chunk}))
    emit(json.dumps({'lidoAudit': 'end', **header}))


def decode_bundle(lines):
    records = []
    total = 0
    for line in lines:
        start = line.find('{"lidoAudit":')
        if start >= 0:
            total += len(line) - start
            if total > 2 * MAX_GZIP:
                raise AuditError('encoded diagnostic log limit exceeded')
            records.append(json.loads(line[start:]))
    if (len(records) < 2 or records[0].get('lidoAudit') != 'begin'
            or records[-1].get('lidoAudit') != 'end'):
        raise AuditError('diagnostic log lacks its complete begin/end records')
    header = {k: v for k, v in records[0].items() if k != 'lidoAudit'}
    if header != {k: v for k, v in records[-1].items() if k != 'lidoAudit'}:
        raise AuditError('diagnostic begin/end digests differ')
    chunks = records[1:-1]
    if (header.get('schema') != 1 or header.get('chunks') != len(chunks)
            or any(row.get('lidoAudit') != 'chunk' or row.get('index') != i for i, row in enumerate(chunks))):
        raise AuditError('diagnostic chunks are missing, duplicated or reordered')
    compressed = base64.b64decode(''.join(row['data'] for row in chunks), validate=True)
    if (len(compressed) > MAX_GZIP or len(compressed) != header['gzipBytes']
            or digest(compressed) != header['gzipSha256']):
        raise AuditError('compressed diagnostic digest or size mismatch')
    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
        archive = stream.read(MAX_RAW + 1)
    if (len(archive) > MAX_RAW or len(archive) != header['rawBytes'] or digest(archive) != header['rawSha256']):
        raise AuditError('raw diagnostic digest or size mismatch')
    value = json.loads(archive)
    if value.get('schema') != 1 or len(value['files']) != header['files']:
        raise AuditError('diagnostic file count or schema mismatch')
    answer = {}
    for name, entry in value['files'].items():
        if not allowed(name):
            raise AuditError('unexpected diagnostic extraction path')
        raw = base64.b64decode(entry['base64'], validate=True)
        if len(raw) != entry['bytes'] or digest(raw) != entry['sha256']:
            raise AuditError('diagnostic file bytes or digest mismatch')
        answer[name] = raw
    return value['metadata'], answer


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--log', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with args.log.open() as stream:
        metadata, files = decode_bundle(stream)
    # A new destination prevents overwriting existing files or following links.
    args.output.mkdir(parents=True, exist_ok=False)
    for name, content in files.items():
        target = args.output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    (args.output / 'controller-audit.json').write_text(json.dumps(metadata, indent=2) + '\n')
    print(json.dumps({'auditVerified': True, 'files': len(files)}))
