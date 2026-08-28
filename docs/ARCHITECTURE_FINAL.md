# Final V1 Architecture

```mermaid
flowchart TB
  U[User / local CLI] --> C[Trusted Research Controller]
  C --> O[Bounded Orchestrator]
  O --> P[SQLite WAL + append-only audit]
  O --> E[Egress Context Builder]
  E --> CL[CloudLeadAdapter<br/>outbound HTTPS only]
  O --> PE[Deterministic Policy + Approval]
  O --> V[Independent Verifier]
  O --> LE[Local Executor Worker]
  LE --> B[Capability Broker]
  B --> S[Bubblewrap sandbox / worktree]
  LE --> J[Durable Job Manager]
  V --> A[CAS Artifact Store + evidence]
  CL -. optional boundary .-> A2A[A2A 1.0 adapter]
  B -. optional façade .-> MCP[MCP 2025-11-25 adapter]
```

The controller is the only component allowed to mutate workflow state, grant capabilities, bind approvals, classify/egress data, invoke verification, and declare acceptance. Cloud and local models produce proposals/claims only. A2A and MCP are removable boundary adapters; their IDs and statuses never replace ResearchRun, WorkOrder, Attempt, Job, or Approval records.
