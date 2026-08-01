"""Executive: mission planning, DAG-based execution, and task orchestration.

This module coordinates high-level goals ("missions") over the same bounded,
review-gated primitives as the rest of Friday.  A mission is decomposed into an
acyclic task plan, executed step by step by a background runner under the owner's
capability scope, and any durable output is proposed to the Inbox for review.
Controllable autonomy is honored throughout: nothing runs when autonomy is
disabled, and nothing is written to knowledge without the user's confirmation.
"""

from __future__ import annotations

from friday.executive.planner import MissionPlanner, PlannedTask
from friday.executive.service import ExecutiveService

__all__ = ["ExecutiveService", "MissionPlanner", "PlannedTask"]
