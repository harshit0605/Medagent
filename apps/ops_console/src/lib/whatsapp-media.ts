/**
 * Download WhatsApp inbound media + transcribe audio via OpenAI Whisper.
 *
 * Flow for an audio message:
 *   1. Resolve the media URL via `GET https://graph.facebook.com/{ver}/{id}`
 *      (returns `{ url, mime_type, file_size, sha256, ... }`).
 *   2. Download the bytes from `url` with the same Bearer token.
 *   3. POST to `https://api.openai.com/v1/audio/transcriptions` with the
 *      blob attached as `file=@audio.ogg`, `model=whisper-1`.
 *   4. Return the plain transcript string (or null on any failure).
 *
 * Failures are logged and return `null` — the caller falls back to whatever
 * text was already on the chat-sdk Message (usually empty for audio).
 *
 * Required env: WHATSAPP_ACCESS_TOKEN, WHATSAPP_GRAPH_VERSION, OPENAI_API_KEY.
 * Optional:     OPENAI_TRANSCRIPTION_MODEL (default "whisper-1").
 */

const GRAPH_BASE = "https://graph.facebook.com";
const OPENAI_TRANSCRIPTIONS_URL =
  "https://api.openai.com/v1/audio/transcriptions";

function token(): string | null {
  return process.env.WHATSAPP_ACCESS_TOKEN || null;
}

function graphVersion(): string {
  return process.env.WHATSAPP_GRAPH_VERSION ?? "v22.0";
}

function openaiKey(): string | null {
  return process.env.OPENAI_API_KEY || null;
}

function transcriptionModel(): string {
  return process.env.OPENAI_TRANSCRIPTION_MODEL ?? "whisper-1";
}

type MetaMediaResolution = {
  url: string;
  mimeType: string;
};

async function resolveMediaUrl(mediaId: string): Promise<MetaMediaResolution | null> {
  const accessToken = token();
  if (!accessToken) return null;
  const res = await fetch(`${GRAPH_BASE}/${graphVersion()}/${mediaId}`, {
    headers: { Authorization: `Bearer ${accessToken}` },
    cache: "no-store",
  });
  if (!res.ok) {
    console.error("[whatsapp-media] resolve failed", mediaId, res.status);
    return null;
  }
  const body = (await res.json()) as { url?: string; mime_type?: string };
  if (!body.url) return null;
  return { url: body.url, mimeType: body.mime_type ?? "audio/ogg" };
}

async function downloadMedia(url: string): Promise<Blob | null> {
  const accessToken = token();
  if (!accessToken) return null;
  const res = await fetch(url, {
    headers: { Authorization: `Bearer ${accessToken}` },
    cache: "no-store",
  });
  if (!res.ok) {
    console.error("[whatsapp-media] download failed", res.status);
    return null;
  }
  return await res.blob();
}

async function transcribe(blob: Blob, mimeType: string): Promise<string | null> {
  const apiKey = openaiKey();
  if (!apiKey) {
    console.warn("[whatsapp-media] OPENAI_API_KEY unset; skipping transcription");
    return null;
  }
  const ext = mimeType.includes("ogg")
    ? "ogg"
    : mimeType.includes("mpeg") || mimeType.includes("mp3")
      ? "mp3"
      : mimeType.includes("wav")
        ? "wav"
        : "audio";
  const file = new File([blob], `voice.${ext}`, { type: mimeType });

  const form = new FormData();
  form.append("file", file);
  form.append("model", transcriptionModel());
  form.append("response_format", "text");

  const res = await fetch(OPENAI_TRANSCRIPTIONS_URL, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiKey}` },
    body: form,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    console.error("[whatsapp-media] whisper failed", res.status, text);
    return null;
  }
  const transcript = (await res.text()).trim();
  return transcript || null;
}

/**
 * End-to-end: media id → transcript text. Returns `null` on any failure
 * (missing env, network, transcription empty); caller falls back to the
 * original empty text and the agent will respond in its general path.
 */
export async function transcribeAudio(mediaId: string): Promise<string | null> {
  const resolved = await resolveMediaUrl(mediaId);
  if (!resolved) return null;
  const blob = await downloadMedia(resolved.url);
  if (!blob) return null;
  return await transcribe(blob, resolved.mimeType);
}

import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";

const PUBLIC_UPLOADS_DIR = path.resolve(
  process.cwd(),
  "public",
  "uploads",
  "prescriptions",
);

function extFromMime(mime: string): string {
  if (mime.includes("png")) return "png";
  if (mime.includes("webp")) return "webp";
  if (mime.includes("gif")) return "gif";
  if (mime.includes("heic")) return "heic";
  return "jpg";
}

/**
 * Download a WhatsApp inbound image and persist it under
 * ``public/uploads/prescriptions/<uuid>.<ext>``. Returns the public-relative
 * path (``/uploads/prescriptions/<uuid>.<ext>``) the orchestrator can pair
 * with the cloudflared host for an OpenAI vision call. Returns null on any
 * failure (missing env, Meta resolve/download error, FS write error).
 */
export async function downloadImageToPublic(
  mediaId: string,
): Promise<{ publicPath: string; mimeType: string } | null> {
  const resolved = await resolveMediaUrl(mediaId);
  if (!resolved) return null;
  const blob = await downloadMedia(resolved.url);
  if (!blob) return null;
  try {
    await mkdir(PUBLIC_UPLOADS_DIR, { recursive: true });
    const ext = extFromMime(resolved.mimeType);
    const filename = `${crypto.randomUUID()}.${ext}`;
    const filePath = path.join(PUBLIC_UPLOADS_DIR, filename);
    const arrayBuffer = await blob.arrayBuffer();
    await writeFile(filePath, Buffer.from(arrayBuffer));
    return {
      publicPath: `/uploads/prescriptions/${filename}`,
      mimeType: resolved.mimeType,
    };
  } catch (err) {
    console.error("[whatsapp-media] image save failed:", err);
    return null;
  }
}
