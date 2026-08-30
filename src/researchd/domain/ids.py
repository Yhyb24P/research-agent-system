from typing import Any, ClassVar

from pydantic_core import CoreSchema, core_schema


class _EntityId(str):
    """Opaque entity ID with static nominal typing and runtime validation."""

    pattern: ClassVar[str]

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: Any
    ) -> CoreSchema:
        del source_type, handler
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema(pattern=cls.pattern)
        )


class WorkspaceId(_EntityId):
    pattern = r"^ws_[A-Za-z0-9][A-Za-z0-9_-]*$"


class RunId(_EntityId):
    pattern = r"^run_[A-Za-z0-9][A-Za-z0-9_-]*$"


class PlanId(_EntityId):
    pattern = r"^plan_[A-Za-z0-9][A-Za-z0-9_-]*$"


class WorkOrderId(_EntityId):
    pattern = r"^wo_[A-Za-z0-9][A-Za-z0-9_-]*$"


class AttemptId(_EntityId):
    pattern = r"^att_[A-Za-z0-9][A-Za-z0-9_-]*$"


class ArtifactId(_EntityId):
    pattern = r"^artifact://sha256/[A-Fa-f0-9]{64}$"


class ObservationId(_EntityId):
    pattern = r"^obs_[A-Za-z0-9][A-Za-z0-9_-]*$"


class ClaimId(_EntityId):
    pattern = r"^claim_[A-Za-z0-9][A-Za-z0-9_-]*$"


class VerificationId(_EntityId):
    pattern = r"^ver_[A-Za-z0-9][A-Za-z0-9_-]*$"


class AgentId(_EntityId):
    pattern = r"^agent_[A-Za-z0-9][A-Za-z0-9_-]*$"


class AgentRuntimeId(_EntityId):
    pattern = r"^runtime_[A-Za-z0-9][A-Za-z0-9_-]*$"


class RuntimeSessionId(_EntityId):
    pattern = r"^runtime_session_[A-Za-z0-9][A-Za-z0-9_-]*$"


class DelegationId(_EntityId):
    pattern = r"^del_[A-Za-z0-9][A-Za-z0-9_-]*$"


class InvocationId(_EntityId):
    pattern = r"^inv_[A-Za-z0-9][A-Za-z0-9_-]*$"


class MessageId(_EntityId):
    pattern = r"^msg_[A-Za-z0-9][A-Za-z0-9_-]*$"
