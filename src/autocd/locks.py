"""Process locks outlive the parent when inherited by its active commands."""

from contextlib import contextmanager
import fcntl
import os


@contextmanager
def file_lock(path, *, shared=False, blocking=False):
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        if not blocking:
            operation |= fcntl.LOCK_NB
        try:
            fcntl.flock(descriptor, operation)
        except BlockingIOError:
            yield None
        else:
            yield descriptor
    finally:
        # Do not explicitly unlock: children may still hold the same open description.
        os.close(descriptor)
