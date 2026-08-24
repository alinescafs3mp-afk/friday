"""Optional, default-off advisory model foundation."""

from .contracts import (
    EffectClass,
    ModelModality,
    ModelPriority,
    ModelRequest,
    ModelUsage,
    ModelWorkload,
    SecondaryAttempt,
    SecondaryEndpointConfig,
    SecondaryFailure,
    SecondaryMode,
    SecondaryResult,
    SecondaryState,
    SecondaryStatus,
)
from .gpt_oss import GptOssProtocolAdapter, ProtocolRejection
from .scheduler import SecondaryBrainScheduler, build_secondary_brain

__all__ = [
    "EffectClass",
    "GptOssProtocolAdapter",
    "ModelModality",
    "ModelPriority",
    "ModelRequest",
    "ModelUsage",
    "ModelWorkload",
    "ProtocolRejection",
    "SecondaryAttempt",
    "SecondaryBrainScheduler",
    "SecondaryEndpointConfig",
    "SecondaryFailure",
    "SecondaryMode",
    "SecondaryResult",
    "SecondaryState",
    "SecondaryStatus",
    "build_secondary_brain",
]
