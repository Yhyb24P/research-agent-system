"""Durable-command authority for attached remote A2A runtimes."""

from researchd.collaboration.registry import AgentRegistryService
from researchd.domain.enums import AgentAdapterKind
from researchd.domain.ids import AgentRuntimeId
from researchd.storage.models import AgentRuntimeRecord


class RemoteAttachmentService:
    """Own remote runtime leases without creating a local RuntimeSession."""

    owner_id = "researchd-remote-attachment"

    def __init__(self, registry: AgentRegistryService) -> None:
        self.registry = registry

    def attach(self, runtime_id: str) -> dict[str, object]:
        runtime = self.registry.require_enabled_runtime(runtime_id)
        if runtime.adapter_kind is not AgentAdapterKind.A2A:
            raise ValueError("remote attachment requires an A2A AgentRuntime")
        lease = self.registry.acquire_runtime(runtime_id, owner_id=self.owner_id)
        return {"runtime_id": str(lease.runtime_id), "lease_id": lease.lease_id, "expires_at": lease.expires_at.isoformat()}

    def detach(self, runtime_id: str) -> dict[str, object]:
        with self.registry.sessions() as session:
            row = session.get(AgentRuntimeRecord, runtime_id)
            if row is None or row.runtime_lease_id is None or row.lease_owner_id != self.owner_id:
                raise ValueError("remote runtime has no daemon-owned attachment")
            lease_id = row.runtime_lease_id
            acquired_at = row.lease_acquired_at
            expires_at = row.lease_expires_at
        if acquired_at is None or expires_at is None:
            raise ValueError("remote runtime attachment is malformed")
        from researchd.collaboration.contracts import AgentRuntimeLease
        self.registry.release_runtime(AgentRuntimeLease(
            lease_id=lease_id, runtime_id=AgentRuntimeId(runtime_id), owner_id=self.owner_id,
            acquired_at=acquired_at, expires_at=expires_at,
        ))
        return {"runtime_id": runtime_id, "detached": True}

    def renew(self, runtime_id: str) -> dict[str, object]:
        return self.attach(runtime_id)
