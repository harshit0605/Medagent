/**
 * Parse Meta webhook status updates and forward each to the Python gateway.
 *
 * Status updates (sent / delivered / read / failed) are NOT exposed as a
 * chat-sdk event, so we extract them from the raw webhook body ourselves and
 * POST each one to the gateway's `/internal/whatsapp/status` endpoint.
 *
 * The Meta payload shape we care about:
 *
 *   {
 *     "entry": [{
 *       "changes": [{
 *         "value": {
 *           "statuses": [
 *             {
 *               "id": "wamid.XXX",                  // outbound message id
 *               "status": "delivered",              // sent | delivered | read | failed
 *               "timestamp": "1614000000",          // unix seconds, string
 *               "recipient_id": "16315551234",
 *               "errors": [{ "code": 131000, "title": "Generic error" }]   // failed only
 *             }
 *           ]
 *         }
 *       }]
 *     }]
 *   }
 */

const GATEWAY_URL = process.env.GATEWAY_URL ?? "http://localhost:8001";

type MetaStatus = {
  id: string;
  status: string;
  timestamp: string;
  recipient_id?: string;
  errors?: { code: number; title: string }[];
};

type MetaWebhookBody = {
  entry?: {
    changes?: {
      value?: {
        statuses?: MetaStatus[];
      };
    }[];
  }[];
};

export function extractStatuses(rawBody: string): MetaStatus[] {
  let payload: MetaWebhookBody;
  try {
    payload = JSON.parse(rawBody) as MetaWebhookBody;
  } catch {
    return [];
  }
  const out: MetaStatus[] = [];
  for (const entry of payload.entry ?? []) {
    for (const change of entry.changes ?? []) {
      for (const status of change.value?.statuses ?? []) {
        if (status?.id && status?.status && status?.timestamp) {
          out.push(status);
        }
      }
    }
  }
  return out;
}

function isoFromUnix(seconds: string): string {
  const epoch = Number(seconds);
  if (!Number.isFinite(epoch)) return new Date().toISOString();
  return new Date(epoch * 1000).toISOString();
}

async function postOne(status: MetaStatus): Promise<void> {
  const error = status.errors?.[0];
  const body = {
    wamid: status.id,
    status: status.status,
    recipient_id: status.recipient_id ?? null,
    timestamp: isoFromUnix(status.timestamp),
    error_code: error?.code ?? null,
    error_title: error?.title ?? null,
    raw: status,
  };
  const res = await fetch(`${GATEWAY_URL}/internal/whatsapp/status`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`gateway status upsert ${res.status}: ${text}`);
  }
}

/**
 * Parse and forward all statuses from the raw webhook body. Errors per-status
 * are caught and logged so a single bad row doesn't abort the whole batch.
 */
export async function forwardStatuses(rawBody: string): Promise<void> {
  const statuses = extractStatuses(rawBody);
  if (statuses.length === 0) return;
  await Promise.all(
    statuses.map(async (status) => {
      try {
        await postOne(status);
      } catch (err) {
        console.error(
          "[whatsapp] status forward failed for wamid %s: %s",
          status.id,
          err,
        );
      }
    }),
  );
}
