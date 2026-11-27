/**
 * msks (#1197): the `screenshot` tool and the `/screenshot`
 * command over the screenshot pipe (#1194), for the agent that
 * runs inside a workspace.
 *
 * The pipe's guest side is one loopback HTTP endpoint. capture.ts
 * beside this file is that side with no dependency on pi (plain
 * node runs it — the smoke test's probe — and this file imports
 * it). This file adds what only pi can do: a tool that returns the
 * capture as image content in one call, a command that puts the
 * capture in front of the agent, and a picker for the vision
 * model.
 *
 * Who sees the capture. The vision model the user picked with
 * `/screenshot model`, stored under one custom key in pi's
 * settings.json — else the active model, directly. The proxy's
 * model list carries no modality data, so llm-models.ts declares
 * every proxy model image-capable; on the direct path a text-only
 * upstream's error is the signal to pick a vision model, which
 * then describes the capture through one nested call and answers
 * in text.
 *
 * The image ships this directory in /etc/skel and in /root's home,
 * beside llm-models.ts, under the same copy-once rules; pi
 * discovers it as extensions/msks-screenshot/index.ts and leaves
 * capture.ts alone (discovery reads index files only; the
 * package.json marks the directory an ES module and declares no
 * manifest, so the index rule applies). A copy a user has edited
 * is theirs; nothing re-overwrites it.
 */
import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import type {
  ExtensionAPI,
  ExtensionContext,
} from "@earendil-works/pi-coding-agent";
import type {
  AssistantMessage,
  ImageContent,
  TextContent,
  Usage,
} from "@earendil-works/pi-ai";
import type { AutocompleteItem } from "@earendil-works/pi-tui";
import { Type } from "typebox";
import { capture, type Capture } from "./capture.ts";

/** The settings.json key the picker writes: a `provider/model-id`
 * from the model list. pi keeps unknown keys on its own writes, so
 * the choice survives a restart. pi re-reads the file only on
 * /reload or a restart, so the tool takes the pick from
 * :data:`picked` in memory: seeded from pi.getSettings() on the
 * first use (the settings are not readable while the extension
 * loads), updated by the picker the moment the file is written. */
export const SETTINGS_KEY = "msksScreenshotVisionModel";

/** The extension API, for the one lazy read of the settings. */
let api: ExtensionAPI | undefined;

let seeded = false;

/** The pick this process works with (see SETTINGS_KEY). */
let picked: string | undefined = undefined;

/** Read the pick from pi's settings once: the key is this
 * extension's own, outside pi's Settings schema. */
function seedVisionModel(): void {
  seeded = true;
  if (api === undefined) {
    return;
  }
  const value = (api.getSettings() as Settings)[SETTINGS_KEY];
  picked = typeof value === "string" && value.trim() ? value.trim() : undefined;
}

/** The picker's clear entry. */
const NONE = "(none): the active model sees captures";

const PICK_HINT = "run /screenshot model to pick a vision model";

const DESCRIBE_PROMPT =
  "Describe this screenshot of the user's screen in detail: the " +
  "windows, the text, and anything that looks like an error.";

type Settings = Record<string, unknown>;
type Content = TextContent | ImageContent;
type CatalogModel = NonNullable<
  ReturnType<ExtensionContext["modelRegistry"]["find"]>
>;

/** The tool's details, one shape across every result. */
interface ShotDetails {
  monitor: number;
  mode: "direct" | "describe" | "error";
  bytes?: number;
  path?: string;
  mime?: string;
  /** The vision model that described the capture. */
  viewer?: string;
  /** The failure's status: refused when nothing listened. */
  status?: number | "refused";
}

interface ShotResult {
  content: Content[];
  details: ShotDetails;
  isError?: boolean;
  usage?: Usage;
}

/** pi's settings file: under the agent directory the environment
 * names, else the default. */
function settingsPath(): string {
  const agentDir =
    process.env.PI_CODING_AGENT_DIR || join(homedir(), ".pi", "agent");
  return join(agentDir, "settings.json");
}

/** The picked vision model, or undefined when none is picked: the
 * first call reads it from pi's settings, the picker's writes
 * update it from then on. */
function visionModel(): string | undefined {
  if (!seeded) {
    seedVisionModel();
  }
  return picked;
}

/** Write (or clear) the vision model in settings.json: a
 * read-modify-write that keeps every other key, landed by rename.
 * A file that is not a JSON object is refused by name — the
 * operator fixes it by hand; the picker never replaces it. */
