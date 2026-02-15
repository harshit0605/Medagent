# Medagent

Initial monorepo scaffold for a WhatsApp-first support automation platform.

## Architecture

```text
+--------------------+       HTTP        +--------------------+
| whatsapp_gateway   | ----------------> | orchestrator       |
| FastAPI webhook +  |                   | LangGraph runtime  |
| outbound adapter   |                   | + policy logic     |
+--------------------+                   +--------------------+
          |                                         |
          |                                         |
          v                                         v
+--------------------+                   +--------------------+
| scheduler          |                   | postgres + redis   |
| job/timer workers  |                   | persistence/queues |
+--------------------+                   +--------------------+

+--------------------+
| ops_console        |
| thin UI scaffold   |
+--------------------+
```

## Repository layout

- `services/whatsapp_gateway`: FastAPI app for webhook intake and outbound messaging integration.
- `services/orchestrator`: FastAPI host for LangGraph workflow runtime.
- `services/scheduler`: FastAPI health/test host for scheduling worker integration points (APScheduler/Celery).
- `apps/ops_console`: placeholder operations UI scaffold.

## Messaging constraints and policy guardrails

- **24-hour customer service window**: Free-form replies are only allowed within 24 hours of the user's last inbound message.
- **Templates outside window**: When outside the 24-hour window, messages must use approved WhatsApp templates.
- **Escalation requirement**: Safety, legal, financial risk, or unresolved intent loops must trigger escalation to a human operator.

These constraints should be enforced in orchestrator decision logic before dispatching outbound messages.

## Local run

1. Copy environment variables:
   ```bash
   cp .env.example .env
   ```
2. Start local stack:
   ```bash
   docker compose up --build
   ```
3. Health checks:
   ```bash
   curl http://localhost:8000/health
   curl http://localhost:8001/health
   curl http://localhost:8002/health
   ```
4. Simulate webhook forwarding:
   ```bash
   curl -X POST http://localhost:8000/webhook \
     -H "Content-Type: application/json" \
     -d '{"user_id":"u-123","message":"hello"}'
   ```

## Notes

- This is a bootstrap baseline; production readiness still requires auth, observability, retries, DLQs, and policy enforcement in the orchestrator.
- Current Docker services install dependencies at runtime for convenience; replace with dedicated Dockerfiles in later iterations.
WhatsApp-first care concierge scaffold for medication adherence, refills, labs, and human escalation.

## Stack
- **Python services managed with `uv`**
- **Ops console managed with `bun`**

## Services
- `services/whatsapp_gateway`: Cloud API webhook ingress and outbound dispatch policy bridge.
- `services/orchestrator`: Intent routing and policy gate (24h customer service window checks).
- `services/scheduler`: Timer/event stubs for dose and refill events.
- `shared/contracts`: Canonical schemas for inbound/outbound messages and domain events.

## Quick start
```bash
uv sync --dev
uv run uvicorn services.whatsapp_gateway.main:app --reload --port 8001
uv run uvicorn services.orchestrator.main:app --reload --port 8002
uv run uvicorn services.scheduler.main:app --reload --port 8003
```

## Bun app
```bash
cd apps/ops_console
bun install
bun run dev
```

## Compliance notes baked into scaffold
- Freeform replies are allowed only inside the 24-hour customer service window.
- Outside 24 hours, outbound must use approved template payloads.
- Human escalation options are explicitly present in orchestrator responses.
- No in-chat medicine commerce flow is implemented; only partner/deep-link execution should be added.
