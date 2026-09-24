/**
 * msks (#266): discover the daemon's LLM proxy models at pi
 * startup. The workspace seed exports MSKSWS_BASE_URL and
 * MSKSWS_API_KEY in every login shell (/etc/profile.d/msks-llm.sh,
 * from the seed script the daemon writes at first boot) — seeded
 * even when the daemon serves no LLM surface, so the environment
 * alone cannot tell whether a proxy listens. This extension makes
 * one fetch of the proxy's /models, bounded at 1500 ms, and
 * registers the catalog under the "msks" provider, so /model
 * always shows the daemon's live model list. No retry, no sleep:
 * an unanswered proxy is a daemon with no model list (the tap
 * drops, and the daemon's configured-but-failed answer is a
 * stable 503) — retrying would only stall pi's startup — so the
 * fetch is attempted once and any failure registers nothing.
 *
 * The image ships this file in /etc/skel (every seed-provisioned
 * account copies it into ~/.pi/agent/extensions/ — pi discovers
 * extensions from per-user directories only) and in /root's home.
 * A copy a user has edited is theirs; nothing re-overwrites it.
 *
 * A variant of klangk's llm-proxy-models.ts: the credential
 * sources differ (the environment pair above, and the apiKey
 * command below prints the token file the seed plants) — and so
 * does the retry posture, for the reasons above; the awaited
 * async factory, the quiet no-op, the embed/rerank filter, and
 * the placeholder metadata carry over verbatim.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

interface OpenAIModel {
  id: string;
  object?: string;
  created?: number;
  owned_by?: string;
}

interface OpenAIModelsResponse {
  data: OpenAIModel[];
}

async function fetchModels(
  baseUrl: string,
  apiKey: string,
): Promise<OpenAIModel[] | null> {
  try {
    const response = await fetch(`${baseUrl}/models`, {
      headers: { Authorization: `Bearer ${apiKey}` },
      signal: AbortSignal.timeout(1500),
    });
    if (!response.ok) {
      console.error(
        "msks llm-models: fetch failed: " +
          `${response.status} ${response.statusText}`,
      );
      return null;
    }
    const payload = (await response.json()) as OpenAIModelsResponse;
    return payload.data ?? [];
  } catch (err) {
    console.error("msks llm-models: fetch failed:", err);
    return null;
  }
}

export default async function (pi: ExtensionAPI) {
  const baseUrl = process.env.MSKSWS_BASE_URL?.replace(/\/+$/, "");
  const apiKey = process.env.MSKSWS_API_KEY ?? "";
  if (!baseUrl || !apiKey) {
    return; // no proxy named in the environment
  }

  const models = await fetchModels(baseUrl, apiKey);
  if (!models || models.length === 0) {
    return;
  }

  // Embedding and reranker models cannot serve chat completions.
  const chatModels = models.filter((m) => {
    const lower = m.id.toLowerCase();
    return !lower.includes("embed") && !lower.includes("rerank");
  });
  if (chatModels.length === 0) {
    return;
  }

  pi.registerProvider("msks", {
    baseUrl,
    api: "openai-completions",
    apiKey: "!cat /etc/msks/llm.token",
    models: chatModels.map((m) => ({
      id: m.id,
      name: m.id,
      reasoning: false,
      input: ["text"] as ("text" | "image")[],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      contextWindow: 128000,
      maxTokens: 8192,
    })),
  });
  console.error(
    `msks llm-models: registered ${chatModels.length} models ` +
      `from ${baseUrl}`,
  );
}
