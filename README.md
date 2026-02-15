# Medagent

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
