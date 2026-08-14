#!/usr/bin/env node
// Emit a speaker-notes handout (the spoken script) + a timing estimate from a deck.json.
// Academic talks are spoken: timing is estimated from the notes, bilingual-aware (EN wpm + CJK cpm).
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { notesHandout, timingReport } from "./lib/notes.mjs";
import { isMain } from "./lib/platform.mjs";
import { parseCheckpointArgument, requireApprovedCheckpoint } from "./lib/checkpoint_gate.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

const fmt = (m) => `${Math.floor(m)}:${String(Math.round((m % 1) * 60)).padStart(2, "0")}`;
const CONTENT_FIELDS = new Set(["text", "transition", "visual_guidance", "takeaway", "source_refs"]);
const FORBIDDEN = /\[(?:MISSING|UNVERIFIED)(?::[^\]]*)?\]|\b(?:ckpt(?:[- ]?\d+)?|checkpoint|audit(?:ed)?|ledger|marker|sha[- ]?256|hash(?:ed|ing)?|artifact[- ]?bundle|resolved_with_audit|pending_human_confirmation|todo|tbd|placeholder|template)\b|<\s*(?:presenter|date|name|author|todo|tbd|placeholder)[^>]*>/i;

function everyString(value, check) {
  if (typeof value === "string") return check(value);
  if (Array.isArray(value)) return value.every((entry) => everyString(entry, check));
  if (value && typeof value === "object") return Object.values(value).every((entry) => everyString(entry, check));
  return true;
}

// Python owns structured note generation.  Node deliberately checks only that the
// renderer-facing legacy field is the exact projection of that source of truth.
export function validateSpeakerNoteProjection(deck) {
  const schema = deck?.meta?.speaker_notes_schema;
  const hasStructuredContent = (deck.slides || []).some((slide) => slide?.speaker_content !== undefined);
  if (!hasStructuredContent && schema === "speaker-content-v1") {
    throw new Error("speaker_content is required for speaker-content-v1 decks");
  }
  if (!hasStructuredContent && schema !== "legacy-v1") {
    throw new Error("legacy speaker notes require meta.speaker_notes_schema='legacy-v1'");
  }
  if (hasStructuredContent && schema !== "speaker-content-v1") {
    throw new Error("structured speaker notes require meta.speaker_notes_schema='speaker-content-v1'");
  }
  let previous = "";
  for (const [index, slide] of (deck.slides || []).entries()) {
    const noteProjection = {
      speaker_notes: slide?.speaker_notes,
      speaker_content: slide?.speaker_content,
    };
    if (!everyString(noteProjection, (text) => !FORBIDDEN.test(text))) {
      throw new Error(`slide ${index + 1}: speaker notes expose forbidden marker, audit, hash, or placeholder text`);
    }
    if (!hasStructuredContent) continue;
    if (!slide?.speaker_content) {
      throw new Error(`slide ${index + 1}: speaker_content is required`);
    }
    if (Object.keys(slide.speaker_content).length !== CONTENT_FIELDS.size || !Object.keys(slide.speaker_content).every((key) => CONTENT_FIELDS.has(key))) {
      throw new Error(`slide ${index + 1}: speaker_content must contain exactly the five supported fields`);
    }
    if (typeof slide.speaker_content.text !== "string" || !slide.speaker_content.text.trim()) {
      throw new Error(`slide ${index + 1}: speaker_content.text must be a non-empty string`);
    }
    const refs = slide.speaker_content.source_refs;
    if (!Array.isArray(refs) || !refs.length || refs.some((ref) => !ref || !Number.isInteger(ref.source_page) || ref.source_page < 1 || typeof ref.section !== "string" || !ref.section.trim() || typeof ref.locator !== "string" || !ref.locator.trim() || typeof ref.evidence_id !== "string" || !ref.evidence_id.trim())) {
      throw new Error(`slide ${index + 1}: speaker_content.source_refs must be meaningful structured refs`);
    }
    if (slide.speaker_notes !== slide.speaker_content.text) {
      throw new Error(`slide ${index + 1}: speaker_notes must equal speaker_content.text`);
    }
    if (previous && previous === slide.speaker_notes) {
      throw new Error(`slide ${index + 1}: adjacent speaker notes must not repeat`);
    }
    previous = slide.speaker_notes;
  }
}

export function writeSpeakerNotes(deckJson, budget, { checkpointRecord, expectedCheckpoint = "CKPT-3", outPath = null } = {}) {
  requireApprovedCheckpoint({
    rootDir: ROOT,
    checkpointRecord,
    artifactPath: deckJson,
    expectedCheckpoint,
  });
  const deck = JSON.parse(fs.readFileSync(deckJson, "utf-8"));
  validateSpeakerNoteProjection(deck);
  const opts = budget ? { budgetMin: budget } : {};
  const out = outPath || path.join(path.dirname(deckJson), "notes.md");
  fs.writeFileSync(out, notesHandout(deck, opts));
  return { out, timing: timingReport(deck, opts) };
}

if (isMain(import.meta.url, process.argv[1])) {
  let parsed;
  try {
    parsed = parseCheckpointArgument(process.argv.slice(2), "speaker_notes.mjs <deck.json> [budgetMin]");
  } catch (error) {
    console.error(`ERROR: ${error.message}`);
    process.exit(1);
  }
  const [deckJson, budgetArg, ...extra] = parsed.positional;
  const budget = budgetArg ? Number(budgetArg) : null;
  if (!deckJson) { console.error("usage: speaker_notes.mjs <deck.json> [budgetMin]"); process.exit(1); }
  if (extra.length || (budgetArg && !Number.isFinite(budget))) { console.error("ERROR: budgetMin must be a number"); process.exit(1); }
  let result;
  try {
    result = writeSpeakerNotes(deckJson, budget, { checkpointRecord: parsed.checkpointRecord });
  } catch (error) {
    console.error(`ERROR: ${error.message}`);
    process.exit(1);
  }
  const { out, timing: t } = result;
  console.log(`Speaker notes -> ${out}`);
  console.log(`Estimated total: ${fmt(t.totalMinutes)}` + (budget ? `  (budget ${budget}:00)` : ""));
  if (budget) {
    const ratio = t.totalMinutes / budget;
    console.log(ratio > 1.15 ? "  ⚠ over budget — cut content or trim notes"
      : ratio < 0.6 ? "  ⚠ well under budget — likely too thin" : "  ✓ within budget");
  }
  if (t.withoutNotes.length) console.log(`Slides without notes: ${t.withoutNotes.join(", ")}`);
  for (const s of t.slides) console.log(`  slide ${String(s.slide).padStart(2)}  ${fmt(s.minutes)}${s.hasNotes ? "" : "  (no notes)"}`);
}
