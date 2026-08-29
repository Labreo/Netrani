"""
netrani.bob
IBM Bob 2.0 Native Integration and Agentic Execution Harness.
"""

from __future__ import annotations

from netrani.bob.agent import (
    BobAgent,
    BobToolCall,
    BobToolResult,
    CustomModePersona,
    load_custom_modes,
    run_bob_escalation,
)

__all__ = [
    "BobAgent",
    "BobToolCall",
    "BobToolResult",
    "CustomModePersona",
    "load_custom_modes",
    "run_bob_escalation",
]
