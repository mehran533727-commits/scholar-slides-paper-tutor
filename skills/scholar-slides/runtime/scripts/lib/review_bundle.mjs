import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import zlib from "node:zlib";

const SCHEMA_VERSION = 1;
export const BUILD_BUNDLE_KIND = "scholar-slides-build-bundle";
export const REVIEW_BUNDLE_KIND = "scholar-slides-review-bundle";
export const REVIEW_RENDER_KIND = "scholar-slides-review-render";
export const REVIEW_SCREENSHOT_VIEWPORT = Object.freeze({ width: 1920, height: 1080 });

function canonicalFile(filePath, label) {
  if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
    throw new Error(`${label} does not exist or is not a regular file: ${filePath}`);
  }
  return fs.realpathSync(filePath);
}

function canonicalDirectory(dirPath, label) {
  if (!fs.existsSync(dirPath) || !fs.statSync(dirPath).isDirectory()) {
    throw new Error(`${label} does not exist or is not a directory: ${dirPath}`);
  }
  return fs.realpathSync(dirPath);
}

function isWithin(root, target) {
  const relative = path.relative(root, target);
  return relative === "" || (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative));
}

export function sha256File(filePath) {
  const digest = crypto.createHash("sha256");
  digest.update(fs.readFileSync(filePath));
  return digest.digest("hex");
}

export function fileEntry(filePath, label = "review bundle file") {
  const canonical = canonicalFile(filePath, label);
  return { path: canonical, sha256: sha256File(canonical) };
}

export function collectDirectoryFiles(directory, { exclude = new Set() } = {}) {
  const root = canonicalDirectory(directory, "review bundle directory");
  const files = [];
  const visit = (current) => {
    for (const child of fs.readdirSync(current, { withFileTypes: true })) {
      const source = path.join(current, child.name);
      if (child.isDirectory()) {
        visit(source);
      } else if (child.isFile() || child.isSymbolicLink()) {
        const canonical = canonicalFile(source, "review bundle file");
        if (!isWithin(root, canonical)) throw new Error(`review bundle symlink escapes its deck directory: ${source}`);
        if (!exclude.has(canonical)) files.push(canonical);
      }
    }
  };
  visit(root);
  return files.sort();
}

