/**
 * msks (#1197): the screenshot pipe's guest side, as one module
 * with no dependency on pi. Plain `node` runs it (Node 22 strips
 * types on its own), which is how the smoke test proves the
 * guest's reach, and the extension beside it (index.ts) imports
 * it.
 *
 * The pipe (#1194) is one loopback HTTP endpoint inside the
 * guest: `GET http://127.0.0.1:<port>/screenshot?monitor=N`
 * answers the capture command's stdout, a command that runs on
 * the client that started the workspace. The port is the one the
 * holder wrote to PORT_FILE (the client's `screenshot_port`,
 * 8662 by default). The file can be absent — a holder that has
 * not landed it yet — and the default answers then.
 *
 * The answers, as docs/screenshot.md's "The guest's contract"
 * table states them: a refused connection (no holder, or a
 * client with no command configured), 400 for a malformed
 * request, 500 for a failed capture command (its stderr tail in
 * the body), 503 when the client runs four captures at once (the
 * body says retry). This module retries a 503 once and turns
 * every failure into one line of text the model reads.
 *
 * Run directly (`node capture.ts [port] [monitor]`), the module
 * captures once, saves the file, and prints one line —
 * `OK <mime> <bytes> <path>`, or `FAIL <status> <text>` — which
 * is the smoke test's probe.
 */

