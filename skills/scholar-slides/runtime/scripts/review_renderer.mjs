#!/usr/bin/env node
// Pending-CKPT-2 review renderer.  This module deliberately renders only local review HTML;
// it never creates presentation/export artifacts and does not need an approved checkpoint.
import fs from "node:fs";
import { createHash } from "node:crypto";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { chromium } from "playwright";

import { renderSlide, validateSpec } from "./lib/layouts.mjs";
import { isMain, resolveChromiumLaunchOptions } from "./lib/platform.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

const THEMES = { "journal-club": "journal-club.css", conference: "conference.css" };
const LANGUAGE_TAG = /^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$/;

function safeRelative(resourcePath) {
  if (typeof resourcePath !== "string" || !resourcePath || path.isAbsolute(resourcePath) || path.win32.isAbsolute(resourcePath) || resourcePath.includes("\\\\")) {
    throw new Error("review asset path must be a non-empty project-relative POSIX path");
  }
  const normalized = path.posix.normalize(resourcePath);
  if (normalized === "." || normalized === ".." || normalized.startsWith("../")) {
    throw new Error("review asset path must not traverse outside the project");
  }
  return normalized;
}

function sha256File(filePath) {
  return createHash("sha256").update(fs.readFileSync(filePath)).digest("hex");
}

function isSha256(value) {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}

function reviewAssetReferences(deck) {
  const refs = new Set();
  for (const slide of Array.isArray(deck.slides) ? deck.slides : []) {
    if (!slide || typeof slide !== "object") continue;
    const figure = slide.figure;
    if (figure && typeof figure.src === "string") refs.add(safeRelative(figure.src));
    const media = slide.media;
    if (media && typeof media === "object") {
      for (const key of ["src", "poster"]) if (typeof media[key] === "string") refs.add(safeRelative(media[key]));
    }
    if (typeof slide.background === "string") refs.add(safeRelative(slide.background));
    if (Array.isArray(slide.images)) {
      for (const image of slide.images) if (image && typeof image.asset === "string") refs.add(safeRelative(image.asset));
    }
  }
  return refs;
}

function validateVisibleAssetGraph(deckJsonPath, deck, graph) {
  if (graph?.schema_version !== 1 || graph?.kind !== "scholar-slides-asset-graph" || !Array.isArray(graph.nodes)) {
    throw new Error("review renderer requires a current scholar-slides asset graph");
  }
  const projectRoot = path.dirname(path.resolve(deckJsonPath));
  const deckRelative = path.relative(projectRoot, path.resolve(deckJsonPath)).split(path.sep).join("/");
  const deckEntry = graph.deck;
  if (!deckEntry || safeRelative(deckEntry.path) !== deckRelative || !isSha256(deckEntry.sha256) || sha256File(deckJsonPath) !== deckEntry.sha256) {
    throw new Error("asset graph deck binding is stale or invalid");
  }
  const declared = new Map();
  for (const node of graph.nodes) {
    if (!node || node.kind !== "visible_asset") continue;
    const relative = safeRelative(node.path);
    if (declared.has(relative) || !isSha256(node.sha256) || !Number.isInteger(node.size_bytes) || node.size_bytes < 0) {
      throw new Error("asset graph visible asset declaration is malformed");
    }
    declared.set(relative, node);
  }
  const referenced = reviewAssetReferences(deck);
  if (referenced.size !== declared.size || [...referenced].some((item) => !declared.has(item))) {
    throw new Error("asset graph visible assets do not exactly match rendered deck resources");
  }
  return { projectRoot, declared };
}

function copyProjectFile(projectRoot, reviewDir, resourcePath) {
  const relative = safeRelative(resourcePath);
  const source = path.resolve(projectRoot, relative);
  const sourceRelative = path.relative(projectRoot, source);
  if (sourceRelative === ".." || sourceRelative.startsWith(`..${path.sep}`) || path.isAbsolute(sourceRelative)) {
    throw new Error("review asset path escapes the project root");
  }
  let component = projectRoot;
  for (const part of relative.split("/")) {
    component = path.join(component, part);
    const info = fs.lstatSync(component);
    if (info.isSymbolicLink()) throw new Error("review asset path may not contain a symbolic link or reparse point");
  }
  const sourceReal = fs.realpathSync(source);
  const rootReal = fs.realpathSync(projectRoot);
  const realRelative = path.relative(rootReal, sourceReal);
  if (realRelative === ".." || realRelative.startsWith(`..${path.sep}`) || path.isAbsolute(realRelative) || !fs.statSync(sourceReal).isFile()) {
    throw new Error("review asset must be a regular project-owned file");
  }
  const destination = path.join(reviewDir, ...relative.split("/"));
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  fs.copyFileSync(sourceReal, destination, fs.constants.COPYFILE_EXCL);
  return destination;
}

