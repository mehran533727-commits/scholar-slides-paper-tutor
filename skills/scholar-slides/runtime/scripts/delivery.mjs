#!/usr/bin/env node
// Task9 formal delivery coordinator.  This layer is deliberately separate from the legacy
// CKPT-3 exporter: a confirmed CKPT-2 is the human approval boundary, while this command builds
// disposable render inputs and records machine-verifiable delivery evidence.
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";
import { chromium } from "playwright";
import { buildDeck } from "./build_deck.mjs";
import { renderPdf } from "./render_deck.mjs";
import { exportPptx } from "./export_pptx.mjs";
import { writeSpeakerNotes } from "./speaker_notes.mjs";
import { requireApprovedCheckpoint } from "./lib/checkpoint_gate.mjs";
import { isMain, resolveChromiumLaunchOptions, resolvePythonCommand } from "./lib/platform.mjs";
import { enforceDeckCjkFont } from "./lib/font_gate.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const RELEASE_VERSION = fs.readFileSync(path.join(ROOT, "VERSION"), "utf8").trim();
export const DELIVERY_FORMATS = Object.freeze(["html", "pdf", "pptx", "notes"]);
export const DELIVERY_COMPANIONS = Object.freeze(["script", "summary"]);

export function parseFormats(value = DELIVERY_FORMATS.join(",")) {
  const values = String(value).split(",").map((item) => item.trim().toLowerCase()).filter(Boolean);
  if (!values.length) throw new Error("at least one format is required");
  const unknown = values.filter((item) => !DELIVERY_FORMATS.includes(item));
  if (unknown.length) throw new Error(`unsupported format(s): ${unknown.join(", ")}`);
  return DELIVERY_FORMATS.filter((item) => values.includes(item));
}

function formalFormatIssues(formats) {
  const values = Array.isArray(formats) ? formats : [];
  const missing = DELIVERY_FORMATS.filter((format) => !values.includes(format));
  const unknown = values.filter((format) => !DELIVERY_FORMATS.includes(format));
  const duplicate = values.filter((format, index) => values.indexOf(format) !== index);
  const issues = [];
  if (missing.length || unknown.length || duplicate.length || values.length !== DELIVERY_FORMATS.length) {
    issues.push(`delivery requires all primary formats: ${DELIVERY_FORMATS.join(",")}`);
  }
  return issues;
}

export function relativePosix(projectDir, target) {
  const root = path.resolve(projectDir);
  const absolute = path.resolve(target);
  const rel = path.relative(root, absolute);
  if (!rel || rel === ".." || rel.startsWith(`..${path.sep}`) || path.isAbsolute(rel)) {
    throw new Error(`path is outside project: ${target}`);
  }
  return rel.split(path.sep).join("/");
}

export function sha256File(filePath) {
  return crypto.createHash("sha256").update(fs.readFileSync(filePath)).digest("hex");
}

