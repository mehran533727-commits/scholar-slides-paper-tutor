#!/usr/bin/env node
// Final MVP export coordinator.  It never renders directly: each child exporter keeps its own
// CKPT-3 validation, and this wrapper verifies the sealed review bundle before creating any
// documented artifact alias.  Child processes always receive argument arrays with shell:false.
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { parseCheckpointArgument, requireApprovedCheckpoint } from "./lib/checkpoint_gate.mjs";
import { verifyReviewBundle } from "./lib/review_bundle.mjs";
import { isMain } from "./lib/platform.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function exportPreflight(deckJsonPath, deckDir, checkpointRecord) {
  const record = requireApprovedCheckpoint({
    rootDir: ROOT,
    checkpointRecord,
    artifactPath: deckJsonPath,
    expectedCheckpoint: "CKPT-3",
  });
  const htmlPath = path.join(deckDir, "deck.html");
  if (!fs.existsSync(htmlPath)) {
    throw new Error(`built interactive HTML is missing: ${htmlPath}; build and review the approved deck first`);
  }
  if (!record.review_bundle?.path) {
    throw new Error("approved CKPT-3 record has no sealed review bundle");
  }
  verifyReviewBundle({
    bundlePath: record.review_bundle.path,
    deckJsonPath,
    htmlPath,
  });
  return record;
}

function runChild(runner, nodeExecutable, script, args) {
  const result = runner(nodeExecutable, [path.join(ROOT, "scripts", script), ...args], {
    cwd: ROOT,
    encoding: "utf8",
    shell: false,
  });
  if (result.error || result.status !== 0) {
    const detail = [result.stdout, result.stderr, result.error?.message].filter(Boolean).join("\n").trim();
    throw new Error(`${script} failed${detail ? `: ${detail}` : ""}`);
  }
}

/**
 * Export canonical MVP deliverables alongside a reviewed build directory.
 *
 * ``preflight`` and ``runner`` are injectable solely for contract tests.  Production invokes
 * CKPT-3 verification and the existing individual exporter CLIs, each of which independently
 * verifies the same checkpoint before writing.
 */
export function exportArtifacts(deckJsonPath, deckDir, {
  checkpointRecord,
  budgetMin = null,
  preflight = exportPreflight,
  runner = spawnSync,
  nodeExecutable = process.execPath,
} = {}) {
  const resolvedDeck = path.resolve(deckJsonPath);
  const resolvedDir = path.resolve(deckDir || path.join(path.dirname(resolvedDeck), "deck"));
  preflight(resolvedDeck, resolvedDir, checkpointRecord);
  const html = path.join(resolvedDir, "slides.html");
  const pdf = path.join(resolvedDir, "slides.pdf");
  const pptx = path.join(resolvedDir, "slides.pptx");
  const speakerNotes = path.join(resolvedDir, "speaker_notes.md");
  const builtHtml = path.join(resolvedDir, "deck.html");

  runChild(runner, nodeExecutable, "render_deck.mjs", [
    builtHtml, "pdf", pdf, "--checkpoint", checkpointRecord,
  ]);
  runChild(runner, nodeExecutable, "export_pptx.mjs", [
    resolvedDeck, pptx, "--checkpoint", checkpointRecord,
  ]);
  const noteArgs = [resolvedDeck];
  if (budgetMin !== null) noteArgs.push(String(budgetMin));
  noteArgs.push("--checkpoint", checkpointRecord);
  runChild(runner, nodeExecutable, "speaker_notes.mjs", noteArgs);

  const legacyNotes = path.join(path.dirname(resolvedDeck), "notes.md");
  if (!fs.existsSync(legacyNotes)) {
    throw new Error(`speaker_notes.mjs completed without its expected notes artifact: ${legacyNotes}`);
  }
  // Copy only after all protected exporters have succeeded; the canonical HTML and notes names
  // then form one coherent hand-off set alongside the existing self-contained assets directory.
  fs.copyFileSync(builtHtml, html);
  fs.copyFileSync(legacyNotes, speakerNotes);
  return { html, pdf, pptx, speakerNotes };
}

if (isMain(import.meta.url, process.argv[1])) {
  let parsed;
  try {
    parsed = parseCheckpointArgument(
      process.argv.slice(2),
      "export_artifacts.mjs <deck.json> [deckDir] [budgetMin]",
    );
  } catch (error) {
    console.error(`ERROR: ${error.message}`);
    process.exit(1);
  }
  const [deckJson, deckDir, budgetArg, ...extra] = parsed.positional;
  const budgetMin = budgetArg ? Number(budgetArg) : null;
  if (!deckJson || extra.length || (budgetArg && !Number.isFinite(budgetMin))) {
    console.error("usage: export_artifacts.mjs <deck.json> [deckDir] [budgetMin] --checkpoint <ckpt-3-record>");
    process.exit(1);
  }
  try {
    const result = exportArtifacts(deckJson, deckDir, {
      checkpointRecord: parsed.checkpointRecord,
      budgetMin,
    });
    console.log(`HTML          -> ${result.html}`);
    console.log(`PDF           -> ${result.pdf}`);
    console.log(`PPTX          -> ${result.pptx}`);
    console.log(`Speaker notes -> ${result.speakerNotes}`);
  } catch (error) {
    console.error(`ERROR: ${error.message}`);
    process.exit(1);
  }
}
