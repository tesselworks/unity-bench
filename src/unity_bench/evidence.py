"""Project-independent evidence identities, immutable receipts and process ownership.

These helpers preserve the existing campaign serialization and cooperative lock
protocol. Locks coordinate participating processes only; they do not establish
that the machine is otherwise idle. Process ancestry is derived from a supplied
census and does not inspect command lines or project data.
"""
import contextlib
import errno
import hashlib
import json
import os


def sha(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    """Campaign identity encoding (sorted keys, ASCII escapes, compact UTF-8).

    This preserves historic receipt hashes; it is not an RFC 8785 encoder.
    Evidence writers separately refuse nonfinite JSON numbers.
    """
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write('\n')


@contextlib.contextmanager
def machine_lock(path):
    """Cooperative OS lock shared by runner services; nonparticipants remain possible."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if not path.stat().st_size:
            stream.write(b'1'); stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) or getattr(error, 'winerror', None) == 33:
                    raise BlockingIOError('Cooperative lock unavailable or held') from error
                raise
            try:
                yield
            finally:
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def descendants(rows, roots):
    owned = set(roots)
    while True:
        next_set = owned | {row['pid'] for row in rows if row['parentPid'] in owned}
        if next_set == owned:
            return owned
        owned = next_set


def ancestry(rows, pid):
    table = {row['pid']: row for row in rows}
    result = set()
    while pid in table and pid not in result:
        result.add(pid); pid = table[pid]['parentPid']
    return result
