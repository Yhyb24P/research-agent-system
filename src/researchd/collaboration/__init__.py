"""Trusted Agent Collaboration Plane contracts and registry."""

from researchd.collaboration.registry import (
    AgentRegistryService,
    RuntimeLeaseConflict,
    RuntimeLeaseInvalid,
)
from researchd.collaboration.delegation import DelegationService
from researchd.collaboration.invocation import InvocationService, StaleInvocationResult
from researchd.collaboration.adapters import CloudLeadAgentAdapter, LocalExecutorAgentAdapter
from researchd.collaboration.gateway import CollaborationGateway
from researchd.collaboration.selector import AgentSelector, AgentSelection
from researchd.collaboration.messages import CollaborationMessageService
from researchd.collaboration.heterogeneous import A2ARemoteAgentAdapter, HttpAgentAdapter, LocalProcessAgentAdapter
from researchd.collaboration.runtime import AgentAdapterCatalog
from researchd.collaboration.langgraph_runtime import LangGraphAgentAdapter

__all__ = ["AgentRegistryService", "RuntimeLeaseConflict", "RuntimeLeaseInvalid", "DelegationService", "InvocationService", "StaleInvocationResult", "CloudLeadAgentAdapter", "LocalExecutorAgentAdapter", "CollaborationGateway", "AgentSelector", "AgentSelection", "CollaborationMessageService", "A2ARemoteAgentAdapter", "HttpAgentAdapter", "LocalProcessAgentAdapter", "AgentAdapterCatalog", "LangGraphAgentAdapter"]
