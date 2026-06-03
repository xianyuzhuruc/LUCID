"""Shared triage ordering for dashboard snapshots."""
from __future__ import annotations


TRIAGE_PRIORITY = {
    "waiting": 0,
    "stalled": 1,
    "working": 2,
    "bash": 3,
}