function copyBundledAssets(reviewDir, theme) {
  const assets = path.join(reviewDir, "assets");
  fs.mkdirSync(assets, { recursive: true });
  const copy = (source, destination) => fs.cpSync(source, path.join(assets, destination), { recursive: true, errorOnExist: true });
  copy(path.join(ROOT, "node_modules", "katex", "dist", "katex.min.css"), "katex.min.css");
  copy(path.join(ROOT, "node_modules", "katex", "dist", "fonts"), "fonts");
  copy(path.join(ROOT, "assets", "templates", "deck-stage", "tokens.css"), "tokens.css");
  copy(path.join(ROOT, "assets", "templates", "deck-stage", "viewport-base.css"), "viewport-base.css");
  copy(path.join(ROOT, "assets", "templates", "themes", "base-theme.css"), "base-theme.css");
  copy(path.join(ROOT, "assets", "templates", "themes", theme), theme);
}

function localStyles(deck) {
  const theme = THEMES[deck.meta?.theme] || THEMES["journal-club"];
  return [
    "assets/katex.min.css",
    "assets/tokens.css",
    "assets/viewport-base.css",
    "assets/base-theme.css",
    `assets/${theme}`,
  ].map((href) => `<link rel="stylesheet" href="${href}">`).join("\n");
}

function reviewCanvas(slide, index) {
  return `<article class="review-canvas" data-review-slide="${index + 1}" data-json-pointer="/slides/${index}" data-slide-canvas="true">` +
    `<div class="reveal"><div class="slides">${renderSlide(slide, {})}</div></div>` +
    "</article>";
}

export function renderReviewHtml(deck) {
  const problems = validateSpec(deck);
  const errors = problems.filter((item) => item.severity === "error");
  if (errors.length) {
    throw new Error(`deck.json spec is invalid for review: ${errors.map((item) => item.detail).join("; ")}`);
  }
  const requestedLanguage = typeof deck.meta?.language === "string" ? deck.meta.language : "";
  const language = LANGUAGE_TAG.test(requestedLanguage) ? requestedLanguage : "en";
  const title = String(deck.meta?.title || "scholar-slides review").replace(/[<>]/g, "");
  const slides = Array.isArray(deck.slides) ? deck.slides : [];
  return `<!doctype html>
<html lang="${language}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; base-uri 'none'; form-action 'none'; frame-src 'none'; object-src 'none'; connect-src 'none'; script-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self'; font-src 'self'; media-src 'self'">
<title>${title}</title>
${localStyles(deck)}
<style>
html, body { margin: 0; background: #15171b; }
.review-document { display: grid; gap: 24px; justify-content: center; padding: 24px; }
.review-canvas { box-sizing: border-box; width: 1920px; height: 1080px; overflow: hidden; position: relative; background: #fff; }
.review-canvas .reveal, .review-canvas .reveal .slides { width: 100%; height: 100%; position: static; transform: none !important; }
.review-canvas .reveal .slides > section { display: block !important; position: static !important; width: 100%; height: 100%; transform: none !important; }
.review-canvas .reveal .slides > section > .slide-inner { min-height: 100%; }
</style>
</head>
<body>
<main class="review-document" aria-label="Review preview">
${slides.map(reviewCanvas).join("\n")}
</main>
</body>
</html>
`;
}

function isPathWithin(root, candidate) {
  const relative = path.relative(root, candidate);
  return relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative);
}

function isReviewFileUrl(rawUrl, reviewRoot) {
  try {
    const parsed = new URL(rawUrl);
    return parsed.protocol === "file:" && isPathWithin(reviewRoot, path.resolve(fileURLToPath(parsed)));
  } catch {
    return false;
  }
}

export function writeReviewHtml(deckJsonPath, outPath) {
  const deck = JSON.parse(fs.readFileSync(deckJsonPath, "utf8"));
  const html = renderReviewHtml(deck);
  fs.writeFileSync(outPath, html, "utf8");
  return { outPath, slideCount: deck.slides.length };
}

