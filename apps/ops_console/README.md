# Medagent Ops Console

Internal Next.js dashboard on top of the Medagent backends. Server-rendered;
all backend calls go through Next.js (no CORS).

## Pages

- **Dashboard** (`/`) — program metrics + ops queue snapshot from `orchestrator /ops/dashboard`.
- **Tickets** (`/tickets`) — filterable list of `OpsTicket` rows. Ack and resolve via Server Actions; the page revalidates after each mutation.
- **Route tester** (`/route-tester`) — paste an inbound message, submit, see the full agent decision (intent, policy reason codes, risk level, escalation, ticket id, runner, composed reply).

## API routes

- **WhatsApp webhook** (`/api/whatsapp/webhook`) — Meta Cloud API ingress.
  - `GET` handles Meta's verify-token handshake.
  - `POST` verifies `X-Hub-Signature-256`, parses the inbound payload, and forwards each parsed message to the Python orchestrator's `/route`. The agent's reply is then dispatched via the Python gateway's `/send` (which calls Meta).

  Built on the [`chat`](https://chat-sdk.dev) SDK + `@chat-adapter/whatsapp` adapter. We use the adapter for the gnarly bits (signature verification, payload normalization) and intentionally do NOT use `thread.post(...)` — outbound delivery (template + freeform) lives in Python so the WhatsApp 24h customer-service-window policy stays in one place.

  Three things the route handles beyond the basic message path:

  - **Background vs inline processing.** `WHATSAPP_WEBHOOK_ASYNC=1` switches to Next.js `after()` so the 200 ack returns fast and processing keeps running afterwards (recommended on Vercel). Default `0` awaits inline before returning 200 — slower (~1–3s including LLM) but guaranteed completion in any runtime.
  - **Status updates** (sent / delivered / read / failed). chat-sdk doesn't surface these as an event, so the route parses them out of the raw body and POSTs each one to the Python gateway's `POST /internal/whatsapp/status`. The gateway upserts into `whatsapp_message_statuses` keyed by `wamid` with a rank-aware guard (a late `delivered` won't clobber a stored `read`). Read them back via `GET /whatsapp/statuses?recipient_id=…`.
  - **Audio inbound** (voice notes). chat-sdk normalises audio messages to a `"[Audio message]"` text placeholder. We detect `message.raw.message.audio.id`, resolve the Meta media URL via the Graph API, download the audio, transcribe via OpenAI Whisper, and forward the transcript to the orchestrator as the patient's message. On any failure (missing Meta token, missing OpenAI key, transcription empty) we fall back to an empty body so the agent's general path still fires.

## Stack

- Next.js 16 + React 19 (App Router, Server Components, Server Actions)
- Tailwind CSS 4 (no extra UI lib in v0)
- Bun for install + dev server

## Environment

Server-only — never prefix with `NEXT_PUBLIC_`. See `.env.example`.

| Var | Default | Used by |
|-----|---------|---------|
| `ORCHESTRATOR_URL` | `http://localhost:8002` | dashboard, tickets, route tester, WhatsApp webhook forwarding |
| `SCHEDULER_URL`    | `http://localhost:8003` | (reserved for future scheduled-events viewer) |
| `GATEWAY_URL`      | `http://localhost:8001` | WhatsApp webhook reply dispatch |
| `WHATSAPP_ACCESS_TOKEN` | _(empty)_ | chat-sdk WhatsApp adapter init |
| `WHATSAPP_APP_SECRET` | _(empty)_ | webhook `X-Hub-Signature-256` HMAC verification |
| `WHATSAPP_PHONE_NUMBER_ID` | _(empty)_ | adapter init |
| `WHATSAPP_VERIFY_TOKEN` | `change-me` | Meta verify-token handshake (GET) |
| `WHATSAPP_GRAPH_VERSION` | `v22.0` | Meta Graph API version |
| `WHATSAPP_BOT_USERNAME` | `medagent` | display name surfaced to chat-sdk |
| `WHATSAPP_WEBHOOK_ASYNC` | `0` | when `1`, /webhook returns 200 immediately and finishes processing via `next/server` `after()`; default awaits inline |
| `OPENAI_API_KEY` | _(empty)_ | required by the audio transcription path (Whisper) |
| `OPENAI_TRANSCRIPTION_MODEL` | `whisper-1` | model passed to `/v1/audio/transcriptions` |

When any of the four required WhatsApp vars (`ACCESS_TOKEN`, `APP_SECRET`, `PHONE_NUMBER_ID`, `VERIFY_TOKEN`) is empty the webhook returns `503 whatsapp_not_configured` with the missing list in the body.

## Run

Make sure the backends are up first:

```bash
# from repo root, in three terminals
uv run uvicorn services.whatsapp_gateway.main:app --port 8001 --reload
uv run uvicorn services.orchestrator.main:app   --port 8002 --reload
uv run uvicorn services.scheduler.main:app      --port 8003 --reload
```

Then:

```bash
cd apps/ops_console
bun install
bun run dev   # http://localhost:3000
```

## Notes

- Pages set `dynamic = "force-dynamic"`; ops data must be live.
- Server Actions revalidate `/` and `/tickets` after every ticket mutation so the dashboard counters reflect the change immediately.
- `apps/ops_console/AGENTS.md` warns that Next.js 16 has breaking changes vs older guides — when adding new pages or APIs, consult `node_modules/next/dist/docs/` first.