function normalizeResourceReference(value, label = "HTML resource") {
  const raw = String(value || "").trim();
  if (!raw || raw.startsWith("#") || /^data:/i.test(raw)) return null;
  if (/^(?:[a-z][a-z\d+.-]*:|\/\/)/i.test(raw) || path.isAbsolute(raw) || /^[A-Za-z]:[\\/]/.test(raw)) {
    throw new Error(`delivery-runtime-invalid-reference: ${label} ${raw}`);
  }
  const withoutQuery = raw.split(/[?#]/, 1)[0].replace(/\\/g, "/");
  const normalized = path.posix.normalize(withoutQuery);
  if (!normalized || normalized === ".") return null;
  if (normalized === ".." || normalized.startsWith("../") || normalized.startsWith("/")) {
    throw new Error(`delivery-runtime-invalid-reference: ${label} ${raw}`);
  }
  return normalized;
}

function assetGraphFor(projectDir, assetGraphPath) {
  const graphPath = assetGraphPath || path.join(projectDir, "asset-graph.json");
  const graph = readJsonFile(graphPath, "approved asset graph");
  if (graph.kind !== "scholar-slides-asset-graph" || !Array.isArray(graph.nodes)) {
    throw new Error("delivery-asset-not-in-approved-graph: invalid approved asset graph");
  }
  const visible = new Map();
  for (const node of graph.nodes.filter((item) => item?.kind === "visible_asset")) {
    const relative = normalizeResourceReference(node.path, "approved asset graph path");
    if (!relative) continue;
    if (visible.has(relative)) throw new Error(`delivery-asset-not-in-approved-graph: duplicate graph path ${relative}`);
    const source = path.resolve(projectDir, relative);
    relativePosix(projectDir, source);
    if (!fs.existsSync(source) || !fs.statSync(source).isFile()) throw new Error(`delivery-asset-not-in-approved-graph: missing ${relative}`);
    if (node.sha256 && sha256File(source) !== node.sha256) throw new Error(`delivery-asset-graph-sha-mismatch: ${relative}`);
    const logicalId = String(node.id || "").replace(/^visible_asset:/, "").replace(/^.*\//, "").replace(/\.[^.]+$/, "");
    visible.set(relative, { logicalId, source, sha256: node.sha256 || sha256File(source), node });
  }
  return { visible, forbidden: new Set((graph.forbidden_assets || []).map((item) => String(item))) };
}

function localPathFrom(root, relative, label) {
  const absolute = path.resolve(root, relative);
  relativePosix(root, absolute);
  if (!fs.existsSync(absolute) || !fs.statSync(absolute).isFile()) throw new Error(`delivery-runtime-missing-asset: ${label} ${relative}`);
  return absolute;
}

function addRuntimeReference(map, relative, stagedRoot, reason) {
  const staged = localPathFrom(stagedRoot, relative, "runtime dependency");
  const previous = map.get(relative);
  if (previous) {
    if (previous.reason !== reason && previous.reason === "css-asset") previous.reason = reason;
    return previous;
  }
  const entry = { outputPath: relative, stagedPath: staged, sha256: sha256File(staged), size_bytes: fs.statSync(staged).size, reason };
  map.set(relative, entry);
  return entry;
}

function cssUrls(css) {
  const refs = [];
  const pattern = /url\(\s*(["']?)([^"')]+)\1\s*\)/gi;
  for (const match of String(css).matchAll(pattern)) refs.push(match[2].trim());
  return refs;
}

function cssImports(css) {
  const refs = [];
  const pattern = /@import\s+(?:url\(\s*)?(["']?)([^"')\s;]+)\1\s*\)?/gi;
  for (const match of String(css).matchAll(pattern)) refs.push(match[2].trim());
  return refs;
}

function normalizePptxArchive(filePath) {
  const python = resolvePythonCommand({ rootDir: ROOT });
  const script = [
    "import os, re, sys, tempfile, zipfile",
    "src = sys.argv[1]; fd, tmp = tempfile.mkstemp(prefix='.pptx-stable-', suffix='.pptx', dir=os.path.dirname(src)); os.close(fd)",
    "try:",
    "    with zipfile.ZipFile(src, 'r') as zin, zipfile.ZipFile(tmp, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zout:",
    "        for name in sorted(zin.namelist()):",
    "            source = zin.getinfo(name); data = zin.read(name); data = re.sub(rb'\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(?:\\.\\d+)?Z', b'2000-01-01T00:00:00Z', data) if name == 'docProps/core.xml' else data; info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)); info.compress_type = zipfile.ZIP_DEFLATED; info.create_system = 3; info.external_attr = 0o600 << 16; info.comment = source.comment; zout.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)",
    "    os.replace(tmp, src)",
    "finally:",
    "    if os.path.exists(tmp): os.unlink(tmp)",
  ].join("\n");
  const result = spawnSync(python.command, [...python.args, "-c", script, filePath], { cwd: ROOT, encoding: "utf8", shell: false });
  if (result.error || result.status !== 0) throw new Error(`PPTX deterministic archive normalization failed: ${result.stderr || result.error?.message || "unknown error"}`);
}

/**
 * Parse the generated HTML with the browser DOM, then resolve only local resources that are
 * reachable from the HTML/CSS chain.  Paper images are separately checked against the approved
 * asset graph; the source project is never copied wholesale.
 */
export async function collectHtmlRuntimeAssets(htmlPath, { projectDir, stagedRoot, assetGraphPath } = {}) {
  const project = path.resolve(projectDir || path.dirname(htmlPath));
  const stage = path.resolve(stagedRoot || path.dirname(htmlPath));
  const graph = assetGraphFor(project, assetGraphPath);
  const browser = await chromium.launch(resolveChromiumLaunchOptions());
  let refs;
  try {
    const page = await browser.newPage();
    await page.goto(pathToFileURL(path.resolve(htmlPath)).href, { waitUntil: "domcontentloaded" });
    refs = await page.evaluate(() => ({
      stylesheets: [...document.querySelectorAll('link[rel~="stylesheet"][href]')].map((node) => node.getAttribute("href")),
      scripts: [...document.querySelectorAll("script[src]")].map((node) => node.getAttribute("src")),
      images: [...document.querySelectorAll("img[src]")].map((node) => ({ src: node.getAttribute("src"), alt: node.getAttribute("alt") || "" })),
    }));
  } finally {
    await browser.close();
  }
  const runtime = new Map();
  const paper = new Map();
  const addPaper = (relative, alt) => {
    if (!relative.startsWith("figures/")) throw new Error(`delivery-runtime-invalid-reference: paper image ${relative}`);
    const approved = graph.visible.get(relative);
    if (!approved) throw new Error(`delivery-asset-not-in-approved-graph: ${relative}`);
    if (graph.forbidden.has(approved.logicalId) || graph.forbidden.has(relative.replace(/^figures\//, "").replace(/\.[^.]+$/, ""))) {
      throw new Error(`delivery-forbidden-asset: ${approved.logicalId || relative}`);
    }
    const staged = localPathFrom(stage, relative, "paper image");
    if (sha256File(staged) !== approved.sha256) throw new Error(`delivery-asset-graph-sha-mismatch: staged ${relative}`);
    if (!paper.has(relative)) paper.set(relative, { outputPath: relative, stagedPath: staged, sourcePath: approved.source, sha256: approved.sha256, size_bytes: fs.statSync(staged).size, reason: "html-image", logical_id: approved.logicalId, alt });
  };
  for (const image of refs.images || []) {
    const relative = normalizeResourceReference(image.src, "HTML image");
    if (!relative) continue;
    if (relative.startsWith("figures/")) addPaper(relative, image.alt);
    else if (relative.startsWith("assets/")) addRuntimeReference(runtime, relative, stage, "html-image");
    else throw new Error(`delivery-runtime-invalid-reference: HTML image ${relative}`);
  }
  const cssQueue = [];
  for (const raw of refs.stylesheets || []) {
    const relative = normalizeResourceReference(raw, "HTML stylesheet");
    if (!relative || !relative.startsWith("assets/")) throw new Error(`delivery-runtime-invalid-reference: stylesheet ${raw}`);
    addRuntimeReference(runtime, relative, stage, "html-stylesheet");
    if (relative.toLowerCase().endsWith(".css")) cssQueue.push(relative);
  }
  for (const raw of refs.scripts || []) {
    const relative = normalizeResourceReference(raw, "HTML script");
    if (!relative || !relative.startsWith("assets/")) throw new Error(`delivery-runtime-invalid-reference: script ${raw}`);
    addRuntimeReference(runtime, relative, stage, "html-script");
  }
  const seenCss = new Set();
  while (cssQueue.length) {
    const relative = cssQueue.shift();
    if (seenCss.has(relative)) continue;
    seenCss.add(relative);
    const css = fs.readFileSync(localPathFrom(stage, relative, "CSS dependency"), "utf8");
    for (const raw of [...cssImports(css), ...cssUrls(css)]) {
      const child = normalizeResourceReference(raw, `CSS dependency ${relative}`);
      if (!child) continue;
      const resolved = path.posix.normalize(path.posix.join(path.posix.dirname(relative), child));
      if (resolved === ".." || resolved.startsWith("../")) throw new Error(`delivery-runtime-invalid-reference: CSS dependency ${raw}`);
      const reason = resolved.startsWith("assets/fonts/") ? "css-font" : "css-asset";
      addRuntimeReference(runtime, resolved, stage, reason);
      if (resolved.toLowerCase().endsWith(".css")) cssQueue.push(resolved);
    }
  }
  const runtimeDependencies = [...runtime.values()].sort((a, b) => a.outputPath.localeCompare(b.outputPath));
  const paperAssets = [...paper.values()].sort((a, b) => a.outputPath.localeCompare(b.outputPath));
  return {
    htmlReferences: {
      stylesheets: [...new Set((refs.stylesheets || []).map((raw) => normalizeResourceReference(raw, "HTML stylesheet")).filter(Boolean))].sort(),
      scripts: [...new Set((refs.scripts || []).map((raw) => normalizeResourceReference(raw, "HTML script")).filter(Boolean))].sort(),
      images: [...new Set((refs.images || []).map((image) => normalizeResourceReference(image.src, "HTML image")).filter(Boolean))].sort(),
    },
    runtimeDependencies,
    paperAssets,
  };
}

export function validateHtmlMarkup(filePath, { runner = spawnSync, rootDir = ROOT } = {}) {
  const python = resolvePythonCommand({ rootDir });
  const script = [
    "import json, sys",
    "from html.parser import HTMLParser",
    "class P(HTMLParser):",
    "    void = {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}",
    "    def __init__(self): super().__init__(convert_charrefs=False); self.findings=[]; self.stack=[]",
    "    def start(self, tag, attrs):",
    "        names=[name.lower() for name,_ in attrs]; dup=sorted({name for name in names if names.count(name)>1})",
    "        if dup: self.findings.append({'code':'delivery-html-duplicate-attribute','tag':tag,'attributes':dup})",
    "        if tag.lower() == 'img' and not any(name.lower() == 'alt' for name,_ in attrs): self.findings.append({'code':'delivery-html-missing-alt','tag':tag})",
    "        if tag.lower() not in self.void: self.stack.append(tag.lower())",
    "    def handle_starttag(self, tag, attrs): self.start(tag, attrs)",
    "    def handle_startendtag(self, tag, attrs): self.start(tag, attrs)",
    "    def handle_endtag(self, tag):",
    "        tag=tag.lower()",
    "        if tag in self.stack:",
    "            if self.stack[-1] != tag: self.findings.append({'code':'delivery-html-invalid-nesting','tag':tag})",
    "            while self.stack and self.stack[-1] != tag: self.stack.pop()",
    "            if self.stack: self.stack.pop()",
    "        else: self.findings.append({'code':'delivery-html-invalid-nesting','tag':tag})",
    "p=P(); p.feed(open(sys.argv[1], encoding='utf-8').read()); p.close()",
    "for tag in reversed(p.stack): p.findings.append({'code':'delivery-html-unclosed-element','tag':tag})",
    "print(json.dumps({'findings':p.findings}, ensure_ascii=False))",
  ].join("\n");
  const result = runner(python.command, [...python.args, "-c", script, filePath], { cwd: rootDir, encoding: "utf8", shell: false });
  if (result.error || result.status !== 0) throw new Error(`HTML structural validation failed: ${result.stderr || result.error?.message || "unknown error"}`);
  const payload = JSON.parse(result.stdout);
  if (payload.findings?.length) throw new Error(`delivery-html-invalid: ${payload.findings.map((finding) => `${finding.code.replaceAll("-", " ")}${finding.tag ? ` (${finding.tag})` : ""}`).join(", ")}`);
  return { status: "pass", findings: [] };
}

function pngDimensions(filePath) {
  const bytes = fs.readFileSync(filePath);
  if (bytes.length < 24 || bytes.readUInt32BE(0) !== 0x89504e47) throw new Error(`invalid PNG screenshot: ${filePath}`);
  return [bytes.readUInt32BE(16), bytes.readUInt32BE(20)];
}

export function compareScreenshotSets(outputDir, referenceDir, { allowCanvasScale = false } = {}) {
  const output = fs.existsSync(outputDir) ? fs.readdirSync(outputDir).filter((name) => name.toLowerCase().endsWith(".png")).sort() : [];
  const reference = fs.existsSync(referenceDir) ? fs.readdirSync(referenceDir).filter((name) => name.toLowerCase().endsWith(".png")).sort() : [];
  if (!reference.length) throw new Error("delivery-review-screenshots-missing: approved review/png is required for delivery validation");
  if (output.length !== reference.length) throw new Error(`validation screenshot count ${output.length} != approved review count ${reference.length}`);
  const outputSize = pngDimensions(path.join(outputDir, output[0]));
  const referenceSize = pngDimensions(path.join(referenceDir, reference[0]));
  const outputRatio = outputSize[0] / outputSize[1];
  const referenceRatio = referenceSize[0] / referenceSize[1];
  if (allowCanvasScale ? Math.abs(outputRatio - referenceRatio) > 0.03 : (outputSize[0] !== referenceSize[0] || outputSize[1] !== referenceSize[1])) {
    throw new Error("validation screenshot canvas differs from approved review");
  }
  return { status: "pass", output_count: output.length, reference_count: reference.length, output_canvas: outputSize, reference_canvas: referenceSize, comparison: allowCanvasScale ? "aspect-and-count" : "canvas-and-count" };
}

function countPdfPages(filePath) {
  const text = fs.readFileSync(filePath).toString("latin1");
  return (text.match(/\/Type\s*\/Page(?!s)\b/g) || []).length;
}

function slideTitle(slide) {
  return String(slide?.action_title || slide?.title || `(${slide?.layout || "slide"})`).trim();
}

function normalizeTitle(value) {
  return String(value || "").replace(/\s+/g, "").trim();
}

export async function inspectHtml(filePath, { expectedSlides, screenshotDir = null, rootDir = ROOT } = {}) {
  const errors = [];
  const markup = validateHtmlMarkup(filePath, { rootDir });
  const browser = await chromium.launch(resolveChromiumLaunchOptions());
  try {
    const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
    page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
    page.on("console", (message) => { if (message.type() === "error") errors.push(`console: ${message.text()}`); });
    page.on("requestfailed", (request) => errors.push(`resource: ${request.url()} (${request.failure()?.errorText || "failed"})`));
    await page.goto(pathToFileURL(path.resolve(filePath)).href, { waitUntil: "networkidle" });
    await page.waitForFunction(() => window.Reveal?.isReady?.(), null, { timeout: 20000 });
    const result = await page.evaluate(() => ({
      slideCount: window.Reveal.getTotalSlides(),
      titles: [...document.querySelectorAll(".reveal .slides > section")].map((section) => {
        const heading = section.querySelector("h1,h2,h3,h4");
        return heading ? heading.textContent : "";
      }),
      texts: [...document.querySelectorAll(".reveal .slides > section")].map((section) => section.textContent || ""),
      notes: [...document.querySelectorAll(".reveal .slides > section")].map((section) => section.querySelector("aside.notes")?.textContent || ""),
      assets: [...document.querySelectorAll(".reveal .slides > section")].map((section) =>
        [...section.querySelectorAll("img")].map((image) => ({
          src: image.getAttribute("src") || "",
          alt: image.getAttribute("alt") || "",
        }))),
      duplicateIds: [...document.querySelectorAll("[id]")].map((node) => node.id).filter((id, index, all) => id && all.indexOf(id) !== index),
    }));
    if (result.duplicateIds.length) throw new Error(`delivery-html-duplicate-id: ${[...new Set(result.duplicateIds)].join(", ")}`);
    if (screenshotDir) {
      fs.mkdirSync(screenshotDir, { recursive: true });
      await page.evaluate(() => window.Reveal.configure({ controls: false, progress: false, slideNumber: false }));
      await page.addStyleTag({ content: ".reveal .controls, .reveal .progress, .reveal .slide-number { display: none !important; }" });
      for (let index = 0; index < result.slideCount; index += 1) {
        await page.evaluate((slide) => window.Reveal.slide(slide), index);
        await page.waitForTimeout(80);
        await page.screenshot({ path: path.join(screenshotDir, `slide-${String(index + 1).padStart(2, "0")}.png`) });
      }
      result.screenshots = fs.readdirSync(screenshotDir).filter((name) => /^slide-\d+\.png$/i.test(name)).sort();
    }
    if (errors.length) throw new Error(`HTML browser validation failed: ${errors.join("; ")}`);
    if (expectedSlides !== undefined && result.slideCount !== expectedSlides) throw new Error(`HTML slide count ${result.slideCount} != ${expectedSlides}`);
    return { ...result, html_validation: markup };
  } finally {
    await browser.close();
  }
}

export function inspectPdf(filePath, { expectedSlides, screenshotDir = null, rootDir = ROOT, runner = spawnSync } = {}) {
  const bytes = fs.readFileSync(filePath);
  if (bytes.length < 5 || bytes.subarray(0, 5).toString("ascii") !== "%PDF-") throw new Error("PDF has no valid signature");
  const raw = bytes.toString("latin1");
  if (/\/JavaScript\b|\/JS\b/.test(raw)) throw new Error("PDF contains executable JavaScript");
  const slideCount = countPdfPages(filePath);
  if (expectedSlides !== undefined && slideCount !== expectedSlides) throw new Error(`PDF page count ${slideCount} != ${expectedSlides}`);
  if (!slideCount) throw new Error("PDF contains no pages");
  const boxes = [...raw.matchAll(/\/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)/g)].map((match) => [Number(match[1]), Number(match[2])]);
  if (boxes.length && boxes.some(([width, height]) => Math.abs(width / height - 16 / 9) > 0.03)) throw new Error("PDF page size is not 16:9");
  const python = resolvePythonCommand({ rootDir });
  const extraction = runner(python.command, [...python.args, "-c", "import pymupdf as fitz,json,sys; d=fitz.open(sys.argv[1]); print(json.dumps({'texts':[p.get_text() for p in d]}, ensure_ascii=False))", filePath], { cwd: rootDir, encoding: "utf8", shell: false });
  if (extraction.error || extraction.status !== 0) {
    const detail = [extraction.stdout, extraction.stderr, extraction.error?.message].filter(Boolean).join("\n").trim();
    throw new Error(`PDF text extraction failed${detail ? `: ${detail}` : ""}`);
  }
  let extracted;
  try { extracted = JSON.parse(extraction.stdout); } catch (error) { throw new Error(`PDF text extraction returned invalid JSON: ${error.message}`); }
  if (!Array.isArray(extracted.texts) || extracted.texts.length !== slideCount || extracted.texts.some((text) => !String(text).trim())) throw new Error("PDF has an empty or unextractable page");
  let screenshots = null;
  if (screenshotDir) {
    fs.mkdirSync(screenshotDir, { recursive: true });
    const render = runner(python.command, [...python.args, "-c", "import pymupdf as fitz,sys; d=fitz.open(sys.argv[1]); out=sys.argv[2]; [p.get_pixmap(matrix=fitz.Matrix(1.5,1.5), alpha=False).save(__import__('os').path.join(out, f'page-{i+1:02d}.png')) for i,p in enumerate(d)]", filePath, screenshotDir], { cwd: rootDir, encoding: "utf8", shell: false });
    if (render.error || render.status !== 0) {
      const detail = [render.stdout, render.stderr, render.error?.message].filter(Boolean).join("\n").trim();
      throw new Error(`PDF page rendering failed${detail ? `: ${detail}` : ""}`);
    }
    screenshots = fs.readdirSync(screenshotDir).filter((name) => /^page-\d+\.png$/i.test(name)).sort();
    if (screenshots.length !== slideCount) throw new Error(`PDF rendered screenshot count ${screenshots.length} != ${slideCount}`);
  }
  return { slideCount, byteLength: bytes.length, pageSize: boxes[0] || null, texts: extracted.texts, screenshots };
}

export function inspectNotes(filePath, { expectedSlides } = {}) {
  const text = fs.readFileSync(filePath, "utf8");
  if (!text.trim()) throw new Error("speaker notes are empty");
  if (/\[(?:MISSING|UNVERIFIED)[^\]]*\]|\b(?:ckpt|checkpoint|audit|ledger|marker|sha[- ]?256|placeholder|todo|tbd)\b/i.test(text)) {
    throw new Error("speaker notes contain internal process, marker, or placeholder text");
  }
  const headings = text.match(/^## Slide\s+\d+\b.*$/gim) || [];
  if (expectedSlides !== undefined && headings.length !== expectedSlides) throw new Error(`speaker notes sections ${headings.length} != ${expectedSlides}`);
  const titles = headings.map((heading) => heading.replace(/^## Slide\s+\d+\s*[—-]?\s*/i, "").replace(/\s+_\([^)]*\)_\s*$/, "").trim());
  const sections = [...text.matchAll(/^## Slide\s+\d+\b.*$([\s\S]*?)(?=^## Slide\s+\d+\b|(?![\s\S]))/gim)];
  const spoken = sections.map((match) => String(match[1] || "").replace(/^\s+|\s+$/g, "").split(/\n\s*\*\*来源\*\*\s*\n/i, 1)[0].trim());
  const metadataTitle = text.match(/^# Speaker notes\s*[—-]\s*(.+)$/im)?.[1]?.trim() || null;
  return { slideCount: headings.length, titles, texts: sections.map((match) => match[0]), spoken, metadataTitle };
}

export function inspectPptx(filePath, { rootDir = ROOT, runner = spawnSync } = {}) {
  // Do not depend on a platform-specific `unzip` executable.  The bundled Python runtime is
  // already part of the scholar-slides toolchain and zipfile gives us a portable OPC inspection
  // without shell interpolation or a second external archive dependency.
  const python = resolvePythonCommand({ rootDir });
  const script = [
    "import html, json, re, sys, zipfile",
    "p = sys.argv[1]",
    "with zipfile.ZipFile(p) as z:",
    "    names = set(z.namelist())",
    "    slides = sorted((n for n in names if n.startswith('ppt/slides/slide') and n.endswith('.xml')), key=lambda n: int(re.search(r'slide(\\d+)\\.xml$', n).group(1)))",
    "    rels = [n for n in names if n.startswith('ppt/slides/_rels/') and n.endswith('.rels')]",
    "    external = []",
    "    slide_texts = []",
    "    slide_assets = []",
    "    has_table = False",
    "    slide_has_text = []",
    "    absolute_paths = []",
    "    for n in rels:",
    "        raw = z.read(n).decode('utf-8', 'replace')",
    "        if 'TargetMode=\"External\"' in raw: external.append(n)",
    "    for n in slides:",
    "        raw = z.read(n).decode('utf-8', 'replace')",
        "        slide_texts.append(' '.join(html.unescape(x) for x in re.findall(r'<a:t>(.*?)</a:t>', raw, re.S)))",
    "        slide_assets.append([html.unescape(x) for x in re.findall(r'descr=\"([^\"]+)\"', raw, re.S) if x.lower().endswith(('.png', '.jpg', '.jpeg', '.svg', '.gif'))])",
    "        slide_has_text.append('<a:t>' in raw)",
    "        if re.search(r'(?:(?<![A-Za-z])[A-Za-z]:[\\\\/]|/(?:home|mnt)/|file:)', raw): absolute_paths.append(n)",
    "        has_table = has_table or '<a:tbl' in raw",
    "    presentation = z.read('ppt/presentation.xml').decode('utf-8', 'replace') if 'ppt/presentation.xml' in names else ''",
    "    size = re.search(r'<p:sldSz cx=\"(\\d+)\" cy=\"(\\d+)\"', presentation)",
    "    wide = bool(size and abs(int(size.group(1)) / int(size.group(2)) - 16 / 9) < 0.03)",
    "    print(json.dumps({'slides': len(slides), 'has_presentation': 'ppt/presentation.xml' in names, 'external_relationships': external, 'has_vba': any(n.startswith('vbaProject') or n.endswith('.bin') for n in names), 'slide_texts': slide_texts, 'slide_assets': slide_assets, 'slide_has_text': slide_has_text, 'absolute_paths': absolute_paths, 'wide': wide, 'has_table': has_table}))",
  ].join("\n");
  const result = runner(python.command, [...python.args, "-c", script, filePath], { cwd: rootDir, encoding: "utf8", shell: false });
  if (result.error || result.status !== 0) {
    const detail = [result.stdout, result.stderr, result.error?.message].filter(Boolean).join("\n").trim();
    throw new Error(`cannot inspect PPTX structure${detail ? `: ${detail}` : ""}`);
  }
  let payload;
  try { payload = JSON.parse(result.stdout); } catch (error) { throw new Error(`PPTX structure inspection returned invalid JSON: ${error.message}`); }
  if (!payload.has_presentation || !payload.slides) throw new Error("PPTX is missing presentation.xml or slide parts");
  if (payload.external_relationships?.length) throw new Error(`PPTX contains external relationships: ${payload.external_relationships.join(", ")}`);
  if (payload.has_vba) throw new Error("PPTX contains a macro or unsupported binary project");
  if (!payload.wide) throw new Error("PPTX layout is not 16:9");
  if (payload.absolute_paths?.length) throw new Error(`PPTX contains absolute paths: ${payload.absolute_paths.join(", ")}`);
  if (payload.slide_has_text?.some((hasText) => !hasText)) throw new Error("PPTX contains a slide without editable text; whole-slide screenshot fallback is not allowed");
  return payload;
}

export function inspectPreparationMarkdown(filePath, { expectedSlides, expectedTitles = null, kind } = {}) {
  const text = fs.readFileSync(filePath, "utf8");
  if (!text.trim()) throw new Error(`${kind} preparation document is empty`);
  if (/(?:\baudit\b|\bcheckpoint\b|\bsha[- ]?256\b|\bjson\s+(?:path|pointer)\b|\bmarker\s+ledger\b|\[(?:MISSING|UNVERIFIED):)/i.test(text)) {
    throw new Error(`${kind} preparation document contains internal workflow language`);
  }
  let entries;
  if (kind === "summary" && Array.isArray(expectedTitles) && expectedTitles.length === expectedSlides) {
    const lines = [...text.matchAll(/^- Slide (\d+) — (.+)$/gm)];
    entries = lines.map((match, index) => {
      const expectedTitle = String(expectedTitles[index] || "").trim();
      if (!expectedTitle || !match[2].startsWith(`${expectedTitle}：`)) {
        throw new Error(`summary slide ${index + 1} does not begin with its complete deck title`);
      }
      return { index: Number(match[1]), title: expectedTitle };
    });
  } else {
    const pattern = kind === "script"
      ? /^## Slide (\d+) — (.+)$/gm
      : /^- Slide (\d+) — ([^：\n]+)：/gm;
    entries = [...text.matchAll(pattern)].map((match) => ({ index: Number(match[1]), title: match[2].trim() }));
  }
  if (entries.length !== expectedSlides) throw new Error(`${kind} slide count ${entries.length} != ${expectedSlides}`);
  entries.forEach((entry, index) => {
    if (entry.index !== index + 1) throw new Error(`${kind} slide order is not contiguous at ${entry.index}`);
  });
  return { slideCount: entries.length, titles: entries.map((entry) => entry.title), utf8: true, internal_content: false };
}

function countPptxSlides(filePath) {
  return inspectPptx(filePath).slides;
}

export function validateConsistency({ deck, artifacts }) {
  const expected = Array.isArray(deck?.slides) ? deck.slides.length : 0;
  const errors = [];
  const checks = [];
  const expectedTitles = (deck?.slides || []).map(slideTitle).map(normalizeTitle);
  const expectedSourceRefs = (deck?.slides || []).map((slide) => String(slide?.source_ref || "").trim());
  const sourceOptionalLayouts = new Set(["paper-title", "section", "outline-agenda", "bullets", "critique-concerns", "discussion-questions", "references"]);
  const expectedAssetIds = (deck?.slides || []).map((slide) => slide?.asset_selection?.candidate_id || slide?.figure?.id || null);
  const normalizeAsset = (value) => String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
  const tableValues = (slide) => {
    if (!slide?.table) return [];
    const values = [];
    if (Array.isArray(slide.table.columns)) values.push(...slide.table.columns.map((column) => column?.label ?? column));
    if (Array.isArray(slide.table.rows)) values.push(...slide.table.rows.flat());
    return values.map((value) => normalizeTitle(value)).filter(Boolean);
  };
  const expectedTableValues = (deck?.slides || []).map(tableValues);
  const expectedTitle = deck?.meta?.title ? normalizeTitle(deck.meta.title) : "";
  const expectedAuthors = Array.isArray(deck?.meta?.source?.paper_metadata?.authors)
    ? deck.meta.source.paper_metadata.authors.map((author) => normalizeTitle(author)).filter(Boolean)
    : [];
  const expectedVersion = normalizeTitle(deck?.meta?.source?.paper_metadata?.version?.resolved || "");
  const expectedSpeakerNotes = (deck?.slides || []).map((slide) => String(slide?.speaker_content?.text ?? slide?.speaker_notes ?? "").trim());
  const expectedVisibleTerms = (deck?.slides || []).map((slide) => {
    const terms = [slide?.action_title || slide?.title];
    if (["bullets", "discussion-questions"].includes(slide?.layout)) terms.push(...(slide?.points || []), ...(slide?.questions || []));
    if (slide?.layout === "references") terms.push(...(slide?.entries || []));
    if (slide?.layout === "assertion-evidence") terms.push(slide?.annotation, slide?.figure?.caption);
    if (slide?.layout === "results-table") {
      if (Array.isArray(slide?.table?.columns)) terms.push(...slide.table.columns.map((column) => column?.label ?? column));
      if (Array.isArray(slide?.table?.rows)) terms.push(...slide.table.rows.flat());
    }
    return terms.map((value) => normalizeTitle(value)).filter(Boolean);
  });
  for (const [format, info] of Object.entries(artifacts || {})) {
    const formatPrefix = `${format}`;
    if (info?.slideCount !== undefined && info.slideCount !== expected) errors.push(`${format} slide count ${info.slideCount} != ${expected}`);
    checks.push({ id: `${formatPrefix}-slide-count`, status: info?.slideCount === expected ? "pass" : "error", expected, actual: info?.slideCount ?? null });
    if (Array.isArray(info?.titles) && info.titles.length === expected) {
      const actualTitles = info.titles.map(normalizeTitle);
      expectedTitles.forEach((title, index) => {
        if (title && actualTitles[index] && title !== actualTitles[index]) {
          errors.push(`${format} slide ${index + 1} title mismatch`);
        }
      });
      checks.push({ id: `${formatPrefix}-titles`, status: "pass", count: actualTitles.length });
    }
    if (Array.isArray(info?.texts) && info.texts.length === expected) {
      expectedTitles.forEach((title, index) => {
        if (title && !normalizeTitle(info.texts[index]).includes(title)) errors.push(`${format} slide ${index + 1} title not present in editable text`);
      });
      expectedSourceRefs.forEach((sourceRef, index) => {
        const sourceOptional = format !== "notes" && sourceOptionalLayouts.has(deck?.slides?.[index]?.layout);
        const actualText = normalizeTitle(info.texts[index]);
        const segments = String(sourceRef || "").split(/[；;]/).map((value) => normalizeTitle(value)).filter(Boolean);
        const sourceMatches = segments.length ? segments.every((segment) => actualText.includes(segment)) : true;
        if (sourceRef && !sourceOptional && !sourceMatches) {
          errors.push(`${format} slide ${index + 1} source ref not present`);
        }
      });
      expectedTableValues.forEach((values, index) => {
        if (format === "notes") return;
        const actual = normalizeTitle(info.texts[index]);
        values.forEach((value) => {
          if (!actual.includes(value)) errors.push(`${format} slide ${index + 1} table value not present`);
        });
      });
      if (format === "notes") {
        if (expectedTitle && info.metadataTitle && normalizeTitle(info.metadataTitle) !== expectedTitle) errors.push(`${format} metadata title mismatch`);
      } else if (expectedTitle && !normalizeTitle(info.texts[0]).includes(expectedTitle)) errors.push(`${format} metadata title mismatch`);
      if (format !== "notes") {
        expectedAuthors.forEach((author) => {
          if (!normalizeTitle(info.texts[0]).includes(author)) errors.push(`${format} author metadata missing`);
        });
        if (expectedVersion && !normalizeTitle(info.texts[0]).includes(expectedVersion)) errors.push(`${format} version metadata missing`);
      }
      checks.push({ id: `${formatPrefix}-source-refs`, status: "pass", count: expectedSourceRefs.filter(Boolean).length });
      checks.push({ id: `${formatPrefix}-table-values`, status: "pass", slides: expectedTableValues.filter((values) => values.length).length });
      if (format !== "notes") {
        expectedVisibleTerms.forEach((terms, index) => terms.forEach((term) => {
          if (term && !normalizeTitle(info.texts[index]).includes(term)) errors.push(`${format} slide ${index + 1} approved visible content missing`);
        }));
        checks.push({ id: `${formatPrefix}-visible-content`, status: "pass", count: expectedVisibleTerms.flat().length });
      }
    }
    if (Array.isArray(info?.notes) && info.notes.length === expected) {
      expectedSpeakerNotes.forEach((note, index) => {
        if (note !== String(info.notes[index] || "").trim()) errors.push(`${format} slide ${index + 1} speaker notes drift`);
      });
      checks.push({ id: `${formatPrefix}-notes-fidelity`, status: "pass", count: expectedSpeakerNotes.length });
    }
    if (format === "notes" && Array.isArray(info?.spoken) && info.spoken.length === expected) {
      expectedSpeakerNotes.forEach((note, index) => {
        if (note !== String(info.spoken[index] || "").trim()) errors.push(`${format} slide ${index + 1} speaker notes drift`);
      });
      checks.push({ id: `${formatPrefix}-spoken-fidelity`, status: "pass", count: expectedSpeakerNotes.length });
    }
    if (Array.isArray(info?.assets) && info.assets.length === expected) {
      expectedAssetIds.forEach((assetId, index) => {
        if (!assetId) return;
        const tokens = (info.assets[index] || []).flatMap((asset) => [asset?.src, asset?.altText, asset?.alt]).map(normalizeAsset);
        if (!tokens.some((token) => token.includes(normalizeAsset(assetId)))) errors.push(`${format} slide ${index + 1} asset ${assetId} not present`);
      });
      checks.push({ id: `${formatPrefix}-assets`, status: "pass", slides: expectedAssetIds.filter(Boolean).length });
    }
    if (Array.isArray(info?.slide_assets) && info.slide_assets.length === expected) {
      expectedAssetIds.forEach((assetId, index) => {
        if (!assetId) return;
        const tokens = (info.slide_assets[index] || []).map(normalizeAsset);
        if (!tokens.some((token) => token.includes(normalizeAsset(assetId)))) errors.push(`${format} slide ${index + 1} asset ${assetId} not present`);
      });
      checks.push({ id: `${formatPrefix}-assets`, status: "pass", slides: expectedAssetIds.filter(Boolean).length });
    }
  }
  if (errors.length) throw new Error(`delivery consistency failed: ${errors.join("; ")}`);
  return {
    schema_version: 1,
    kind: "scholar-slides-delivery-consistency",
    status: "pass",
    summary: { errors: 0, warnings: 0, info: 0 },
    errors: [],
    checks,
    slide_count: expected,
    titles: expectedTitles,
    source_refs: (deck?.slides || []).map((slide) => slide?.source_ref || null),
    asset_ids: (deck?.slides || []).map((slide) => slide?.asset_selection?.candidate_id || slide?.figure?.id || null),
    metadata: {
      title: deck?.meta?.title || null,
      authors: deck?.meta?.source?.paper_metadata?.authors || [],
      version: deck?.meta?.source?.paper_metadata?.version?.resolved || null,
      language: deck?.meta?.language || null,
      theme: deck?.meta?.theme || null,
    },
    formats: Object.keys(artifacts || {}),
  };
}

function writeJson(filePath, payload) {
  fs.writeFileSync(filePath, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
}

export function writeDeliveryCheckpoint(projectDir, {
  sourceCheckpoint = "checkpoint-2.json",
  manifestPath = "delivery/delivery-manifest.json",
  validationPath = "delivery/delivery-validation.json",
  consistencyPath = "delivery/delivery-consistency.json",
} = {}) {
  const delivery = path.join(projectDir, "delivery");
  const source = typeof sourceCheckpoint === "string" && !path.isAbsolute(sourceCheckpoint)
    ? path.resolve(projectDir, sourceCheckpoint)
    : null;
  if (!source || path.basename(source) !== "checkpoint-2.json") throw new Error("delivery checkpoint requires a project-relative CKPT-2 source path");
  let sourcePayload;
  try { sourcePayload = JSON.parse(fs.readFileSync(source, "utf8")); } catch (error) { throw new Error(`delivery checkpoint source cannot be read: ${error.message}`); }
  if (sourcePayload?.checkpoint !== "CKPT-2" || !["approved", "confirmed"].includes(sourcePayload?.status)) {
    throw new Error("delivery checkpoint requires an approved or confirmed CKPT-2 source record");
  }
  const manifest = path.resolve(projectDir, manifestPath);
  const validation = path.resolve(projectDir, validationPath);
  const consistency = path.resolve(projectDir, consistencyPath);
  if (![manifest, validation, consistency].every((p) => fs.existsSync(p))) throw new Error("delivery checkpoint requires manifest, validation, and consistency reports");
  const manifestPayload = readJsonFile(manifest, "delivery manifest");
  const manifestIssues = manifestShapeIssues(manifestPayload);
  if (manifestIssues.length) throw new Error(`delivery checkpoint manifest validation failed: ${manifestIssues.join("; ")}`);
  const validationIssues = reportPassIssues(validation, "delivery validation report", 2);
  const consistencyIssues = reportPassIssues(consistency, "delivery consistency report", 1);
  if (validationIssues.length || consistencyIssues.length) {
    throw new Error(`delivery checkpoint reports are not passing: ${[...validationIssues, ...consistencyIssues].join("; ")}`);
  }
  const artifactIssues = validationArtifactIssues(projectDir, manifestPayload.validation_artifacts, manifestPayload.runtime_dependencies);
  if (artifactIssues.length) throw new Error(`delivery checkpoint validation artifacts are invalid: ${artifactIssues.join("; ")}`);
  const sourceRel = relativePosix(projectDir, source);
  const deliveryRel = (target) => {
    const rel = relativePosix(delivery, target);
    if (rel.startsWith("..")) throw new Error("delivery checkpoint reports must live inside delivery/");
    return rel;
  };
  const payload = {
    schema_version: 1,
    checkpoint: "DELIVERY",
    status: "ready",
    requires: { checkpoint: "CKPT-2", path: `../${sourceRel}`, sha256: sha256File(source) },
    manifest: { path: deliveryRel(manifest), sha256: sha256File(manifest) },
    validation: { path: deliveryRel(validation), sha256: sha256File(validation) },
    consistency: { path: deliveryRel(consistency), sha256: sha256File(consistency) },
    created_at: new Date().toISOString(),
  };
  writeJson(path.join(delivery, "checkpoint-delivery.json"), payload);
  return payload;
}

function readJsonFile(filePath, label) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch (error) {
    throw new Error(`${label} cannot be read: ${error.message}`);
  }
}

function manifestShapeIssues(manifest) {
  const issues = [];
  const safeDeliveryPath = (value) => {
    if (typeof value !== "string" || !value.startsWith("delivery/") || value.includes("\\")) return false;
    const relative = value.slice("delivery/".length);
    return Boolean(relative) && path.posix.normalize(relative) === relative && !relative.startsWith("../") && !relative.startsWith("/");
  };
  if (!manifest || typeof manifest !== "object") return ["delivery manifest is not an object"];
  if (manifest.schema_version !== 2) issues.push("delivery manifest schema is obsolete; re-export required");
  if (manifest.dependencies) issues.push("delivery manifest legacy dependencies field is not allowed; re-export required");
  if (manifest.status !== "pass") issues.push(`delivery manifest status ${manifest.status || "missing"}`);
  issues.push(...formalFormatIssues(manifest.formats));
  if (!manifest.files || typeof manifest.files !== "object") {
    issues.push("delivery manifest files object is missing");
  } else {
    for (const format of [...DELIVERY_FORMATS, ...DELIVERY_COMPANIONS]) {
      const output = manifest.files[format];
      if (!output || !safeDeliveryPath(output.path) || !/^[a-f0-9]{64}$/i.test(String(output.sha256 || ""))) {
        issues.push(`delivery manifest primary output ${format} is missing or malformed`);
      }
    }
  }
  if (!Array.isArray(manifest.runtime_dependencies)) {
    issues.push("delivery manifest runtime_dependencies must be an array");
  } else {
    const reasons = new Set(["html-stylesheet", "html-script", "css-font", "css-asset", "html-image"]);
    for (const dependency of manifest.runtime_dependencies) {
      if (!dependency || !safeDeliveryPath(dependency.path) || !/^[a-f0-9]{64}$/i.test(String(dependency.sha256 || "")) || !reasons.has(dependency.reason)) {
        issues.push(`delivery manifest runtime dependency is malformed: ${dependency?.path || "missing"}`);
      }
    }
  }
  const exporter = manifest.exporter;
  if (!exporter || typeof exporter !== "object" || !/^[a-f0-9]{64}$/i.test(String(exporter.implementation_sha256 || ""))) {
    issues.push("delivery manifest exporter implementation hash is missing or malformed");
  }
  if (!exporter || typeof exporter.implementation_files !== "object" || !Object.keys(exporter.implementation_files || {}).length) {
    issues.push("delivery manifest exporter implementation_files is missing or empty");
  } else {
    for (const [name, hash] of Object.entries(exporter.implementation_files)) {
      let safe = false;
      try {
        relativePosix(path.join(ROOT, "scripts"), path.resolve(path.join(ROOT, "scripts"), name));
        safe = true;
      } catch { /* reported below */ }
      if (!safe || path.isAbsolute(name) || name.includes("\\") || !/^[a-f0-9]{64}$/i.test(String(hash || ""))) {
        issues.push(`delivery manifest exporter implementation file is malformed: ${name}`);
      }
    }
  }
  const reports = manifest.reports;
  if (!reports || typeof reports !== "object") {
    issues.push("delivery manifest reports object is missing");
  } else {
    for (const reportName of ["consistency", "validation", "freeze"]) {
      const report = reports[reportName];
      if (!report || !safeDeliveryPath(report.path) || !/^[a-f0-9]{64}$/i.test(String(report.sha256 || ""))) {
        issues.push(`delivery manifest report ${reportName} is missing or malformed`);
      }
    }
  }
  const artifacts = manifest.validation_artifacts;
  if (!artifacts || typeof artifacts !== "object" || typeof artifacts.retained !== "boolean" || !Array.isArray(artifacts.paths)) {
    issues.push("delivery manifest validation_artifacts is malformed");
  } else if ((!artifacts.retained && artifacts.paths.length) || (artifacts.retained && !artifacts.paths.length)) {
    issues.push("delivery manifest validation_artifacts retained/paths mismatch");
  }
  return [...new Set(issues)];
}

function reportPassIssues(filePath, label, expectedSchema) {
  const issues = [];
  let report;
  try { report = readJsonFile(filePath, label); } catch (error) { return [error.message]; }
  if (expectedSchema !== undefined && report.schema_version !== expectedSchema) issues.push(`${label} schema is invalid`);
  if (report.status !== "pass") issues.push(`${label} is not passing`);
  if (Number(report.summary?.errors || 0) > 0 || (Array.isArray(report.errors) && report.errors.length)) issues.push(`${label} contains errors`);
  return issues;
}

function validationArtifactIssues(projectDir, artifacts, runtime = []) {
  const issues = [];
  if (!artifacts || typeof artifacts !== "object" || typeof artifacts.retained !== "boolean" || !Array.isArray(artifacts.paths)) {
    return ["validation_artifacts is malformed"];
  }
  if ((!artifacts.retained && artifacts.paths.length) || (artifacts.retained && !artifacts.paths.length)) {
    issues.push("validation_artifacts retained/paths mismatch");
  }
  for (const artifactPath of artifacts.paths) {
    if (typeof artifactPath !== "string" || path.isAbsolute(artifactPath)) {
      issues.push(`validation_artifacts path is not project-relative: ${artifactPath}`);
      continue;
    }
    try {
      const absolute = resolveProjectReference(projectDir, artifactPath, "validation_artifacts path");
      if (!fs.existsSync(absolute)) issues.push(`validation_artifacts path is missing: ${artifactPath}`);
      else assertNoSymlinkTree(absolute);
    } catch (error) { issues.push(error.message); }
    if (runtime.some((dependency) => dependency.path === artifactPath || dependency.path === `delivery/${artifactPath}`)) {
      issues.push(`delivery-validation-artifact-in-runtime: ${artifactPath}`);
    }
  }
  return issues;
}

function resolveProjectReference(projectDir, reference, label) {
  if (typeof reference !== "string" || path.isAbsolute(reference)) throw new Error(`${label} must be a project-relative path`);
  const absolute = path.resolve(projectDir, reference);
  relativePosix(projectDir, absolute);
  return absolute;
}

export function verifyDeliveryCheckpoint(projectDir, { markStale = true } = {}) {
  const project = path.resolve(projectDir);
  const delivery = path.join(project, "delivery");
  const checkpointPath = path.join(delivery, "checkpoint-delivery.json");
  const issues = [];
  let checkpoint;
  try {
    checkpoint = readJsonFile(checkpointPath, "delivery checkpoint");
  } catch (error) {
    return { status: "stale", issues: [error.message] };
  }
  const compare = (file, expected, label) => {
    if (!fs.existsSync(file)) {
      issues.push(`${label} is missing`);
      return;
    }
    if (expected && sha256File(file) !== expected) issues.push(`${label} hash mismatch`);
  };
  if (checkpoint.checkpoint !== "DELIVERY") issues.push("delivery checkpoint kind mismatch");
  if (!["ready", "stale"].includes(checkpoint.status)) issues.push(`delivery checkpoint status ${checkpoint.status || "missing"}`);
  let source;
  try {
    const sourceRef = String(checkpoint.requires?.path || "");
    source = resolveProjectReference(project, sourceRef.replace(/^\.\.\//, ""), "delivery checkpoint source");
  } catch (error) {
    issues.push(error.message);
  }
  if (source) compare(source, checkpoint.requires?.sha256, "approved CKPT-2");
  const reportFiles = {};
  for (const name of ["manifest", "validation", "consistency"]) {
    const ref = checkpoint[name];
    if (!ref?.path || path.isAbsolute(ref.path) || ref.path.startsWith("..")) {
      issues.push(`delivery checkpoint ${name} path is invalid`);
      continue;
    }
    const file = path.resolve(delivery, ref.path);
    try { relativePosix(delivery, file); } catch (error) { issues.push(`${name} path is outside delivery`); continue; }
    reportFiles[name] = file;
    compare(file, ref.sha256, `${name} report`);
  }
  let manifest = null;
  if (reportFiles.manifest && fs.existsSync(reportFiles.manifest)) {
    try { manifest = readJsonFile(reportFiles.manifest, "delivery manifest"); } catch (error) { issues.push(error.message); }
  }
  if (manifest) {
    issues.push(...manifestShapeIssues(manifest));
    issues.push(...validationArtifactIssues(project, manifest.validation_artifacts, manifest.runtime_dependencies));
    try {
      const sourceRef = resolveProjectReference(project, manifest.source_checkpoint?.path, "manifest source checkpoint");
      compare(sourceRef, manifest.source_checkpoint?.sha256, "manifest source checkpoint");
    } catch (error) { issues.push(error.message); }
    for (const [format, fileRef] of Object.entries(manifest.files || {})) {
      try {
        const file = resolveProjectReference(project, fileRef.path, `manifest ${format} output`);
        compare(file, fileRef.sha256, `manifest ${format} output`);
      } catch (error) { issues.push(error.message); }
    }
    for (const dependency of manifest.runtime_dependencies || []) {
      try {
        const file = resolveProjectReference(project, dependency.path, "manifest runtime dependency");
        compare(file, dependency.sha256, `manifest runtime dependency ${dependency.path}`);
      } catch (error) { issues.push(error.message); }
    }
    for (const report of Object.values(manifest.reports || {})) {
      try {
        const file = resolveProjectReference(project, report.path, "manifest report");
        compare(file, report.sha256, `manifest report ${report.path}`);
      } catch (error) { issues.push(error.message); }
    }
    for (const input of Object.values(manifest.inputs || {})) {
      try {
        const file = resolveProjectReference(project, input.path, "manifest input");
        compare(file, input.sha256, `manifest input ${input.path}`);
      } catch (error) { issues.push(error.message); }
    }
    const implementation = path.join(ROOT, "scripts", "delivery.mjs");
    if (manifest.exporter?.implementation_sha256 && sha256File(implementation) !== manifest.exporter.implementation_sha256) {
      issues.push("delivery exporter implementation changed");
    }
    for (const [name, expectedHash] of Object.entries(manifest.exporter?.implementation_files || {})) {
      const file = path.join(ROOT, "scripts", name);
      if (!fs.existsSync(file) || sha256File(file) !== expectedHash) issues.push(`delivery exporter file changed: ${name}`);
    }
    if (manifest.fonts?.cjk) {
      try {
        const deckPath = resolveProjectReference(project, manifest.inputs?.["deck.json"]?.path || "deck.json", "manifest deck input");
        const currentFont = enforceDeckCjkFont({ rootDir: ROOT, deckJsonPath: deckPath });
        if (currentFont !== manifest.fonts.cjk) issues.push("verified CJK font identity changed");
      } catch (error) { issues.push(`verified CJK font is no longer available: ${error.message}`); }
    }
  }
  for (const name of ["validation", "consistency"]) {
    const file = reportFiles[name];
    if (!file || !fs.existsSync(file)) continue;
    issues.push(...reportPassIssues(file, `${name} report`, name === "validation" ? 2 : 1));
  }
  const uniqueIssues = [...new Set(issues)];
  if (uniqueIssues.length && markStale) {
    const stale = { ...checkpoint, status: "stale", stale: { detected_at: new Date().toISOString(), issues: uniqueIssues } };
    writeJson(checkpointPath, stale);
    return { status: "stale", issues: uniqueIssues, checkpoint: stale };
  }
  return { status: uniqueIssues.length || checkpoint.status === "stale" ? "stale" : "ready", issues: uniqueIssues, checkpoint };
}

function copyChecked(src, dst) {
  fs.mkdirSync(path.dirname(dst), { recursive: true });
  fs.copyFileSync(src, dst);
  return dst;
}

function assertNoSymlinkTree(root) {
  const stat = fs.lstatSync(root);
  if (stat.isSymbolicLink()) throw new Error(`symlink/reparse path is not allowed in delivery inputs: ${root}`);
  if (!stat.isDirectory()) return;
  for (const entry of fs.readdirSync(root)) assertNoSymlinkTree(path.join(root, entry));
}

function listFiles(root, relative = "") {
  const current = path.join(root, relative);
  const entries = [];
  for (const name of fs.readdirSync(current).sort()) {
    const child = path.join(relative, name);
    const absolute = path.join(root, child);
    const stat = fs.lstatSync(absolute);
    if (stat.isSymbolicLink()) throw new Error(`symlink/reparse path is not allowed in delivery output: ${absolute}`);
    if (stat.isDirectory()) entries.push(...listFiles(root, child));
    else entries.push(absolute);
  }
  return entries;
}

function bundleRelativePath(reference) {
  if (typeof reference !== "string" || path.isAbsolute(reference) || !reference.startsWith("delivery/")) {
    throw new Error(`delivery-runtime-invalid-reference: manifest path ${reference || "missing"}`);
  }
  const relative = reference.slice("delivery/".length);
  if (!relative || relative === ".." || relative.startsWith("../")) throw new Error(`delivery-runtime-invalid-reference: manifest path ${reference}`);
  return relative;
}

function bundleFile(deliveryDir, reference) {
  const relative = bundleRelativePath(reference);
  const file = path.resolve(deliveryDir, relative);
  relativePosix(deliveryDir, file);
  return file;
}

export async function validateReleaseBundle({ projectDir, deliveryDir, manifest, validation, consistency } = {}) {
  const project = path.resolve(projectDir);
  const delivery = path.resolve(deliveryDir || path.join(project, "delivery"));
  const errors = [];
  errors.push(...manifestShapeIssues(manifest));
  errors.push(...validationArtifactIssues(project, manifest?.validation_artifacts, manifest?.runtime_dependencies));
  const checkFile = (reference, expected, label) => {
    try {
      const file = bundleFile(delivery, reference);
      if (!fs.existsSync(file)) errors.push(`${label} is missing`);
      else if (expected && sha256File(file) !== expected) errors.push(`${label} hash mismatch`);
      return file;
    } catch (error) { errors.push(error.message); return null; }
  };
  for (const [format, output] of Object.entries(manifest?.files || {})) checkFile(output.path, output.sha256, `manifest ${format} output`);
  const runtime = Array.isArray(manifest?.runtime_dependencies) ? manifest.runtime_dependencies : [];
  for (const dependency of runtime) {
    if (/validation(?:\/|\\)|(?:^|[\\/])png[\\/]/i.test(String(dependency.path || ""))) errors.push(`delivery-validation-artifact-in-runtime: ${dependency.path}`);
    checkFile(dependency.path, dependency.sha256, `runtime dependency ${dependency.path}`);
  }
  if (validation?.schema_version !== 2) errors.push("delivery validation report schema is invalid");
  if (validation?.status !== "pass") errors.push("delivery validation report is not passing");
  if (Number(validation?.summary?.errors || 0) > 0 || (Array.isArray(validation?.errors) && validation.errors.length)) errors.push("delivery validation report contains errors");
  if (consistency?.schema_version !== 1) errors.push("delivery consistency report schema is invalid");
  if (consistency?.status !== "pass") errors.push("delivery consistency report is not passing");
  if (Number(consistency?.summary?.errors || 0) > 0 || (Array.isArray(consistency?.errors) && consistency.errors.length)) errors.push("delivery consistency report contains errors");
  const htmlOutput = manifest?.files?.html?.path ? bundleFile(delivery, manifest.files.html.path) : null;
  let collected = { runtimeDependencies: [], paperAssets: [], htmlReferences: { images: [] } };
  if (htmlOutput && fs.existsSync(htmlOutput)) {
    try { collected = await collectHtmlRuntimeAssets(htmlOutput, { projectDir: project, stagedRoot: delivery, assetGraphPath: path.join(project, "asset-graph.json") }); }
    catch (error) { errors.push(error.message); }
  }
  const runtimePaths = runtime.map((item) => bundleRelativePath(item.path)).sort();
  const expectedRuntimePaths = [...collected.runtimeDependencies.map((item) => item.outputPath), ...collected.paperAssets.map((item) => item.outputPath)].sort();
  if (htmlOutput && runtimePaths.join("\n") !== expectedRuntimePaths.join("\n")) errors.push("delivery-runtime-overinclusive: manifest runtime dependency set differs from HTML resource graph");
  const finalFigures = fs.existsSync(path.join(delivery, "figures")) ? listFiles(path.join(delivery, "figures")).map((file) => relativePosix(delivery, file)).sort() : [];
  const referencedFigures = collected.paperAssets.map((item) => item.outputPath).sort();
  const manifestFigures = runtime.filter((item) => String(item.path || "").startsWith("delivery/figures/")).map((item) => bundleRelativePath(item.path)).sort();
  if (finalFigures.join("\n") !== referencedFigures.join("\n")) errors.push("delivery-runtime-unused-asset: delivery/figures is not the exact HTML image set");
  if (manifestFigures.join("\n") !== referencedFigures.join("\n")) errors.push("delivery-runtime-overinclusive: manifest html-image set differs from HTML image set");
  const artifacts = manifest?.validation_artifacts || { retained: false, paths: [] };
  if (!Array.isArray(artifacts.paths)) errors.push("validation_artifacts.paths must be an array");
  for (const artifactPath of artifacts.paths || []) {
    if (runtime.some((item) => item.path === artifactPath)) errors.push(`delivery-validation-artifact-in-runtime: ${artifactPath}`);
  }
  if (errors.length) throw new Error(`release bundle validation failed: ${[...new Set(errors)].join("; ")}`);
  return {
    status: "pass",
    findings: [],
    runtime_dependencies: runtimePaths,
    html_images: referencedFigures,
    validation_artifacts: artifacts,
  };
}

function freezeInputs(project, checkpoint) {
  const candidates = [
    "checkpoint-2.json", "deck.json", "digest.json", "asset-graph.json", "review/review-manifest.json",
    "review/ckpt2-readiness.json", "review/semantic-qa.json", "review/visual-qa.json",
    "review/aesthetics_report.json", "review/aesthetics-qa.json",
    "presentation-script.md", "presentation-summary.md",
  ];
  const inputs = {};
  for (const rel of candidates) {
    const file = path.join(project, rel);
    if (fs.existsSync(file) && fs.statSync(file).isFile()) inputs[rel] = { path: rel, sha256: sha256File(file) };
  }
  if (!inputs["checkpoint-2.json"] || !inputs["deck.json"]) throw new Error("cannot freeze Task9 inputs: checkpoint-2.json and deck.json are required");
  return { schema_version: 1, source_checkpoint: checkpoint.checkpoint, inputs };
}

function assertInputsUnchanged(project, frozen) {
  for (const entry of Object.values(frozen.inputs)) {
    const file = path.join(project, entry.path);
    if (!fs.existsSync(file) || sha256File(file) !== entry.sha256) throw new Error(`approved input changed during delivery: ${entry.path}`);
  }
}

function assertPortableHtml(filePath) {
  const html = fs.readFileSync(filePath, "utf8");
  if (/https?:\/\//i.test(html)) throw new Error("HTML delivery contains an external network URL");
  if (/(?:[A-Z]:[\\/]|\\\\\\\\|\/home\/|\/mnt\/)/i.test(html)) throw new Error("HTML delivery contains an absolute local path");
  if (/\b(?:TODO|TBD|placeholder|template)\b/i.test(html)) throw new Error("HTML delivery contains a placeholder");
  if (/(?:CKPT[- ]?\d|ready_for_human|review-manifest|semantic-qa|visual-qa|audit-ledger|internal-review)/i.test(html)) throw new Error("HTML delivery contains internal review chrome");
}

function assertCanonicalAesthetics(report, checkpoint, label) {
  if (!report || report.schema_version !== 1 || report.kind !== "scholar-slides-aesthetics-qa" || report.status !== "pass" || !Array.isArray(report.rework) || report.rework.length) throw new Error(`review report ${label} is not a passing canonical aesthetics contract`);
  if (report.inputs?.deck_sha256 !== checkpoint.artifact?.sha256 || report.inputs?.asset_graph_sha256 !== checkpoint.asset_graph?.sha256) throw new Error(`review report ${label} aesthetics inputs are stale`);
  if (!Array.isArray(report.slides) || !Array.isArray(report.weakest3)) throw new Error(`review report ${label} aesthetics evidence is malformed`);
  for (const slide of report.slides) {
    const dims = slide?.dimensions;
    const keys = ["hierarchy_focus", "typography", "space_grid", "figures_data_ink", "color_contrast", "consistency_finish"];
    if (!Number.isInteger(slide?.slide) || typeof slide?.has_figure_or_data !== "boolean" || !dims || Object.keys(dims).length !== 6 || !keys.every((key) => Object.hasOwn(dims, key))) throw new Error(`review report ${label} has malformed aesthetics slide evidence`);
    if ((slide.has_figure_or_data && !Number.isInteger(dims.figures_data_ink)) || (!slide.has_figure_or_data && dims.figures_data_ink !== null)) throw new Error(`review report ${label} has inconsistent figure/data evidence`);
    if (!keys.filter((key) => dims[key] !== null).every((key) => Number.isInteger(dims[key]) && dims[key] >= 0 && dims[key] <= 4)) throw new Error(`review report ${label} has invalid aesthetics scores`);
    const total = keys.reduce((sum, key) => sum + (dims[key] ?? 0), 0); const outOf = slide.has_figure_or_data ? 24 : 20;
    if (slide.total !== total || slide.outOf !== outOf) throw new Error(`review report ${label} has invalid aesthetics totals`);
  }
}

export function ensureCurrentReview(projectDir, checkpoint) {
  const readiness = path.join(projectDir, "review", "ckpt2-readiness.json");
  if (!fs.existsSync(readiness)) throw new Error("current CKPT-2 review readiness evidence is missing");
  let readinessPayload;
  try { readinessPayload = JSON.parse(fs.readFileSync(readiness, "utf8")); } catch (error) { throw new Error(`review readiness cannot be read: ${error.message}`); }
  if (readinessPayload.status !== "ready_for_human_approval") throw new Error("review readiness is not current and ready_for_human_approval");
  if (readinessPayload.automated_aesthetics_qa?.status !== "pass" || Number(readinessPayload.automated_aesthetics_qa?.rework_count ?? 1) !== 0) throw new Error("aesthetics review readiness is missing, stale, or has open rework");
  if (readinessPayload.deck_sha256 && readinessPayload.deck_sha256 !== checkpoint.artifact?.sha256) throw new Error("review readiness deck hash is stale");
  if (readinessPayload.asset_graph_sha256 && readinessPayload.asset_graph_sha256 !== checkpoint.asset_graph?.sha256) throw new Error("review readiness asset graph hash is stale");
  const manifestPath = path.join(projectDir, "review", "review-manifest.json");
  if (!fs.existsSync(manifestPath)) throw new Error("approved review manifest is missing");
  if (checkpoint.review_bundle?.sha256 && sha256File(manifestPath) !== checkpoint.review_bundle.sha256) throw new Error("approved CKPT-2 review bundle is stale");
  let manifest; try { manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8")); } catch (error) { throw new Error(`approved review manifest cannot be read: ${error.message}`); }
  if (manifest?.schema_version !== 1 || manifest?.kind !== "scholar-slides-review-manifest" || !Array.isArray(manifest.outputs)) throw new Error("approved review manifest is malformed");
  for (const reportName of ["semantic-qa.json", "visual-qa.json", "aesthetics_report.json", "aesthetics-qa.json"]) {
    const reportPath = path.join(projectDir, "review", reportName);
    if (!fs.existsSync(reportPath)) throw new Error(`current review report is missing: ${reportName}`);
    let report;
    try { report = JSON.parse(fs.readFileSync(reportPath, "utf8")); } catch (error) { throw new Error(`review report ${reportName} cannot be read: ${error.message}`); }
    const rawErrors = report.summary?.errors ?? report.errors ?? 0;
    const errors = Array.isArray(rawErrors) ? rawErrors.length : Number(rawErrors);
    if (report.status && report.status !== "pass") throw new Error(`review report ${reportName} is not passing`);
    if (errors > 0) throw new Error(`review report ${reportName} has ${errors} error(s)`);
    if (reportName.startsWith("aesthetics") && (!Array.isArray(report.rework) || report.rework.length > 0)) throw new Error(`review report ${reportName} has open aesthetics rework`);
    if (reportName.startsWith("aesthetics")) {
      const entry = manifest.outputs.find((item) => item?.path === `review/${reportName}`);
      if (!entry || entry.sha256 !== sha256File(reportPath)) throw new Error(`approved review manifest has missing or stale aesthetics evidence: ${reportName}`);
      assertCanonicalAesthetics(report, checkpoint, reportName);
    }
  }
  const bundlePath = checkpoint.review_bundle?.path;
  if (!bundlePath || !fs.existsSync(bundlePath)) throw new Error("approved CKPT-2 review bundle is missing");
  if (checkpoint.review_bundle.sha256 && sha256File(bundlePath) !== checkpoint.review_bundle.sha256) throw new Error("approved CKPT-2 review bundle is stale");
}

export async function createDelivery(projectDir, {
  formats = DELIVERY_FORMATS,
  resume = false,
  keepValidationArtifacts = false,
  runner = null,
  build = buildDeck,
  pdf = renderPdf,
  pptx = exportPptx,
  notes = writeSpeakerNotes,
} = {}) {
  const project = path.resolve(projectDir);
  const selected = Array.isArray(formats) ? parseFormats(formats.join(",")) : parseFormats(formats);
  const formatIssues = formalFormatIssues(selected);
  if (formatIssues.length) throw new Error(formatIssues.join("; "));
  const deckJson = path.join(project, "deck.json");
  const checkpointPath = path.join(project, "checkpoint-2.json");
  if (!fs.existsSync(deckJson)) throw new Error(`deck.json is missing: ${deckJson}`);
  const checkpoint = requireApprovedCheckpoint({ rootDir: ROOT, checkpointRecord: checkpointPath, artifactPath: deckJson, expectedCheckpoint: "CKPT-2" });
  ensureCurrentReview(project, checkpoint);
  const frozen = freezeInputs(project, checkpoint);
  const temp = path.join(project, `.delivery.tmp-${process.pid}-${crypto.randomBytes(4).toString("hex")}`);
  const validationTemp = path.join(project, `.delivery-validation.tmp-${process.pid}-${crypto.randomBytes(4).toString("hex")}`);
  const stagedDeck = path.join(temp, "deck");
  const final = path.join(project, "delivery");
  if (fs.existsSync(final) && fs.lstatSync(final).isSymbolicLink()) throw new Error("delivery output directory may not be a symlink/reparse point");
  const retainedValidationPath = path.join(project, "build", "delivery-validation");
  if (!keepValidationArtifacts && fs.existsSync(retainedValidationPath)) {
    assertNoSymlinkTree(retainedValidationPath);
    fs.rmSync(retainedValidationPath, { recursive: true, force: true });
  }
  if (resume) {
    for (const name of fs.readdirSync(project)) {
      if (name.startsWith(".delivery.tmp-") && name !== path.basename(temp)) {
        const stale = path.join(project, name);
        assertNoSymlinkTree(stale);
        fs.rmSync(stale, { recursive: true, force: true });
      }
    }
  }
  fs.mkdirSync(temp, { recursive: true });
  writeJson(path.join(temp, "export-inputs.json"), frozen);
  let previous = null;
  let swapped = false;
  let retainedValidation = null;
  try {
    const deck = JSON.parse(fs.readFileSync(deckJson, "utf8"));
    const built = build(deckJson, stagedDeck, { checkpointRecord: checkpointPath });
    const cjkFont = enforceDeckCjkFont({ rootDir: ROOT, deckJsonPath: deckJson });
    const htmlAssets = selected.includes("html")
      ? await collectHtmlRuntimeAssets(built.htmlPath, { projectDir: project, stagedRoot: stagedDeck, assetGraphPath: path.join(project, "asset-graph.json") })
      : { runtimeDependencies: [], paperAssets: [], htmlReferences: { images: [] } };
    for (const asset of [...htmlAssets.runtimeDependencies, ...htmlAssets.paperAssets]) {
      const destination = path.join(temp, asset.outputPath);
      relativePosix(temp, destination);
      copyChecked(asset.stagedPath, destination);
    }
    fs.mkdirSync(validationTemp, { recursive: true });
    const files = {};
    const info = {};
    if (selected.includes("html")) {
      files.html = copyChecked(built.htmlPath, path.join(temp, "slides.html"));
      assertPortableHtml(files.html);
      info.html = await inspectHtml(files.html, { expectedSlides: deck.slides.length, screenshotDir: path.join(validationTemp, "html", "png") });
      info.html.review_comparison = compareScreenshotSets(path.join(validationTemp, "html", "png"), path.join(project, "review", "png"));
    }
    if (selected.includes("pdf")) {
      files.pdf = await pdf(built.htmlPath, path.join(temp, "slides.pdf"), { cjkFont });
      info.pdf = inspectPdf(files.pdf, { expectedSlides: deck.slides.length, screenshotDir: path.join(validationTemp, "pdf", "png") });
      info.pdf.review_comparison = compareScreenshotSets(path.join(validationTemp, "pdf", "png"), path.join(project, "review", "png"), { allowCanvasScale: true });
    }
    if (selected.includes("pptx")) {
      const result = await pptx(deckJson, path.join(temp, "slides.pptx"), { checkpointRecord: checkpointPath, expectedCheckpoint: "CKPT-2", assetDir: path.join(temp, "pptx-assets") });
      files.pptx = result.out;
      normalizePptxArchive(files.pptx);
      const structure = inspectPptx(files.pptx);
      if (deck.slides.some((slide) => slide.layout === "results-table") && !structure.has_table) throw new Error("PPTX results-table slide is not a native editable table");
      info.pptx = {
        slideCount: structure.slides,
        texts: structure.slide_texts,
        slide_assets: structure.slide_assets,
        editable_text: structure.slide_has_text.every(Boolean),
        has_table: structure.has_table,
      };
    }
    if (selected.includes("notes")) {
      const result = notes(deckJson, null, { checkpointRecord: checkpointPath, expectedCheckpoint: "CKPT-2", outPath: path.join(temp, "speaker_notes.md") });
      files.notes = result.out;
      info.notes = inspectNotes(files.notes, { expectedSlides: deck.slides.length });
    }
    const scriptSource = path.join(project, "presentation-script.md");
    if (!fs.existsSync(scriptSource)) throw new Error("presentation-script.md is missing; regenerate the CKPT-2 candidate before delivery");
    files.script = copyChecked(scriptSource, path.join(temp, "presentation-script.md"));
    info.script = inspectPreparationMarkdown(files.script, { expectedSlides: deck.slides.length, kind: "script" });
    const summarySource = path.join(project, "presentation-summary.md");
    if (!fs.existsSync(summarySource)) throw new Error("presentation-summary.md is missing; regenerate the CKPT-2 candidate before delivery");
    files.summary = copyChecked(summarySource, path.join(temp, "presentation-summary.md"));
    info.summary = inspectPreparationMarkdown(files.summary, {
      expectedSlides: deck.slides.length,
      expectedTitles: deck.slides.map(slideTitle),
      kind: "summary",
    });
    if (fs.existsSync(stagedDeck)) {
      assertNoSymlinkTree(stagedDeck);
      fs.rmSync(stagedDeck, { recursive: true, force: true });
    }
    const consistency = validateConsistency({ deck, artifacts: info });
    writeJson(path.join(temp, "delivery-consistency.json"), consistency);
    assertInputsUnchanged(project, frozen);
    const manifestFiles = Object.fromEntries(Object.entries(files).map(([format, file]) => [format, { path: `delivery/${path.basename(file)}`, sha256: sha256File(file), size_bytes: fs.statSync(file).size }]));
    const runtimeDependencies = [...htmlAssets.runtimeDependencies, ...htmlAssets.paperAssets]
      .map((asset) => {
        const file = path.join(temp, asset.outputPath);
        return {
          path: `delivery/${asset.outputPath}`,
          sha256: sha256File(file),
          size_bytes: fs.statSync(file).size,
          reason: asset.reason,
          ...(asset.logical_id ? { logical_id: asset.logical_id } : {}),
        };
      })
      .sort((a, b) => a.path.localeCompare(b.path));
    const validationArtifacts = { retained: Boolean(keepValidationArtifacts), paths: [] };
    if (keepValidationArtifacts) {
      const retained = path.join(project, "build", "delivery-validation");
      fs.mkdirSync(path.dirname(retained), { recursive: true });
      if (fs.existsSync(retained)) { assertNoSymlinkTree(retained); fs.rmSync(retained, { recursive: true, force: true }); }
      fs.renameSync(validationTemp, retained);
      retainedValidation = retained;
      validationArtifacts.paths = [relativePosix(project, retained)];
    }
    const validation = {
      schema_version: 2,
      kind: "scholar-slides-delivery-validation",
      status: "pass",
      summary: { errors: 0, warnings: 0, info: 0 },
      errors: [],
      warnings: [],
      portability: "pass",
      consistency: "pass",
      inputs_frozen: true,
      validation_artifacts: validationArtifacts,
      checks: {
        html: { browser: "playwright-chromium", slide_count: info.html?.slideCount ?? null, screenshots: info.html?.screenshots?.length ?? 0, html_validation: info.html?.html_validation || null, review_comparison: info.html?.review_comparison || null },
        pdf: { signature: true, slide_count: info.pdf?.slideCount ?? null, page_size: info.pdf?.pageSize || null, screenshots: info.pdf?.screenshots?.length ?? 0, review_comparison: info.pdf?.review_comparison || null },
        pptx: { opc: true, slide_count: info.pptx?.slideCount ?? null, editable_text: info.pptx?.editable_text === true, native_table: info.pptx?.has_table === true, external_relationships: false, absolute_paths: false },
        notes: { slide_count: info.notes?.slideCount ?? null, utf8: true, internal_content: false },
        script: { slide_count: info.script?.slideCount ?? null, utf8: info.script?.utf8 === true, internal_content: info.script?.internal_content === true },
        summary: { slide_count: info.summary?.slideCount ?? null, utf8: info.summary?.utf8 === true, internal_content: info.summary?.internal_content === true },
      },
      formats: info,
    };
    writeJson(path.join(temp, "delivery-validation.json"), validation);
    const implementationFiles = [
      "delivery.mjs", "scholar_slides.py", "user_documents.py", "build_deck.mjs", "render_deck.mjs", "export_pptx.mjs", "speaker_notes.mjs", "font_preflight.py",
      ...fs.readdirSync(path.join(ROOT, "scripts", "lib"), { withFileTypes: true })
        .filter((entry) => entry.isFile() && entry.name.endsWith(".mjs"))
        .map((entry) => `lib/${entry.name}`)
        .sort(),
    ];
    const manifest = {
      schema_version: 2,
      kind: "scholar-slides-delivery-manifest",
      status: "pass",
      source_checkpoint: { path: "checkpoint-2.json", sha256: sha256File(checkpointPath), status: checkpoint.status },
      formats: selected,
      inputs: frozen.inputs,
      files: manifestFiles,
      runtime_dependencies: runtimeDependencies,
      validation_artifacts: validationArtifacts,
      reports: {
        consistency: { path: "delivery/delivery-consistency.json", sha256: sha256File(path.join(temp, "delivery-consistency.json")) },
        validation: { path: "delivery/delivery-validation.json", sha256: sha256File(path.join(temp, "delivery-validation.json")) },
        freeze: { path: "delivery/export-inputs.json", sha256: sha256File(path.join(temp, "export-inputs.json")) },
      },
      exporter: {
        name: "scholar-slides",
        version: RELEASE_VERSION,
        node: process.version,
        implementation_sha256: sha256File(path.join(ROOT, "scripts", "delivery.mjs")),
        implementation_files: Object.fromEntries(implementationFiles.map((name) => [name, sha256File(path.join(ROOT, "scripts", name))])),
      },
      theme: { name: deck.meta?.theme || null, language: deck.meta?.language || null },
      fonts: { cjk: cjkFont || null },
    };
    writeJson(path.join(temp, "delivery-manifest.json"), manifest);
    await validateReleaseBundle({ projectDir: project, deliveryDir: temp, manifest, validation, consistency });
    if (previous) fs.renameSync(final, previous);
    try {
      previous = fs.existsSync(final) ? `${final}.previous-${process.pid}` : null;
      if (previous) fs.renameSync(final, previous);
      fs.renameSync(temp, final);
      swapped = true;
      assertInputsUnchanged(project, frozen);
      const checkpointPayload = writeDeliveryCheckpoint(project);
      const verified = verifyDeliveryCheckpoint(project, { markStale: false });
      if (verified.status !== "ready") throw new Error(`delivery checkpoint verification failed: ${verified.issues.join("; ")}`);
      if (previous) fs.rmSync(previous, { recursive: true, force: true });
      if (!keepValidationArtifacts && fs.existsSync(validationTemp)) fs.rmSync(validationTemp, { recursive: true, force: true });
      return { delivery: final, checkpoint: checkpointPayload, manifest, validation, consistency };
    } catch (error) {
      if (swapped && fs.existsSync(final)) fs.rmSync(final, { recursive: true, force: true });
      if (previous) fs.renameSync(previous, final);
      throw error;
    }
  } catch (error) {
    if (!swapped && fs.existsSync(temp)) fs.rmSync(temp, { recursive: true, force: true });
    if (fs.existsSync(validationTemp)) fs.rmSync(validationTemp, { recursive: true, force: true });
    if (retainedValidation && fs.existsSync(retainedValidation)) fs.rmSync(retainedValidation, { recursive: true, force: true });
    throw error;
  }
}

if (isMain(import.meta.url, process.argv[1])) {
  const argv = process.argv.slice(2);
  const project = argv.shift();
  let formats = DELIVERY_FORMATS.join(",");
  let resume = false;
  let keepValidationArtifacts = false;
  let json = false;
  let verbose = false;
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--formats" && argv[i + 1]) { formats = argv[++i]; continue; }
    if (argv[i] === "--resume") { resume = true; continue; }
    if (argv[i] === "--keep-validation-artifacts") { keepValidationArtifacts = true; continue; }
    if (argv[i] === "--json") { json = true; continue; }
    if (argv[i] === "--verbose") { verbose = true; continue; }
    console.error(`ERROR: unsupported argument ${argv[i]}`); process.exit(1);
  }
  if (!project) { console.error("usage: delivery.mjs <project> [--formats html,pdf,pptx,notes] [--resume] [--keep-validation-artifacts] [--json] [--verbose]"); process.exit(1); }
  createDelivery(project, { formats, resume, keepValidationArtifacts }).then((result) => {
    if (json) console.log(JSON.stringify({ ok: true, ...result }));
    else console.log(`Task9 delivery ready -> ${result.delivery}`);
  }).catch((error) => {
    if (json) console.log(JSON.stringify({ ok: false, error: error.message }));
    else console.error(`ERROR: ${error.message}`);
    process.exit(1);
  });
}