export function buildReviewArtifacts(deckJsonPath, assetGraphPath, reviewDir) {
  const deck = JSON.parse(fs.readFileSync(deckJsonPath, "utf8"));
  const graph = JSON.parse(fs.readFileSync(assetGraphPath, "utf8"));
  if (fs.existsSync(reviewDir)) throw new Error("review output directory must be created atomically by the orchestrator");
  const { projectRoot, declared } = validateVisibleAssetGraph(deckJsonPath, deck, graph);
  fs.mkdirSync(reviewDir, { recursive: false });
  try {
    const theme = THEMES[deck.meta?.theme] || THEMES["journal-club"];
    copyBundledAssets(reviewDir, theme);
    for (const [relative, node] of declared) {
      const destination = copyProjectFile(projectRoot, reviewDir, relative);
      if (fs.statSync(destination).size !== node.size_bytes || sha256File(destination) !== node.sha256) {
        throw new Error("asset graph visible asset hash or size does not match the copied review resource");
      }
    }
    const htmlPath = path.join(reviewDir, "slides-review.html");
    const html = renderReviewHtml(deck);
    fs.writeFileSync(htmlPath, html, "utf8");
    return { htmlPath, slideCount: deck.slides.length };
  } catch (error) {
    fs.rmSync(reviewDir, { recursive: true, force: true });
    throw error;
  }
}

const REVIEW_VIEWPORT = Object.freeze({ width: 1920, height: 1080, device_scale_factor: 1 });

