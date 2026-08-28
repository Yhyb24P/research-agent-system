"""Trusted Agent Collaboration Plane contracts and registry."""

from researchd.collaboration.registry import AgentRegistryService
from researchd.collaboration.delegation import DelegationService
from researchd.collaboration.invocation import InvocationService
from researchd.collaboration.adapters import CloudLeadAgentAdapter, LocalExecutorAgentAdapter
from researchd.collaboration.gateway import CollaborationGateway

__all__ = ["AgentRegistryService", "DelegationService", "InvocationService", "CloudLeadAgentAdapter", "LocalExecutorAgentAdapter", "CollaborationGateway"]
