"""Fail-closed codecs between authoritative researchd DTOs and A2A artifacts."""

from uuid import uuid4

from researchd.adapters.a2a.schemas import A2AArtifact, A2AMessage, A2APart, A2ATask
from researchd.domain.base import DomainModel
from researchd.executor.contracts import ExecutorResult, GrantedWorkOrder


GRANTED_WORK_ORDER_MEDIA_TYPE = "application/vnd.researchd.granted-work-order+json"
EXECUTOR_RESULT_MEDIA_TYPE = "application/vnd.researchd.executor-result+json"


class A2ACodecError(ValueError):
    pass


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


def decode_executor_result(task: A2ATask, *, expected_attempt_id: str) -> ExecutorResult:
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
    result = ExecutorResult.model_validate(candidates[0])
    if result.attempt_id != expected_attempt_id:
        raise A2ACodecError("A2A ExecutorResult attempt scope does not match the invocation")
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
    return output_type.model_validate(candidates[0])