export function writeVisionModel(
  value: string | undefined,
  path: string = settingsPath(),
): void {
  let doc: Settings = {};
  let existing: string | undefined;
  try {
    existing = readFileSync(path, "utf8");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") {
      throw error;
    }
  }
  if (existing !== undefined) {
    const parsed: unknown = JSON.parse(existing);
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      Array.isArray(parsed)
    ) {
      throw new Error(`${path} does not hold a JSON object; fix it by hand`);
    }
    doc = parsed as Settings;
  }
  if (value === undefined) {
    delete doc[SETTINGS_KEY];
  } else {
    doc[SETTINGS_KEY] = value;
  }
  mkdirSync(dirname(path), { recursive: true });
  const staging = `${path}.msks-${process.pid}.tmp`;
  writeFileSync(staging, `${JSON.stringify(doc, null, 2)}\n`, "utf8");
  renameSync(staging, path);
}

function modelRef(model: { provider: string; id: string }): string {
  return `${model.provider}/${model.id}`;
}

/** The model list the picker shows: the session's scoped models
 * when scoping is configured, else the whole catalogue. */
function modelRefs(ctx: ExtensionContext): string[] {
  const scoped = ctx.scopedModels.map((entry) => modelRef(entry.model));
  const refs =
    scoped.length > 0 ? scoped : ctx.modelRegistry.getAvailable().map(modelRef);
  return [...new Set(refs)].sort();
}

function splitRef(ref: string): { provider: string; id: string } {
  const slash = ref.indexOf("/");
  if (slash < 0) {
    return { provider: ref, id: "" };
  }
  return { provider: ref.slice(0, slash), id: ref.slice(slash + 1) };
}

type Viewer =
  { mode: "direct" } | { mode: "describe"; ref: string; model: CatalogModel };

/** Who sees the capture: the picked vision model when one is set
 * and it is not the active model, else the active model directly.
 * A picked model missing from the catalogue is an Error with the
 * hint — never a silent switch to a model that may take no
 * images — and the tool returns it as its text. */
function pickedViewer(ctx: ExtensionContext): Viewer | Error {
  const chosen = visionModel();
  if (chosen === undefined) {
    return { mode: "direct" };
  }
  const active = ctx.model;
  if (active !== undefined && modelRef(active) === chosen) {
    return { mode: "direct" };
  }
  const { provider, id } = splitRef(chosen);
  const model = ctx.modelRegistry.find(provider, id);
  if (model === undefined) {
    return new Error(
      `the vision model ${chosen} is not in the model list; ${PICK_HINT}`,
    );
  }
  return { mode: "describe", ref: chosen, model };
}

function text(content: string): TextContent {
  return { type: "text", text: content };
}

/** One capture, seen by whoever the choice above picks. The
 * saved full-resolution file is in the text either way, for the
 * crops a downscaled view cannot serve. */
async function shoot(
  ctx: ExtensionContext,
  params: { monitor?: number; prompt?: string },
  signal?: AbortSignal,
): Promise<ShotResult> {
  const monitor = params.monitor ?? 0;
  const who = pickedViewer(ctx);
  if (who instanceof Error) {
    return {
      content: [text(who.message)],
      details: { monitor, mode: "error" },
      isError: true,
    };
  }
  let shot: Capture;
  try {
    shot = await capture({ monitor, signal });
  } catch (error) {
    return {
      content: [text(`the capture failed: ${String(error)}`)],
      details: { monitor, mode: "error" },
      isError: true,
    };
  }
  if (!shot.ok) {
    return {
      content: [text(shot.text)],
      details: { monitor, mode: "error", status: shot.status },
      isError: true,
    };
  }
  const saved =
    `monitor ${monitor}, ${shot.bytes.length} bytes, ` +
    `saved to ${shot.path}`;
  const image: ImageContent = {
    type: "image",
    data: Buffer.from(shot.bytes).toString("base64"),
    mimeType: shot.mime,
  };
  if (who.mode === "direct") {
    return {
      content: [text(saved), image],
      details: {
        monitor,
        mode: "direct",
        bytes: shot.bytes.length,
        path: shot.path,
        mime: shot.mime,
      },
    };
  }
  const details: ShotDetails = {
    monitor,
    mode: "describe",
    bytes: shot.bytes.length,
    path: shot.path,
    mime: shot.mime,
    viewer: who.ref,
  };
  let answer: AssistantMessage;
  try {
    answer = await ctx.modelRegistry.complete(
      who.model,
      {
        messages: [
          {
            role: "user",
            content: [text(params.prompt ?? DESCRIBE_PROMPT), image],
            timestamp: Date.now(),
          },
        ],
      },
      { signal },
    );
  } catch (error) {
    return {
      content: [
        text(
          `the vision model ${who.ref} did not answer (${String(error)}); ` +
            `the capture is saved at ${shot.path}; ${PICK_HINT}`,
        ),
      ],
      details,
      isError: true,
    };
  }
  if (answer.stopReason === "error" || answer.stopReason === "aborted") {
    return {
      content: [
        text(
          `the vision model ${who.ref} did not describe the capture ` +
            `(${answer.errorMessage ?? answer.stopReason}); the capture ` +
            `is saved at ${shot.path}; ${PICK_HINT}`,
        ),
      ],
      details,
      isError: true,
      usage: answer.usage,
    };
  }
  const described = answer.content
    .filter((block) => block.type === "text")
    .map((block) => block.text)
    .join("\n")
    .trim();
  return {
    content: [
      text(
        `${described || "(the vision model answered nothing)"}\n\n` +
          `(${saved}; described by ${who.ref})`,
      ),
    ],
    details,
    usage: answer.usage,
  };
}

