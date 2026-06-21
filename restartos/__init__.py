"""RestartOS — agentic line-recovery system. Reads OT, writes IT, never actuates."""
__version__ = "2.0.0"


import os as _os

def load_env(path: str = None) -> int:
    """Minimal .env loader (no dependency). Loads KEY=VALUE into os.environ
    without overwriting already-set vars. Returns count loaded."""
    if path is None:
        path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), ".env")
    if not _os.path.exists(path):
        return 0
    n = 0
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in _os.environ:
            _os.environ[k] = v; n += 1
    return n
