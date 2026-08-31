"""Fail-closed codecs between authoritative researchd DTOs and A2A artifacts."""

from uuid import uuid4

from pydantic import Field, ValidationError

from researchd.adapters.a2a.schemas import A2AArtifact, A2AMessage, A2APart, A2ATask
from researchd.context.agent_context import AgentContextBundle
from researchd.domain.base import DomainModel
from researchd.executor.contracts import ExecutorResult, GrantedWorkOrder


GRANTED_WORK_ORDER_MEDIA_TYPE = "application/vnd.researchd.granted-work-order+json"
EXECUTOR_RESULT_MEDIA_TYPE = "application/vnd.researchd.executor-result+json"
REMOTE_EXECUTION_REQUEST_MEDIA_TYPE = "application/vnd.researchd.remote-execution-request+json"


class A2ACodecError(ValueError):
    pass


class RemoteExecutionRequest(DomainModel):
    """The only execution input permitted to cross the governed A2A boundary.

    It deliberately omits controller capabilities, local sandbox configuration,
    and workspace grants.  The context bundle is assembled by the controller
    for the target trust zone before this model is constructed.
    """

    invocation_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    work_order_id: str = Field(min_length=1, max_length=128)
    attempt_id: str = Field(min_length=1, max_length=128)
    objective: str = Field(min_length=1, max_length=16_384)
    context: AgentContextBundle


def encode_remote_execution_request(
    request: RemoteExecutionRequest,
    *,
    message_id: str | None = None,
    context_id: str | None = None,
    reference_task_ids: tuple[str, ...] = (),
) -> A2AMessage:
    """Encode a scope-bound, redacted A2A execution candidate request."""
    return A2AMessage(
        messageId=message_id or f"msg_{uuid4().hex}",
        contextId=context_id,
        role="ROLE_USER",
        parts=(A2APart(
            data=request.model_dump(mode="json"),
            mediaType=REMOTE_EXECUTION_REQUEST_MEDIA_TYPE,
        ),),
        referenceTaskIds=reference_task_ids,
    )


def encode_granted_work_order(
    work_order: GrantedWorkOrder,
    *,
    message_id: str | None = None,
    context_id: str | None = None,
    reference_task_ids: tuple[str, ...] = (),
) -> A2AMessage:
    return A2AMessage(
        messageId=message_id or f"msg_{uuid4().hex}",
        contextId=context_id,
        role="ROLE_USER",
        parts=(A2APart(data=work_order.model_dump(mode="json"), mediaType=GRANTED_WORK_ORDER_MEDIA_TYPE),),
        referenceTaskIds=reference_task_ids,
    )


def encode_executor_result(result: ExecutorResult, *, artifact_id: str | None = None) -> A2AArtifact:
    return A2AArtifact(
        artifactId=artifact_id or f"artifact_{uuid4().hex}",
        name="researchd ExecutorResult",
        parts=(A2APart(data=result.model_dump(mode="json"), mediaType=EXECUTOR_RESULT_MEDIA_TYPE),),
    )


def decode_executor_result(
    task: A2ATask,
    *,
    expected_attempt_id: str,
    forbid_capability_results: bool = False,
) -> ExecutorResult:
    if task.status.state != "TASK_STATE_COMPLETED":
        raise A2ACodecError("only a completed A2A Task can contain an authoritative result candidate")
    candidates = [
        part.data
        for artifact in task.artifacts
        for part in artifact.parts
        if part.mediaType == EXECUTOR_RESULT_MEDIA_TYPE and part.data is not None
    ]
    if len(candidates) != 1:
        raise A2ACodecError("A2A Task must contain exactly one typed ExecutorResult artifact")
    try:
        result = ExecutorResult.model_validate(candidates[0])
    except ValidationError as exc:
        raise A2ACodecError("A2A ExecutorResult artifact is malformed") from exc
    if result.attempt_id != expected_attempt_id:
        raise A2ACodecError("A2A ExecutorResult attempt scope does not match the invocation")
    if forbid_capability_results and result.capability_results:
        raise A2ACodecError("remote A2A ExecutorResult cannot claim controller capabilities")
    return result


def decode_typed_artifact(task: A2ATask, *, media_type: str, output_type: type[DomainModel]) -> DomainModel:
    if task.status.state != "TASK_STATE_COMPLETED":
        raise A2ACodecError("only a completed A2A Task can contain an output candidate")
    candidates = [
        part.data
        for artifact in task.artifacts
        for part in artifact.parts
        if part.mediaType == media_type and part.data is not None
    ]
    if len(candidates) != 1:
        raise A2ACodecError(f"A2A Task must contain exactly one {media_type} artifact")
    try:
        return output_type.model_validate(candidates[0])
    except ValidationError as exc:
        raise A2ACodecError(f"A2A {media_type} artifact is malformed") from exc