/** `/screenshot model`: the picker over the model list, with a
 * clear entry; a cancelled picker leaves the choice alone and
 * says what it is. */
async function pickVisionModel(
  pi: ExtensionAPI,
  ctx: ExtensionContext,
): Promise<void> {
  const current = visionModel();
  const refs = modelRefs(ctx);
  if (refs.length === 0) {
    ctx.ui.notify("no models to pick from: the model list is empty", "error");
    return;
  }
  const values: (string | undefined)[] = [undefined, ...refs];
  const options = [
    current === undefined ? `${NONE} (current)` : NONE,
    ...refs.map((ref) => (ref === current ? `${ref} (current)` : ref)),
  ];
  const choice = await ctx.ui.select("Vision model for screenshots", options);
  if (choice === undefined) {
    ctx.ui.notify(`vision model unchanged: ${current ?? NONE}`, "info");
    return;
  }
  const index = options.indexOf(choice);
  if (index < 0) {
    ctx.ui.notify("that choice is not in the list; nothing changed", "error");
    return;
  }
  const next = values[index];
  try {
    writeVisionModel(next);
  } catch (error) {
    ctx.ui.notify(`could not write the setting: ${String(error)}`, "error");
    return;
  }
  picked = next;
  ctx.ui.notify(
    next
      ? `vision model set to ${next}: it describes captures when it is not the active model`
      : "vision model cleared: the active model sees captures",
    "info",
  );
}

export default function screenshotExtension(pi: ExtensionAPI): void {
  api = pi;
  pi.registerTool({
    name: "screenshot",
    label: "Screenshot",
    description:
      "Capture one screen of the client that started this workspace " +
      "(the user's own machine) and return it as an image — or as a " +
      "vision model's description when one is picked with " +
      "/screenshot model.",
    promptSnippet:
      "Capture the user's screen (the client that started this workspace) and see it",
    promptGuidelines: [
      "Use screenshot when the user asks to see their screen, their monitor, or what is on their desktop.",
      "When a screenshot result is followed by a provider error about images, ask the user to run /screenshot model to pick a vision model.",
    ],
    parameters: Type.Object({
      monitor: Type.Optional(
        Type.Integer({
          minimum: 0,
          description: "the screen to capture, 0 by default",
        }),
      ),
      prompt: Type.Optional(
        Type.String({
          description:
            "what to look at, when a vision model describes the capture",
        }),
      ),
    }),
    async execute(_toolCallId, params, signal, _onUpdate, ctx) {
      return shoot(ctx, params, signal ?? undefined);
    },
  });

  pi.registerCommand("screenshot", {
    description:
      "Capture the client's screen: /screenshot [monitor], or /screenshot model to pick the vision model",
    getArgumentCompletions(prefix: string): AutocompleteItem[] | null {
      const items: AutocompleteItem[] =
        prefix === "" || "model".startsWith(prefix)
          ? [
              {
                value: "model",
                label: "model",
                description: "pick the vision model",
              },
            ]
          : [];
      return items.length > 0 ? items : null;
    },
    async handler(args: string, ctx: ExtensionContext) {
      const arg = args.trim();
      if (arg === "model") {
        await pickVisionModel(pi, ctx);
        return;
      }
      let monitor = 0;
      if (arg !== "") {
        if (!/^\d+$/.test(arg)) {
          ctx.ui.notify(
            "usage: /screenshot [monitor], or /screenshot model",
            "error",
          );
          return;
        }
        monitor = Number(arg);
      }
      const result = await shoot(ctx, { monitor }, ctx.signal);
      const line = result.content
        .filter((block) => block.type === "text")
        .map((block) => block.text)
        .join("\n");
      if (result.isError) {
        ctx.ui.notify(line, "error");
        return;
      }
      // A follow-up reaches the agent when it is idle (a new
      // turn at once) and after its current work when it is not.
      pi.sendUserMessage(result.content, { deliverAs: "followUp" });
    },
  });
}
