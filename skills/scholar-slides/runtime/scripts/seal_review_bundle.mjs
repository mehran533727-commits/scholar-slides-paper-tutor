#!/usr/bin/env node
// Freeze the CKPT-2 review evidence (built HTML, QA report, and screenshots) for CKPT-3.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseCheckpointArgument, requireApprovedCheckpoint } from "./lib/checkpoint_gate.mjs";
import {
  BUILD_BUNDLE_KIND,
  fileEntry,
  verifyReviewBundle,
  verifyReviewRenderEvidence,
  writeReviewBundle,
} from "./lib/review_bundle.mjs";
import { isMain } from "./lib/platform.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function qaReportEvidence(qaPath, approvedDeck) {
  let report;
  try {
    report = JSON.parse(fs.readFileSync(qaPath, "utf8"));
  } catch (error) {
    throw new Error(`QA report cannot be read: ${error.message}`);
  }
  if (!report || report.schema_version !== 1 || report.kind !== "scholar-slides-qa-report" || !Array.isArray(report.findings)) {
    throw new Error("QA report must be a current scholar-slides-qa-report with a findings array");
  }
  const expectedDeck = fileEntry(approvedDeck, "approved deck artifact");
  if (!report.deck || report.deck.path !== expectedDeck.path || report.deck.sha256 !== expectedDeck.sha256) {
    throw new Error("QA report deck does not match the approved CKPT-2 artifact");
  }
  for (const finding of report.findings) {
    if (!finding || typeof finding !== "object" || !["P0", "P1", "P2", "P3"].includes(finding.severity)) {
      throw new Error("QA report contains an invalid finding severity");
    }
    if (["P0", "P1"].includes(finding.severity)) {
      throw new Error(`QA report has unresolved ${finding.severity} finding(s); repair them before CKPT-3`);
    }
  }
  return fileEntry(qaPath, "QA report");
}

export function sealReviewBundle(deckDir, { checkpointRecord, screenshotsDir, outPath } = {}) {
  const record = requireApprovedCheckpoint({
    rootDir: ROOT,
    checkpointRecord,
    expectedCheckpoint: "CKPT-2",
  });
  const htmlPath = path.join(deckDir, "deck.html");
  verifyReviewBundle({
    bundlePath: path.join(deckDir, ".scholar-slides-build.json"),
    deckJsonPath: record.artifact?.path,
    htmlPath,
    acceptedKinds: [BUILD_BUNDLE_KIND],
  });
  const qaPath = path.join(deckDir, "qa_report.json");
  const deck = JSON.parse(fs.readFileSync(record.artifact.path, "utf8"));
  const expectedSlides = Array.isArray(deck.slides) ? deck.slides.length : 0;
  if (!expectedSlides) throw new Error("cannot seal review bundle for a deck with no slides");
  const resolvedScreenshotsDir = screenshotsDir || path.join(deckDir, "slides");
  const reviewRenderPath = path.join(deckDir, ".scholar-slides-review-render.json");
  const reviewRender = verifyReviewRenderEvidence({
    evidencePath: reviewRenderPath,
    deckJsonPath: record.artifact.path,
    htmlPath,
    screenshotsDir: resolvedScreenshotsDir,
  });
  const evidence = {
    qa_report: qaReportEvidence(qaPath, record.artifact.path),
    review_render: fileEntry(reviewRender.evidencePath, "review render evidence"),
    screenshots: reviewRender.screenshots,
  };
  return writeReviewBundle({
    deckJsonPath: record.artifact.path,
    deckDir,
    outPath: outPath || path.join(deckDir, ".scholar-slides-review.json"),
    evidence,
  });
}

if (isMain(import.meta.url, process.argv[1])) {
  let parsed;
  try {
    parsed = parseCheckpointArgument(process.argv.slice(2), "seal_review_bundle.mjs <deckDir> [screenshotsDir] [out.json]");
  } catch (error) {
    console.error(`ERROR: ${error.message}`);
    process.exit(1);
  }
  const [deckDir, screenshotsDir, outPath, ...extra] = parsed.positional;
  if (!deckDir || extra.length) {
    console.error("usage: seal_review_bundle.mjs <deckDir> [screenshotsDir] [out.json] --checkpoint <ckpt-2-record>");
    process.exit(1);
  }
  try {
    const bundle = sealReviewBundle(deckDir, { checkpointRecord: parsed.checkpointRecord, screenshotsDir, outPath });
    console.log(`sealed review bundle -> ${bundle}`);
  } catch (error) {
    console.error(`ERROR: ${error.message}`);
    process.exit(1);
  }
}
