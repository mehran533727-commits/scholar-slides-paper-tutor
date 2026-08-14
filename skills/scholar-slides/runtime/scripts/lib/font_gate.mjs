import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { resolvePythonCommand } from "./platform.mjs";

const CJK_RE = /[\u3400-\u9fff]/gu;

export function requiredCjkText(deck) {
  // Use unique code points in deterministic first-seen order.  The font preflight must cover
  // the deck's real Han characters, not merely a friendly sample phrase.
  return [...new Set(JSON.stringify(deck).match(CJK_RE) || [])].join("");
}

export function deckRequiresCjkFont(deck) {
  return requiredCjkText(deck).length > 0;
}

export function enforceDeckCjkFont({
  rootDir,
  deckJsonPath,
  env = process.env,
  platform = process.platform,
  runner = spawnSync,
}) {
  const deck = JSON.parse(fs.readFileSync(deckJsonPath, "utf8"));
  const text = requiredCjkText(deck);
  if (!text) return null;
  const python = resolvePythonCommand({ rootDir, env, platform });
  const result = runner(
    python.command,
    [
      ...python.args,
      path.join(rootDir, "scripts", "font_preflight.py"),
      "--language", "zh", "--require-cjk", "--json", "--text", text,
    ],
    { cwd: rootDir, env, encoding: "utf8", shell: false },
  );
  if (result.error || result.status !== 0) {
    const detail = [result.stdout, result.stderr, result.error?.message].filter(Boolean).join("\n").trim();
    throw new Error(`Chinese final output requires a verified CJK font.${detail ? ` ${detail}` : ""}`);
  }
  try {
    const response = JSON.parse(result.stdout);
    if (!response.family) throw new Error("CJK font preflight returned no verified font family");
    if (response.covered_text !== text) {
      throw new Error("CJK font preflight did not cover the actual deck text");
    }
    return response.family;
  } catch (error) {
    if (error.message.includes("no verified")) throw error;
    throw new Error(`CJK font preflight returned invalid JSON: ${error.message}`);
  }
}
