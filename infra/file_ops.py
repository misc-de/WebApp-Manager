from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(target: str | Path, content: str, encoding: str = 'utf-8') -> None:
    target_path = Path(target)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f'.{target_path.name}.', dir=str(target_path.parent))
    try:
        with os.fdopen(fd, 'w', encoding=encoding) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target_path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
