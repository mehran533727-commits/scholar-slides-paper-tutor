#!/usr/bin/env node
// Render a built deck.html to a one-page-per-slide vector PDF (projection) and/or per-slide
// PNG screenshots (for the QA self-review stage) using Playwright + Chromium.
import path from "node:path";
import fs from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import { chromium } from "playwright";
import { isMain, resolveChromiumLaunchOptions } from "./lib/platform.mjs";
import { parseCheckpointArgument, requireApprovedCheckpoint } from "./lib/checkpoint_gate.mjs";
import { BUILD_BUNDLE_KIND, verifyReviewBundle, writeReviewRenderEvidence } from "./lib/review_bundle.mjs";
import { enforceDeckCjkFont } from "./lib/font_gate.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

async function withReveal(htmlPath, fn) {
  const browser = await chromium.launch(resolveChromiumLaunchOptions());
  try {
    const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
    await page.goto(pathToFileURL(path.resolve(htmlPath)).href, { waitUntil: "networkidle" });
    await page.waitForFunction(() => window.Reveal && window.Reveal.isReady && window.Reveal.isReady(), null, {
      timeout: 20000,
    });
    return await fn(page);
  } finally {
    await browser.close();
  }
}

// PDF comes from the static print HTML (deck.print.html): no reveal runtime, real CSS @page
// paging -> one vector page per slide, text/equations selectable.
export async function renderPdf(htmlPath, pdfPath, { cjkFont = null } = {}) {
  const printPath = htmlPath.replace(/deck\.html$/, "deck.print.html");
  const target = printPath !== htmlPath ? printPath : htmlPath;
  const browser = await chromium.launch(resolveChromiumLaunchOptions());
  try {
    const page = await browser.newPage();
    await page.goto(pathToFileURL(path.resolve(target)).href, { waitUntil: "networkidle" });
    if (cjkFont) {
      // The verified family must be the one CSS actually chooses; a broad fallback stack alone
      // can pick an earlier installed font that lacks a rarer Han glyph.
      await page.addStyleTag({
        content: `:root { --sans: ${JSON.stringify(cjkFont)}, sans-serif; --serif: ${JSON.stringify(cjkFont)}, serif; }`,
      });
    }
    await page.evaluate(() => (document.fonts ? document.fonts.ready : null));
    await page.pdf({ path: pdfPath, preferCSSPageSize: true, printBackground: true });
  } finally {
    await browser.close();
  }
  // Chromium stamps the current wall-clock time into PDF metadata.  Normalize those fixed-width
  // metadata fields so identical approved inputs produce a stable delivery hash.
  const rawPdf = fs.readFileSync(pdfPath, "latin1");
  const stablePdf = rawPdf.replace(/D:\d{14}/g, "D:20000101000000");
  if (stablePdf !== rawPdf) fs.writeFileSync(pdfPath, stablePdf, "latin1");
  return pdfPath;
}

async function screenshotSlides(htmlPath, outDir, max = 0) {
  const fs = await import("node:fs");
  fs.mkdirSync(outDir, { recursive: true });
  return withReveal(htmlPath, async (page) => {
    // The screenshots ARE the deliverable pixels the aesthetics rubric scores — presentation
    // chrome (nav chevrons, progress bar, page chip) must not be baked into them.
    await page.evaluate(() =>
      window.Reveal.configure({ controls: false, progress: false, slideNumber: false }));
    await page.addStyleTag({
      content: ".reveal .controls, .reveal .progress, .reveal .slide-number { display: none !important; }",
    });
    const total = await page.evaluate(() => window.Reveal.getTotalSlides());
    const n = max > 0 ? Math.min(max, total) : total;
    const shots = [];
    for (let i = 0; i < n; i++) {
      await page.evaluate((idx) => window.Reveal.slide(idx), i);
      await page.waitForTimeout(120);
      const p = path.join(outDir, `slide-${String(i + 1).padStart(2, "0")}.png`);
      await page.screenshot({ path: p });
      shots.push(p);
    }
    return shots;
  });
}

if (isMain(import.meta.url, process.argv[1])) {
  const rawArgs = process.argv.slice(2);
  const reviewIndex = rawArgs.indexOf("--review");
  const isReview = reviewIndex !== -1;
  if (isReview) rawArgs.splice(reviewIndex, 1);
  let parsed;
  try {
    parsed = parseCheckpointArgument(rawArgs, "render_deck.mjs <deck.html> [pdf|png] [outPath] [--review]");
  } catch (error) {
    console.error(`ERROR: ${error.message}`);
    process.exit(1);
  }
  const [htmlPath, mode = "pdf", outArg, ...extra] = parsed.positional;
  if (!htmlPath) {
    console.error("usage: render_deck.mjs <deck.html> [pdf|png] [outPath] [--review] --checkpoint <record.json>");
    process.exit(1);
  }
  if (extra.length || !["pdf", "png"].includes(mode)) {
    console.error("ERROR: mode must be pdf or png");
    process.exit(1);
  }
  if (isReview && mode !== "png") {
    console.error("ERROR: --review only produces PNG screenshots; final PDF requires approved CKPT-3 without --review");
    process.exit(1);
  }
  const run = async () => {
    const record = requireApprovedCheckpoint({
      rootDir: ROOT,
      checkpointRecord: parsed.checkpointRecord,
      expectedCheckpoint: isReview ? "CKPT-2" : "CKPT-3",
    });
    const reviewBundle = isReview
      ? path.join(path.dirname(htmlPath), ".scholar-slides-build.json")
      : record.review_bundle?.path;
    if (!reviewBundle) throw new Error("final render requires a sealed CKPT-2 review bundle in the approved CKPT-3 record");
    verifyReviewBundle({
      bundlePath: reviewBundle,
      deckJsonPath: record.artifact?.path,
      htmlPath,
      acceptedKinds: isReview ? [BUILD_BUNDLE_KIND] : undefined,
    });
    const cjkFont = !isReview && mode === "pdf"
      ? enforceDeckCjkFont({ rootDir: ROOT, deckJsonPath: record.artifact?.path })
      : null;
    if (mode === "png") {
      const dir = outArg || path.join(path.dirname(htmlPath), "slides");
      const shots = await screenshotSlides(htmlPath, dir);
      if (isReview) {
        const evidence = writeReviewRenderEvidence({
          deckJsonPath: record.artifact?.path,
          deckDir: path.dirname(htmlPath),
          screenshotsDir: dir,
          screenshots: shots,
        });
        console.log(`sealed review render evidence -> ${evidence}`);
      }
      console.log(`screenshot ${shots.length} slides -> ${dir}`);
    } else {
      const pdf = outArg || path.join(path.dirname(htmlPath), "deck.pdf");
      await renderPdf(htmlPath, pdf, { cjkFont });
      console.log(`rendered PDF -> ${pdf}`);
    }
  };
  run().catch((e) => {
    console.error(e);
    process.exit(1);
  });
}
