# Medagent

WhatsApp-first care concierge scaffold for medication adherence, refills, lab follow-ups, and human escalation.

## Architecture

```text
+--------------------+       HTTP        +--------------------+
| whatsapp_gateway   | ----------------> | orchestrator       |
| FastAPI webhook +  |                   | policy + routing   |
| outbound adapters  |                   | (LangGraph-ready)  |
+--------------------+                   +--------------------+
          |                                         |
          v                                         v
+--------------------+                   +--------------------+
| scheduler          |                   | postgres + redis   |
| timer/event stubs  |                   | persistence/queues |
+--------------------+                   +--------------------+

+--------------------+
| ops_console        |
| thin UI scaffold   |
+--------------------+
```

## Repository layout

- `services/whatsapp_gateway`: Cloud API webhook ingress and outbound dispatch bridge.
- `services/orchestrator`: intent routing and policy gate (24-hour window checks).
- `services/orchestrator/agent_workflow.py`: typed agent workflow with LangGraph-compatible graph builder and deterministic fallback runner.
- `services/scheduler`: timer/event stubs for dose and refill events.
- `shared/contracts`: canonical inbound/outbound/event schemas.
- `docs/template_pack.md`: WhatsApp template pack.

## Compliance guardrails baked into scaffold

- Free-form replies are allowed only inside the **24-hour customer service window**.
- Outside 24 hours, outbound must use approved **template** payloads.
- Human escalation options are explicitly present in orchestrator responses.
- No WhatsApp-native medicine commerce flow is implemented (partner/deep-link only).

## Quick start

```bash
uv sync --dev
uv run uvicorn services.whatsapp_gateway.main:app --reload --port 8001
uv run uvicorn services.orchestrator.main:app --reload --port 8002
uv run uvicorn services.scheduler.main:app --reload --port 8003
```

## Ops console

```bash
cd apps/ops_console
bun install
bun run dev
```


## Ops API (Sprint 5)

Orchestrator now exposes basic operations endpoints for console wiring:

- `POST /ops/tickets`
- `GET /ops/tickets`
- `POST /ops/tickets/{ticket_id}/ack`
- `POST /ops/tickets/{ticket_id}/resolve`
- `GET /ops/dashboard`