import {
  chmodSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

/** The pipe's default guest-side port (screenshotserv's
 * DEFAULT_PORT). */
export const DEFAULT_PORT = 8662;

/** The file the holder writes the configured port to (#1197). */
export const PORT_FILE = "/tmp/msks-screenshot-port";

/** Where captures stay at full resolution: pi downsamples an
 * image before the model sees it, and small text on a 4K capture
 * needs a crop and a second look. A capture shows the operator's
 * screen, so the directory is this account's alone (see
 * captureDir). */
export const CAPTURE_DIR = "/tmp/msks-screenshots";

/** The pipe kills a hung capture command at 60 s and answers
 * 500; the client's own wait sits past that. */
export const CAPTURE_TIMEOUT_MS = 65_000;

/** The body of a 503 says retry: one retry, after this pause. */
export const BUSY_RETRY_MS = 1_000;

/** The most body text a failure carries: a stderr tail, a rule,
 * a remedy — never a capture's worth of bytes. */
export const MAX_TEXT = 600;

/** A refused connection covers two causes the guest cannot tell
 * apart (nothing listens in both), and this line carries both
 * remedies. */
export const NO_HOLDER_TEXT =
  "no holder serves the pipe: start the workspace from a client " +
  "with screenshot_command set, or run `msks screenshot hold " +
  "<workspace>` on the client";

/** One capture: the bytes and where they were saved, or the
 * failure the pipe answered with (`refused` when nothing
 * listened, the HTTP status otherwise). */
export type Capture =
  | { ok: true; bytes: Uint8Array; mime: string; path: string }
  | { ok: false; status: number | "refused"; text: string };

export interface CaptureOptions {
  /** The screen to capture; 0 by default. */
  monitor?: number;
  /** The port; PORT_FILE's value, else DEFAULT_PORT. */
  port?: number;
  /** Where to read the port from; the test's seam. */
  portFile?: string;
  /** The caller's cancellation (pi's tool signal). */
  signal?: AbortSignal;
}

/** The port the pipe serves on in this guest: the holder's file
 * when it carries a port, the default otherwise — an absent file
 * (a holder that has not landed it) and a malformed one (a
 * half-written line read mid-write) both answer the default. */
export function readPort(portFile: string = PORT_FILE): number {
  let raw: string;
  try {
    raw = readFileSync(portFile, "utf8").trim();
  } catch {
    return DEFAULT_PORT; // absent or unreadable: the default answers
  }
  if (!/^\d{1,5}$/.test(raw)) {
    return DEFAULT_PORT;
  }
  const port = Number(raw);
  return port >= 1 && port <= 65535 ? port : DEFAULT_PORT;
}

function ascii(bytes: Uint8Array, from: number, to: number): string {
  let out = "";
  for (let i = from; i < to && i < bytes.length; i += 1) {
    out += String.fromCharCode(bytes[i]);
  }
  return out;
}

/** The image type from the body's first bytes: the pipe answers
 * `application/octet-stream`, so the bytes are the only evidence.
 * An unknown body is taken as PNG — the recipes all produce it,
 * and the model side tolerates a wrong extension better than a
 * missing one. */
export function sniffMime(bytes: Uint8Array): string {
  if (
    bytes.length >= 8 &&
    bytes[0] === 0x89 &&
    bytes[1] === 0x50 &&
    bytes[2] === 0x4e &&
    bytes[3] === 0x47
  ) {
    return "image/png";
  }
  if (
    bytes.length >= 3 &&
    bytes[0] === 0xff &&
    bytes[1] === 0xd8 &&
    bytes[2] === 0xff
  ) {
    return "image/jpeg";
  }
  if (
    bytes.length >= 12 &&
    ascii(bytes, 0, 4) === "RIFF" &&
    ascii(bytes, 8, 12) === "WEBP"
  ) {
    return "image/webp";
  }
  const head = ascii(bytes, 0, 6);
  if (head === "GIF87a" || head === "GIF89a") {
    return "image/gif";
  }
  return "image/png";
}

/** The file extension that matches the sniffed type. */
export function extensionFor(mime: string): string {
  if (mime === "image/jpeg") {
    return "jpg";
  }
  if (mime === "image/webp") {
    return "webp";
  }
  if (mime === "image/gif") {
    return "gif";
  }
  return "png";
}

let sequence = 0;

/** One candidate directory, made private or refused: it must be a
 * directory this account owns, not a symlink another account
 * planted at the path, and it is chmod to this account alone (a
 * directory that existed with wider modes narrows on the first
 * save). Undefined when the candidate cannot be made private —
 * unwritable, raced away, or another account's. */
function privateDir(candidate: string, uid: number): string | undefined {
  try {
    mkdirSync(candidate, { recursive: true, mode: 0o700 });
    const stat = lstatSync(candidate);
    if (
      !stat.isDirectory() ||
      stat.isSymbolicLink() ||
      (uid >= 0 && stat.uid !== uid)
    ) {
      return undefined;
    }
    chmodSync(candidate, 0o700);
    return candidate;
  } catch {
    return undefined; // unwritable, or raced away: the next candidate
  }
}

/** The directory captures are saved in: CAPTURE_DIR when it can be
 * made this account's alone, else a private one named for the
 * account under the system temp. Both checks run per save — the
 * directory can change hands between two captures. Neither
 * candidate answering is an error the tool reports, never a save
 * into a directory another account reads. */
function captureDir(): string {
  const uid = typeof process.getuid === "function" ? process.getuid() : -1;
  const preferred = privateDir(CAPTURE_DIR, uid);
  if (preferred !== undefined) {
    return preferred;
  }
  const fallback = privateDir(
    join(tmpdir(), `msks-screenshots-${uid < 0 ? "user" : uid}`),
    uid,
  );
  if (fallback === undefined) {
    throw new Error(
      `no private directory for the capture: ${CAPTURE_DIR} is another account's or unwritable`,
    );
  }
  return fallback;
}

/** Save one capture at full resolution, readable by this account
 * alone, named by its stamp (the sequence keeps two captures in
 * one millisecond apart) and typed by its sniffed mime. Returns
 * the path. */
export function saveCapture(bytes: Uint8Array, mime: string): string {
  const dir = captureDir();
  sequence = (sequence + 1) % 1_000_000;
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const path = `${dir}/screenshot-${stamp}-${sequence}.${extensionFor(mime)}`;
  writeFileSync(path, bytes, { mode: 0o600 });
  return path;
}

/** The failure line for a transport error: the client-side
 * timeout, the caller's cancellation, or the refused connection
 * with both remedies and the socket's own word. */
function refusedText(
  error: unknown,
  signal: AbortSignal,
  caller: AbortSignal | undefined,
): string {
  const reason = signal.reason as { name?: string } | undefined;
  if (reason !== undefined && reason.name === "TimeoutError") {
    return `the pipe did not answer within ${CAPTURE_TIMEOUT_MS / 1000} s`;
  }
  if (caller?.aborted) {
    return "the capture was cancelled";
  }
  const cause = (error as { cause?: unknown }).cause;
  const detail =
    cause instanceof Error
      ? cause.message
      : error instanceof Error
        ? error.message
        : String(error);
  return `${NO_HOLDER_TEXT} (${detail})`;
}

/** A failure body as one line: whitespace collapsed and capped,
 * so a stderr tail or a rule stays readable and a hostile body
 * cannot flood the context. */
function bodyText(bytes: Uint8Array): string {
  const text = new TextDecoder("utf-8", { fatal: false })
    .decode(bytes)
    .replace(/\s+/g, " ")
    .trim();
  return text.length > MAX_TEXT ? `${text.slice(0, MAX_TEXT)}…` : text;
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolvePromise, reject) => {
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolvePromise();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      reject(
        signal.reason instanceof Error ? signal.reason : new Error("aborted"),
      );
    };
    if (signal.aborted) {
      onAbort();
      return;
    }
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

/** Capture one screen through the pipe. A 503 is retried once;
 * every other answer is final. The timeout covers the whole
 * exchange, the caller's signal beside it. */
export async function capture(options: CaptureOptions = {}): Promise<Capture> {
  const monitor = options.monitor ?? 0;
  const port = options.port ?? readPort(options.portFile);
  const url = `http://127.0.0.1:${port}/screenshot?monitor=${monitor}`;
  const timeout = AbortSignal.timeout(CAPTURE_TIMEOUT_MS);
  const signal = options.signal
    ? AbortSignal.any([options.signal, timeout])
    : timeout;
  for (let attempt = 0; ; attempt += 1) {
    let response: Response;
    try {
      response = await fetch(url, { signal });
    } catch (error) {
      return {
        ok: false,
        status: "refused",
        text: refusedText(error, signal, options.signal),
      };
    }
    const body = new Uint8Array(await response.arrayBuffer());
    if (response.status === 200) {
      const mime = sniffMime(body);
      return { ok: true, bytes: body, mime, path: saveCapture(body, mime) };
    }
    if (response.status === 503 && attempt === 0) {
      try {
        await sleep(BUSY_RETRY_MS, signal);
      } catch (error) {
        return {
          ok: false,
          status: "refused",
          text: refusedText(error, signal, options.signal),
        };
      }
      continue;
    }
    return {
      ok: false,
      status: response.status,
      text: `the pipe answered ${response.status}: ${bodyText(body) || "(no body)"}`,
    };
  }
}

/** The command-line probe: `node capture.ts [port] [monitor]`
 * prints one line and exits 0 on a capture, 1 on a failure. */
async function main(argv: string[]): Promise<number> {
  const [portArg, monitorArg] = argv;
  const port = portArg !== undefined ? Number(portArg) : undefined;
  const monitor = monitorArg !== undefined ? Number(monitorArg) : 0;
  if (
    (port !== undefined && !Number.isInteger(port)) ||
    !Number.isInteger(monitor) ||
    monitor < 0
  ) {
    console.error(
      "FAIL usage: node capture.ts [port] [monitor] — both are whole numbers",
    );
    return 2;
  }
  const shot = await capture({ port, monitor });
  if (shot.ok) {
    console.log(`OK ${shot.mime} ${shot.bytes.length} ${shot.path}`);
    return 0;
  }
  console.log(`FAIL ${shot.status} ${shot.text}`);
  return 1;
}

const invoked = process.argv[1];
if (
  invoked !== undefined &&
  resolve(invoked) === fileURLToPath(import.meta.url)
) {
  main(process.argv.slice(2)).then(
    (code) => {
      process.exitCode = code;
    },
    (error: unknown) => {
      console.log(`FAIL error ${String(error)}`);
      process.exitCode = 2;
    },
  );
}
