"""Version-locked byte patches; no third-party dependencies or text-offset guessing."""
import base64
import bisect
import hashlib
import json
import os
import pathlib
import tempfile


class PatchError(ValueError):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def _prefix(a, ai, b, bi):
    limit = min(len(a) - ai, len(b) - bi)
    n = 0
    while n + 4096 <= limit and a[ai+n:ai+n+4096] == b[bi+n:bi+n+4096]:
        n += 4096
    while n < limit and a[ai+n] == b[bi+n]:
        n += 1
    return n


def create_delta(before, after):
    """Build a sparse forward-only patch. Refuse huge unmatched application tails."""
    result = {'before_sha256': sha256(before), 'after_sha256': sha256(after),
              'before_size': len(before), 'after_size': len(after), 'edits': []}
    if before == after:
        return result
    width, stride = 16, 32
    anchors = {}
    for i in range(0, max(0, len(before)-width+1), stride):
        anchors.setdefault(before[i:i+width], []).append(i)
    ai = bi = 0
    while ai < len(before) or bi < len(after):
        common = _prefix(before, ai, after, bi)
        ai += common
        bi += common
        if ai == len(before) and bi == len(after):
            break
        match = None
        best_cost = 1 << 60
        upper = min(len(after)-width+1, bi + 65536)
        for nb in range(bi, max(bi, upper)):
            if nb - bi > best_cost:
                break
            positions = anchors.get(after[nb:nb+width])
            if not positions:
                continue
            k = bisect.bisect_left(positions, ai)
            if k < len(positions) and positions[k] <= ai + 262144:
                cost = positions[k] - ai + nb - bi
                if cost < best_cost:
                    match = positions[k], nb
                    best_cost = cost
        if match is None:
            if len(before)-ai > 2*1024*1024:
                raise PatchError('No nearby anchor at bytes %d/%d; refusing a whole program tail' % (ai, bi))
            end_a, end_b = len(before), len(after)
        else:
            end_a, end_b = match
            while end_a > ai and end_b > bi and before[end_a-1] == after[end_b-1]:
                end_a -= 1
                end_b -= 1
        if end_a == ai and end_b == bi:
            raise PatchError('Delta builder made no progress')
        result['edits'].append({'start': ai, 'end': end_a,
                               'data': base64.b64encode(after[bi:end_b]).decode('ascii')})
        ai, bi = end_a, end_b
    if apply_bytes(before, result) != after:
        raise PatchError('Builder round-trip failed')
    return result


def apply_bytes(source, patch):
    digest = sha256(source)
    if digest == patch['after_sha256']:
        return source
    if digest != patch['before_sha256'] or len(source) != patch['before_size']:
        raise PatchError('File does not match the supported original; no patch applied')
    cursor = 0
    pieces = []
    for edit in patch['edits']:
        start, end = edit['start'], edit['end']
        if not isinstance(start, int) or not isinstance(end, int) or not cursor <= start <= end <= len(source):
            raise PatchError('Invalid or overlapping byte ranges')
        try:
            data = base64.b64decode(edit['data'], validate=True)
        except Exception as exc:
            raise PatchError('Invalid patch payload') from exc
        pieces.extend((source[cursor:start], data))
        cursor = end
    pieces.append(source[cursor:])
    target = b''.join(pieces)
    if len(target) != patch['after_size'] or sha256(target) != patch['after_sha256']:
        raise PatchError('Reconstructed file failed SHA-256 verification')
    return target


def safe_path(root, relative):
    rel = pathlib.PurePosixPath(relative)
    if rel.is_absolute() or '..' in rel.parts or '\\' in relative or not rel.parts:
        raise PatchError('Unsafe patch path')
    root = pathlib.Path(root).resolve()
    target = root
    for part in rel.parts:
        target = target / part
        if target.is_symlink():
            raise PatchError('Symlinks are not accepted as patch targets')
    return target


def atomic_write(path, data, mode=None):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise PatchError('Refusing to replace a symlink')
    if mode is None:
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, temp = tempfile.mkstemp(prefix='.'+path.name+'.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def apply_tree(root, files):
    """Preflight every file before writing. Call only on an isolated staging tree."""
    prepared = []
    for relative, patch in files.items():
        target = safe_path(root, relative)
        source = target.read_bytes() if target.exists() else b''
        reconstructed = apply_bytes(source, patch)
        if reconstructed != source:
            prepared.append((target, reconstructed))
    for target, data in prepared:
        atomic_write(target, data)
    return len(prepared)
