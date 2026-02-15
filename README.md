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
