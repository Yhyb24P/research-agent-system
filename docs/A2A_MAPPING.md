# A2A Mapping

The A2A adapter is optional boundary code. It is pinned to A2A `1.0.0` and exposes an example Agent Card using `supportedInterfaces`; no A2A listener is started and no public endpoint is opened.

| Internal record | A2A mapping |
|---|---|
| ResearchRun | never an A2A Task; used only to derive a stable context grouping |
| WorkOrder + Attempt | one outbound A2A interaction/task |
| `AgentInteraction.interaction_id` | durable local mapping primary key |
| `AgentInteraction.a2a_context_id` | remote context ID, opaque and non-authoritative |
| `AgentInteraction.a2a_task_id` | remote Task ID, opaque and non-authoritative |
| internal WorkOrder/Attempt IDs | retained in payload metadata and local foreign keys |

Dispatch writes the local interaction reservation before the outbound call and sends a deterministic idempotency key. Repeating a dispatch reuses the same mapping and lets the remote endpoint reconcile the same task. A terminal task cannot be restarted: `refine_terminal_task` creates a new A2A task and interaction, preserves `contextId`, and records `refinesTaskId`.

A2A status never grants local capability or approval. Policy, approvals, sandbox, artifacts, verification, and controller transitions remain authoritative.
