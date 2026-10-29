"""Write a file that holds personal data so that only the owner can read it where the platform supports that (POSIX mode 0600).
On Windows the file inherits the folder's permissions (the user's profile folder is already private to that account); no ACL change is made."""
from __future__ import annotations

import os
from pathlib import Path


def write_private_text(path, text: str) -> Path:
    p = Path(path)
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        os.chmod(p, 0o600)            # also tightens a file that already existed with wider permissions
    except OSError:
        pass
    return p