async function waitForStableReview(page) {
  return page.evaluate(async () => {
    if (document.fonts) await document.fonts.ready;
    const images = [...document.images];
    await Promise.all(images.map(async (image) => {
      if (image.complete) return;
      await new Promise((resolve) => {
        image.addEventListener("load", resolve, { once: true });
        image.addEventListener("error", resolve, { once: true });
      });
    }));
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
}

function browserSlideDiagnostics(slide, slideIndex) {
  const slidePointer = slide.dataset.jsonPointer || `/slides/${Math.max(slideIndex - 1, 0)}`;
  const layout = slide.querySelector("[data-layout]")?.getAttribute("data-layout") || slide.dataset.layout || "";
  const rect = (element) => {
    const box = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return {
      x: Math.round(box.x), y: Math.round(box.y), width: Math.round(box.width), height: Math.round(box.height),
      client_width: element.clientWidth, client_height: element.clientHeight,
      scroll_width: element.scrollWidth, scroll_height: element.scrollHeight,
      font_size_px: Number.parseFloat(style.fontSize) || 0,
    };
  };
  const visible = (element) => {
    const style = getComputedStyle(element);
    const box = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && box.width > 0 && box.height > 0;
  };
  const roleFor = (element) => {
    if (element.matches("h1, h2, .s-action-title")) return "title";
    if (element.matches(".s-source, figcaption")) return "footer";
    if (element.matches("th, td, table")) return "table";
    if (element.matches("img")) return "image";
    return "body";
  };
  const semanticRoleFor = (element) => {
    if (element.matches("h1, h2, .s-action-title")) return "title";
    if (element.matches(".s-source, figcaption")) return "footer";
    if (element.matches(".s-page-number, .page-number, [aria-hidden='true']")) return "decorative";
    if (element.matches(".eq, .equation, math") || (element.matches(".s-equation") && element.tagName.toLowerCase() !== "section")) return "equation";
    if (element.matches("th, td, table")) return "table";
    if (element.matches("img")) return "figure";
    return "body";
  };
  const domSelector = (element) => {
    const tag = element.tagName.toLowerCase();
    const classes = [...element.classList]
      .map((name) => name.replace(/[^a-zA-Z0-9_-]/g, "-"))
      .filter(Boolean);
    return `${tag}${classes.map((name) => `.${name}`).join("")}`;
  };
  const pointerFor = (element, role) => {
    if (element.matches(".s-action-title")) return `${slidePointer}/action_title`;
    if (element.matches("h1, h2")) return `${slidePointer}/title`;
    if (element.matches(".s-source, figcaption")) return `${slidePointer}/source_ref`;
    if (element.matches("table")) return `${slidePointer}/table`;
    if (element.matches("th, td")) return `${slidePointer}/table`;
    if (element.matches("img")) return `${slidePointer}/figure/src`;
    if (element.matches("li")) return `${slidePointer}/points`;
    if (element.matches(".evidence-node")) {
      const nodes = [...element.parentElement.querySelectorAll(":scope > .evidence-node")];
      return `${slidePointer}/native_diagram/nodes/${Math.max(nodes.indexOf(element), 0)}/label`;
    }
    return `${slidePointer}/${role}`;
  };
  const lineCount = (element) => {
    const range = document.createRange();
    range.selectNodeContents(element);
    const topRows = new Set([...range.getClientRects()].map((box) => Math.round(box.top)).filter((top) => Number.isFinite(top)));
    return Math.max(topRows.size, 1);
  };
  const diagnosticElement = (element) => {
    const role = roleFor(element);
    const semanticRole = semanticRoleFor(element);
    const column = element.closest(".cols > .col-text, .cols > .col-fig");
    return {
      role,
      semantic_role: semanticRole,
      dom_selector: domSelector(element),
      json_pointer: pointerFor(element, role),
      line_count: role === "title" ? lineCount(element) : undefined,
      table_cells: role === "table" ? element.querySelectorAll("th, td").length : undefined,
      overlap_candidate: (element.matches("h1, h2, .s-action-title, table, .evidence-node") || (element.matches("img") && !element.closest("table"))) && !element.matches("th, td"),
      overflow_check: !element.matches("math"),
      column: column ? (column.classList.contains("col-fig") ? "figure" : "text") : undefined,
      ...rect(element),
    };
  };
  const semanticNodes = [...slide.querySelectorAll(
    "h1, h2, .s-action-title, li, p, td, th, figcaption, .s-source, .s-annotation, .note, .head, .body, .evidence-node, .col-text, .col-fig, .s-author, .s-affiliation, .authors, .affil, .venue, .presenter, .eq, .equation, math, svg text, .s-page-number, .page-number, img, table",
  )].filter(visible);
  const elements = semanticNodes.map(diagnosticElement);
  const textFor = (element) => {
    if (element.matches("img")) return element.getAttribute("alt") || element.getAttribute("aria-label") || "";
    return (element.innerText || element.textContent || "").trim();
  };
  const hasSemanticPayload = (element) => Boolean(
    textFor(element) || element.matches("img, table, math, .eq, .equation") || element.querySelector("img, table, svg, math, video"),
  );
  const semanticElements = elements.filter((element, index) =>
    !["footer", "background", "decorative", "page-number"].includes(element.semantic_role) && hasSemanticPayload(semanticNodes[index]),
  );
  const visibleTextRegions = semanticNodes.map((element) => {
    const role = roleFor(element);
    return {
      text: textFor(element),
      json_pointer: pointerFor(element, role),
      dom_selector: domSelector(element),
      semantic_role: semanticRoleFor(element),
    };
  }).filter((region) => region.text);
  const images = [...slide.querySelectorAll("img")].map((image) => ({
    src: image.getAttribute("src") || "",
    json_pointer: pointerFor(image, "image"),
    complete: image.complete,
    natural_width: image.naturalWidth,
    natural_height: image.naturalHeight,
    object_fit: getComputedStyle(image).objectFit,
    ...rect(image),
  }));
  const columns = [...slide.querySelectorAll(".cols > .col-text, .cols > .col-fig")]
    .filter(visible)
    .map((column, index) => ({
      index,
      kind: column.classList.contains("col-fig") ? "figure" : "text",
      dom_selector: domSelector(column),
      text_length: textFor(column).length,
      has_content: Boolean(textFor(column) || column.querySelector("img, table, svg, math, video")),
      ...rect(column),
    }));
  // innerText follows the browser's visible-text semantics, so QA sees text rendered by every
  // layout rather than only the handful of semantic selectors used for geometry diagnostics.
  const visibleText = [slide.innerText.trim()].filter(Boolean);
  const family = getComputedStyle(slide).fontFamily;
  const isCjk = document.documentElement.lang.toLowerCase().startsWith("zh");
  return {
    slide_index: slideIndex,
    json_pointer: slidePointer,
    layout,
    visible_text: visibleText,
    visible_text_regions: visibleTextRegions,
    elements,
    semantic_elements: semanticElements,
    columns,
    images,
    font: {
      cjk_ok: !isCjk || document.fonts.check(`16px ${family}`, "中文"),
      families: [family],
    },
  };
}

export async function captureReviewScreenshots(htmlPath, screenshotsDir, { timeoutMs = 20000 } = {}) {
  const requested = path.resolve(screenshotsDir);
  if (fs.existsSync(requested)) throw new Error("review screenshot directory already exists; use a fresh atomic review directory");
  const temporary = path.join(path.dirname(requested), `.${path.basename(requested)}.tmp`);
  if (fs.existsSync(temporary)) fs.rmSync(temporary, { recursive: true, force: true });
  fs.mkdirSync(temporary, { recursive: false });
  const networkRequests = [];
  const consoleErrors = [];
  const pageErrors = [];
  const reviewRoot = path.dirname(path.resolve(htmlPath));
  const browser = await chromium.launch(resolveChromiumLaunchOptions());
  try {
    const page = await browser.newPage({
      viewport: { width: REVIEW_VIEWPORT.width, height: REVIEW_VIEWPORT.height },
      deviceScaleFactor: REVIEW_VIEWPORT.device_scale_factor,
      locale: "zh-CN",
      timezoneId: "Asia/Shanghai",
    });
    await page.route("**/*", async (route) => {
      const url = route.request().url();
      if (url.startsWith("about:") || isReviewFileUrl(url, reviewRoot)) return route.continue();
      networkRequests.push(url);
      return route.abort();
    });
    page.on("console", (message) => { if (message.type() === "error") consoleErrors.push(message.text()); });
    page.on("pageerror", (error) => pageErrors.push(error.message));
    await page.goto(pathToFileURL(path.resolve(htmlPath)).href, { waitUntil: "load", timeout: timeoutMs });
    await waitForStableReview(page);
    const canvases = page.locator("[data-slide-canvas='true']");
    const count = await canvases.count();
    if (!count) throw new Error("review HTML has no stable slide canvases");
    const slides = [];
    for (let index = 0; index < count; index += 1) {
      const canvas = canvases.nth(index);
      const box = await canvas.boundingBox();
      if (!box || Math.round(box.width) !== REVIEW_VIEWPORT.width || Math.round(box.height) !== REVIEW_VIEWPORT.height) {
        throw new Error(`review slide ${index + 1} does not match the ${REVIEW_VIEWPORT.width}x${REVIEW_VIEWPORT.height} canvas contract`);
      }
      const target = path.join(temporary, `slide-${String(index + 1).padStart(2, "0")}.png`);
      await canvas.screenshot({ path: target, animations: "disabled" });
      slides.push(await canvas.evaluate(browserSlideDiagnostics, index + 1));
    }
    fs.renameSync(temporary, requested);
    return {
      slide_count: count,
      viewport: REVIEW_VIEWPORT,
      slides,
      network_requests: networkRequests.sort(),
      console_errors: consoleErrors.sort(),
      page_errors: pageErrors.sort(),
      renderer_version: `chromium-${browser.version()}`,
    };
  } catch (error) {
    fs.rmSync(temporary, { recursive: true, force: true });
    throw error;
  } finally {
    await browser.close();
  }
}

if (isMain(import.meta.url, process.argv[1])) {
  const argv = process.argv.slice(2);
  const run = async () => {
    if (argv[0] === "--capture") {
      const [, deckJsonPath, assetGraphPath, reviewDir, ...extra] = argv;
      if (!deckJsonPath || !assetGraphPath || !reviewDir || extra.length) {
        throw new Error("usage: review_renderer.mjs --capture <deck.json> <asset-graph.json> <review-dir>");
      }
      const build = buildReviewArtifacts(deckJsonPath, assetGraphPath, reviewDir);
      const diagnostics = await captureReviewScreenshots(build.htmlPath, path.join(reviewDir, "png"));
      fs.writeFileSync(path.join(reviewDir, "renderer-log.json"), `${JSON.stringify(diagnostics, null, 2)}\n`, "utf8");
      console.log(JSON.stringify(diagnostics));
      return;
    }
    const [deckJsonPath, outPath, ...extra] = argv;
    if (!deckJsonPath || !outPath || extra.length) {
      throw new Error("usage: review_renderer.mjs <deck.json> <review/slides-review.html>");
    }
    const result = writeReviewHtml(deckJsonPath, outPath);
    console.log(`review HTML (${result.slideCount} slides) -> ${result.outPath}`);
  };
  run().catch((error) => {
    console.error(`ERROR: ${error.message}`);
    process.exit(1);
  });
}

export { ROOT };
