"""Trusted Agent Collaboration Plane contracts and registry."""

from researchd.collaboration.agent_definitions import AgentDefinition
from researchd.collaboration.aliases import (
    AgentAliasAmbiguous,
    AgentAliasError,
    AgentAliasNotFound,
    AgentAliasService,
    CLI_ALIAS_LABEL,
)
from researchd.collaboration.install import AgentInstallation, AgentInstallService
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
from researchd.collaboration.heterogeneous import A2ARemoteAgentAdapter, HttpAgentAdapter, HttpxAgentClient, LocalProcessAgentAdapter, ManagedProcessAgentAdapter
from researchd.collaboration.runtime import AgentAdapterCatalog
from researchd.collaboration.langgraph_runtime import LangGraphAgentAdapter
from researchd.collaboration.action_broker import AgentActionBroker, AgentMessageAction
from researchd.collaboration.handoff import HandoffProposal, HandoffProposalAction, HandoffProposalService, HandoffResolutionService

__all__ = ["AgentActionBroker", "AgentMessageAction", "HandoffProposal", "HandoffProposalAction", "HandoffProposalService", "HandoffResolutionService", "AgentAliasAmbiguous", "AgentAliasError", "AgentAliasNotFound", "AgentAliasService", "AgentDefinition", "AgentInstallation", "AgentInstallService", "AgentRegistryService", "CLI_ALIAS_LABEL", "RuntimeLeaseConflict", "RuntimeLeaseInvalid", "DelegationService", "InvocationService", "StaleInvocationResult", "CloudLeadAgentAdapter", "LocalExecutorAgentAdapter", "CollaborationGateway", "AgentSelector", "AgentSelection", "CollaborationMessageService", "A2ARemoteAgentAdapter", "HttpAgentAdapter", "HttpxAgentClient", "LocalProcessAgentAdapter", "ManagedProcessAgentAdapter", "AgentAdapterCatalog", "LangGraphAgentAdapter"]
