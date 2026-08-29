"""Deterministic research-critic specialist implemented as a LangGraph Agent."""

from importlib import import_module
from typing import Any, TypedDict, cast

from researchd.collaboration.contracts import (
    ResearchCriticResult,
    SpecialistFinding,
    SpecialistInvocationInput,
)
from researchd.collaboration.langgraph_runtime import LangGraphExecutable


class _ResearchCriticState(TypedDict, total=False):
    request: dict[str, Any]
    result: dict[str, Any]


def _critique(state: _ResearchCriticState) -> _ResearchCriticState:
    request = SpecialistInvocationInput.model_validate(state["request"])
    findings: list[SpecialistFinding] = []
    evidence_refs: set[str] = set()
    if not request.claims:
        findings.append(SpecialistFinding(
            code="NO_REVIEWABLE_CLAIMS",
            severity="WARNING",
            detail="The specialist received no explicit claims to review.",
        ))
    for claim in request.claims:
        evidence_refs.update(claim.evidence_refs)
        if not claim.evidence_refs:
            findings.append(SpecialistFinding(
                code="CLAIM_WITHOUT_EVIDENCE",
                severity="ERROR",
                claim_id=claim.claim_id,
                detail=f"Claim {claim.claim_id} has no evidence reference.",
            ))
    missing_focus = tuple(focus for focus in request.review_focus if focus.strip() == "")
    if missing_focus:
        findings.append(SpecialistFinding(
            code="EMPTY_REVIEW_FOCUS",
            severity="WARNING",
            detail="One or more review-focus entries are empty.",
        ))
    requires_revision = any(item.severity == "ERROR" for item in findings)
    result = ResearchCriticResult(
        summary=(
            f"Reviewed {len(request.claims)} claims; {len(findings)} issue(s) require attention."
            if findings
            else f"Reviewed {len(request.claims)} claims with explicit evidence references."
        ),
        findings=tuple(findings),
        recommendation="REVISE" if requires_revision else "ACCEPT",
        cited_evidence_refs=tuple(sorted(evidence_refs)),
    )
    return {"result": result.model_dump(mode="json")}


def build_research_critic_graph() -> LangGraphExecutable:
    """Build the optional graph without importing LangGraph in the core path."""

    try:
        graph_module = import_module("langgraph.graph")
    except ModuleNotFoundError as error:
        raise RuntimeError("install the 'langgraph-agent' extra to build this Agent") from error
    builder = graph_module.StateGraph(_ResearchCriticState)
    builder.add_node("critique", _critique)
    builder.add_edge(graph_module.START, "critique")
    builder.add_edge("critique", graph_module.END)
    return cast(LangGraphExecutable, builder.compile())


__all__ = ["build_research_critic_graph"]