function crc32(buffer) {
  let crc = 0xffffffff;
  for (const byte of buffer) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

// A review image is evidence, not merely a filename ending in .png.  Verify the complete PNG
// container and decoded scanline size so a text file, truncated file, or fake extension cannot
// be sealed into CKPT-3 evidence.
export function assertReviewScreenshotPng(filePath, { width, height } = REVIEW_SCREENSHOT_VIEWPORT) {
  const image = fs.readFileSync(canonicalFile(filePath, "review screenshot"));
  const signature = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
  if (image.length < signature.length + 25 || !image.subarray(0, 8).equals(signature)) {
    throw new Error(`review screenshot is not a PNG: ${filePath}`);
  }
  let offset = signature.length;
  let ihdr;
  let ended = false;
  const idat = [];
  while (offset < image.length) {
    if (offset + 12 > image.length) throw new Error(`review screenshot is truncated: ${filePath}`);
    const length = image.readUInt32BE(offset);
    const type = image.subarray(offset + 4, offset + 8).toString("ascii");
    const start = offset + 8;
    const end = start + length;
    if (end + 4 > image.length) throw new Error(`review screenshot has an invalid chunk length: ${filePath}`);
    const data = image.subarray(start, end);
    const expectedCrc = image.readUInt32BE(end);
    if (crc32(image.subarray(offset + 4, end)) !== expectedCrc) {
      throw new Error(`review screenshot has a corrupt ${type} CRC: ${filePath}`);
    }
    if (!ihdr) {
      if (type !== "IHDR" || length !== 13) throw new Error(`review screenshot lacks a valid IHDR: ${filePath}`);
      ihdr = data;
    }
    if (type === "IDAT") idat.push(data);
    if (type === "IEND") {
      if (length !== 0 || end + 4 !== image.length) throw new Error(`review screenshot has invalid trailing data: ${filePath}`);
      ended = true;
      break;
    }
    offset = end + 4;
  }
  if (!ihdr || !ended || !idat.length) throw new Error(`review screenshot has incomplete PNG data: ${filePath}`);
  const actualWidth = ihdr.readUInt32BE(0);
  const actualHeight = ihdr.readUInt32BE(4);
  const bitDepth = ihdr[8];
  const colorType = ihdr[9];
  if (actualWidth !== width || actualHeight !== height || bitDepth !== 8 || ![2, 6].includes(colorType) || ihdr[10] !== 0 || ihdr[11] !== 0 || ihdr[12] !== 0) {
    throw new Error(`review screenshot must be a ${width}x${height} non-interlaced 8-bit RGB/RGBA PNG: ${filePath}`);
  }
  let decoded;
  try {
    decoded = zlib.inflateSync(Buffer.concat(idat));
  } catch (error) {
    throw new Error(`review screenshot PNG data cannot be decoded: ${filePath}: ${error.message}`);
  }
  const bytesPerPixel = colorType === 6 ? 4 : 3;
  const expectedBytes = actualHeight * (1 + actualWidth * bytesPerPixel);
  if (decoded.length !== expectedBytes) throw new Error(`review screenshot has invalid decoded dimensions: ${filePath}`);
  const scanline = 1 + actualWidth * bytesPerPixel;
  for (let offset = 0; offset < decoded.length; offset += scanline) {
    if (decoded[offset] > 4) throw new Error(`review screenshot has an invalid PNG filter byte: ${filePath}`);
  }
}

function validatedScreenshotEntries(screenshots, expectedSlides, screenshotsDir) {
  if (!Array.isArray(screenshots) || screenshots.length !== expectedSlides) {
    throw new Error(`review render evidence must cover all ${expectedSlides} deck slide(s)`);
  }
  const directory = canonicalDirectory(screenshotsDir, "review screenshots directory");
  return screenshots.map((entry, index) => {
    const current = fileEntry(entry?.path, "review screenshot");
    if (current.sha256 !== entry?.sha256) throw new Error(`review screenshot is stale: ${current.path}`);
    const expectedName = `slide-${String(index + 1).padStart(2, "0")}.png`;
    if (path.dirname(current.path) !== directory || path.basename(current.path) !== expectedName) {
      throw new Error(`review screenshot ${index + 1} must be ${path.join(directory, expectedName)}`);
    }
    assertReviewScreenshotPng(current.path);
    return current;
  });
}

export function writeReviewRenderEvidence({ deckJsonPath, deckDir, screenshotsDir, screenshots }) {
  const directory = canonicalDirectory(deckDir, "deck directory");
  const deck = fileEntry(deckJsonPath, "deck artifact");
  const html = canonicalFile(path.join(directory, "deck.html"), "built deck.html");
  const buildBundlePath = path.join(directory, ".scholar-slides-build.json");
  verifyReviewBundle({
    bundlePath: buildBundlePath,
    deckJsonPath,
    htmlPath: html,
    acceptedKinds: [BUILD_BUNDLE_KIND],
  });
  const parsedDeck = JSON.parse(fs.readFileSync(deck.path, "utf8"));
  const expectedSlides = Array.isArray(parsedDeck.slides) ? parsedDeck.slides.length : 0;
  const entries = screenshots.map((screenshot) => fileEntry(screenshot, "review screenshot"));
  const verifiedScreenshots = validatedScreenshotEntries(entries, expectedSlides, screenshotsDir);
  const target = path.join(directory, ".scholar-slides-review-render.json");
  fs.writeFileSync(target, `${JSON.stringify({
    schema_version: SCHEMA_VERSION,
    kind: REVIEW_RENDER_KIND,
    deck,
    interactive_html: html,
    build_bundle: fileEntry(buildBundlePath, "build bundle"),
    screenshots: verifiedScreenshots,
    viewport: REVIEW_SCREENSHOT_VIEWPORT,
  }, null, 2)}\n`);
  return target;
}

export function verifyReviewRenderEvidence({ evidencePath, deckJsonPath, htmlPath, screenshotsDir }) {
  const canonicalEvidence = canonicalFile(evidencePath, "review render evidence");
  let evidence;
  try {
    evidence = JSON.parse(fs.readFileSync(canonicalEvidence, "utf8"));
  } catch (error) {
    throw new Error(`review render evidence cannot be read: ${error.message}`);
  }
  if (!evidence || evidence.schema_version !== SCHEMA_VERSION || evidence.kind !== REVIEW_RENDER_KIND) {
    throw new Error("review render evidence has an unsupported schema or kind");
  }
  const deck = fileEntry(deckJsonPath, "checkpoint deck artifact");
  if (!evidence.deck || evidence.deck.path !== deck.path || evidence.deck.sha256 !== deck.sha256) {
    throw new Error("review render evidence deck does not match the approved checkpoint artifact");
  }
  const html = canonicalFile(htmlPath, "render HTML");
  if (evidence.interactive_html !== html) throw new Error("review render evidence is not bound to deck.html");
  if (!evidence.build_bundle?.path || !evidence.build_bundle?.sha256) throw new Error("review render evidence has no build bundle");
  const currentBuild = fileEntry(evidence.build_bundle.path, "review render build bundle");
  if (currentBuild.sha256 !== evidence.build_bundle.sha256) throw new Error("review render evidence build bundle is stale");
  verifyReviewBundle({
    bundlePath: currentBuild.path,
    deckJsonPath,
    htmlPath: html,
    acceptedKinds: [BUILD_BUNDLE_KIND],
  });
  const parsedDeck = JSON.parse(fs.readFileSync(deck.path, "utf8"));
  const expectedSlides = Array.isArray(parsedDeck.slides) ? parsedDeck.slides.length : 0;
  const expectedViewport = REVIEW_SCREENSHOT_VIEWPORT;
  if (!evidence.viewport || evidence.viewport.width !== expectedViewport.width || evidence.viewport.height !== expectedViewport.height) {
    throw new Error("review render evidence has an unexpected screenshot viewport");
  }
  const screenshots = validatedScreenshotEntries(evidence.screenshots, expectedSlides, screenshotsDir);
  return { evidence, evidencePath: canonicalEvidence, screenshots };
}

export function writeReviewBundle({ deckJsonPath, deckDir, outPath, kind = REVIEW_BUNDLE_KIND, evidence = undefined }) {
  const deck = fileEntry(deckJsonPath, "deck artifact");
  const directory = canonicalDirectory(deckDir, "deck directory");
  const html = canonicalFile(path.join(directory, "deck.html"), "built deck.html");
  const printHtml = canonicalFile(path.join(directory, "deck.print.html"), "built deck.print.html");
  const target = path.resolve(outPath || path.join(directory, ".scholar-slides-review.json"));
  const canonicalTarget = fs.existsSync(target) ? fs.realpathSync(target) : target;
  const files = collectDirectoryFiles(directory, { exclude: new Set([canonicalTarget]) }).map((file) => fileEntry(file));
  if (!files.some((file) => file.path === html)) throw new Error("review bundle is missing built deck.html");
  const bundle = {
    schema_version: SCHEMA_VERSION,
    kind,
    deck,
    interactive_html: html,
    print_html: printHtml,
    files,
  };
  if (evidence !== undefined) bundle.evidence = evidence;
  fs.writeFileSync(target, `${JSON.stringify(bundle, null, 2)}\n`);
  return target;
}

export function verifyReviewBundle({ bundlePath, deckJsonPath, htmlPath, acceptedKinds = [REVIEW_BUNDLE_KIND] }) {
  const canonicalBundle = canonicalFile(bundlePath, "review bundle");
  let bundle;
  try {
    bundle = JSON.parse(fs.readFileSync(canonicalBundle, "utf8"));
  } catch (error) {
    throw new Error(`review bundle cannot be read: ${error.message}`);
  }
  if (!bundle || bundle.schema_version !== SCHEMA_VERSION || !acceptedKinds.includes(bundle.kind)) {
    throw new Error("review bundle has an unsupported schema or kind");
  }
  const deck = fileEntry(deckJsonPath, "checkpoint deck artifact");
  if (!bundle.deck || bundle.deck.path !== deck.path || bundle.deck.sha256 !== deck.sha256) {
    throw new Error("review bundle deck does not match the approved checkpoint artifact");
  }
  const html = canonicalFile(htmlPath, "render HTML");
  if (bundle.interactive_html !== html) throw new Error("render HTML is not the reviewed deck.html path");
  const printHtml = canonicalFile(path.join(path.dirname(html), "deck.print.html"), "render print HTML");
  if (bundle.print_html !== printHtml) throw new Error("review bundle does not bind the deck.print.html used for PDF render");
  if (!Array.isArray(bundle.files) || !bundle.files.length) throw new Error("review bundle has no files");
  const seen = new Set();
  let foundHtml = false;
  let foundPrintHtml = false;
  for (const file of bundle.files) {
    if (!file || typeof file.path !== "string" || typeof file.sha256 !== "string") {
      throw new Error("review bundle contains an invalid file entry");
    }
    const current = fileEntry(file.path);
    if (seen.has(current.path)) throw new Error("review bundle contains duplicate file entries");
    seen.add(current.path);
    if (current.sha256 !== file.sha256) throw new Error(`review bundle is stale: SHA-256 changed (${current.path})`);
    foundHtml ||= current.path === html;
    foundPrintHtml ||= current.path === printHtml;
  }
  if (!foundHtml) throw new Error("review bundle does not include render HTML");
  if (!foundPrintHtml) throw new Error("review bundle does not include render print HTML");
  return { bundle, bundlePath: canonicalBundle };
}
