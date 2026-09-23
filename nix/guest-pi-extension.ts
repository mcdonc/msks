/**
 * msks (#266): discover the daemon's LLM proxy models at pi
 * startup. The workspace seed exports MSKSWS_BASE_URL and
 * MSKSWS_API_KEY in every login shell (/etc/profile.d/msks-llm.sh,
 * from the seed script the daemon writes at first boot); this
 * extension fetches the proxy's /models and registers the catalog
 * under the "msks" provider, so /model always shows the daemon's
 * live model list. A workspace whose daemon serves no model list
 * registers nothing, quietly.
 *
 * The image ships this file in /etc/skel (every seed-provisioned
 * account copies it into ~/.pi/agent/extensions/ — pi discovers
 * extensions from per-user directories only) and in /root's home.
 * A copy a user has edited is theirs; nothing re-overwrites it.
 *
 * A variant of klangk's llm-proxy-models.ts: the credential
 * sources differ (the environment pair above, and the apiKey
 * command below prints the token file the seed plants), everything
 * else — the awaited async factory, three fetch attempts with
 * backoff, the quiet no-op, the embed/rerank filter, the
 * placeholder metadata — carries over verbatim.
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
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const response = await fetch(`${baseUrl}/models`, {
        headers: { Authorization: `Bearer ${apiKey}` },
      });
      if (response.ok) {
        const payload = (await response.json()) as OpenAIModelsResponse;
        return payload.data ?? [];
      }
      if (attempt < 3 && response.status >= 500) {
        await new Promise((r) => setTimeout(r, 2000 * attempt));
        continue;
      }
      console.error(
        "msks llm-models: fetch failed: " +
          `${response.status} ${response.statusText}`,
      );
      return null;
    } catch (err) {
      if (attempt < 3) {
        await new Promise((r) => setTimeout(r, 2000 * attempt));
        continue;
      }
      console.error("msks llm-models: fetch failed:", err);
      return null;
    }
  }
  return null;
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
