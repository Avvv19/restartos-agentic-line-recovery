"""
restartos.security
=================
The OT/IT boundary, enforced by the CAPABILITY SET — not by a prompt.

This is the single most important safety rule in the architecture: the agent
READS OT (L0-L3) and WRITES only IT/business systems (L4). There is no tool,
token, or code path by which it can actuate the line. Restart and LOTO are
performed by humans.

Any attempt to obtain a WRITE capability against an OT plane raises
OTWriteForbidden — a hard, un-promptable failure.
"""
from __future__ import annotations

from .domain import Access, Plane


class OTWriteForbidden(PermissionError):
    pass


# The capability matrix. Note: every OT plane is READ-only; only IT is writable.
_CAPS = {
    (Plane.OT_CONTROL, Access.READ): True,
    (Plane.OT_CONTROL, Access.WRITE): False,   # NEVER
    (Plane.OT_OPS, Access.READ): True,
    (Plane.OT_OPS, Access.WRITE): False,       # NEVER
    (Plane.IT_BUSINESS, Access.READ): True,
    (Plane.IT_BUSINESS, Access.WRITE): True,   # gated + idempotent
}


def assert_capability(plane: Plane, access: Access) -> None:
    if not _CAPS.get((plane, access), False):
        if access == Access.WRITE and plane in (Plane.OT_CONTROL, Plane.OT_OPS):
            raise OTWriteForbidden(
                f"BLOCKED: write to {plane.value} is forbidden by construction. "
                "The agent cannot actuate the line — humans perform restart/LOTO.")
        raise PermissionError(f"capability denied: {access.value} on {plane.value}")


def writable_planes() -> list[str]:
    return [p.value for (p, a), ok in _CAPS.items() if a == Access.WRITE and ok]
