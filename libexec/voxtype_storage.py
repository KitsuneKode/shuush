"""Shared transactions for Shuush's history and correction writers."""
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def locked(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # The sibling inode stays stable when a writer replaces the data file.
    with path.with_name(path.name + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def atomic_write(path, text):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def update_history(path, entry, changes):
    with locked(path):
        if not path.exists():
            return False
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for i in reversed(range(len(lines))):
            try:
                row = json.loads(lines[i])
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            if entry.get("take_id"):
                matches = row.get("take_id") == entry["take_id"]
            else:
                matches = all(row.get(k) == entry.get(k) for k in ("ts", "raw"))
            if matches:
                row.update(changes(row))
                lines[i] = json.dumps(row, ensure_ascii=False)
                atomic_write(path, "\n".join(lines) + "\n")
                return True
        return False
