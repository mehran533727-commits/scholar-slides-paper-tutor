// QA / integrity pure logic: extract claims from a deck spec, ground numbers against the
// source text, collect integrity flags, and parse a built deck's HTML for defects. The
// browser-bound checks live in verify_slides.mjs; these are deterministic and unit-tested.

import { rolesIn } from "./emphasis.mjs";

const SUP = { "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4", "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9" };

export function mapSuperscripts(s) {
  return String(s ?? "").replace(/[⁰¹²³⁴⁵⁶⁷⁸⁹]/g, (c) => SUP[c]);
}

export function numberTokens(s) {
  return mapSuperscripts(s).match(/\d+(?:\.\d+)?/g) || [];
}

// Flatten a deck spec into grounded text fields: {slide, layout, field, text}.
export function collectFields(deck) {
  const out = [];
  const add = (field, text, slide, layout) => {
    if (text != null && String(text).trim() !== "") out.push({ slide, layout, field, text: String(text) });
  };
  const visit = (value, field, slide, layout) => {
    if (typeof value === "string" || typeof value === "number") {
      add(field, value, slide, layout);
    } else if (Array.isArray(value)) {
      value.forEach((child, index) => visit(child, `${field}[${index}]`, slide, layout));
    } else if (value && typeof value === "object") {
      for (const [key, child] of Object.entries(value)) {
        if (key !== "layout") visit(child, field ? `${field}.${key}` : key, slide, layout);
      }
    }
  };
  visit(deck.meta || {}, "meta", 0, "meta");
  (deck.slides || []).forEach((slide, index) => {
    visit(slide, "", index + 1, slide.layout || "?");
  });
  return out;
}

const FLAG_RE = /\[(MISSING|UNVERIFIED|REDRAWN|GENERATED)\b[^\]]*\]/g;

export function collectFlags(deck) {
  const flags = [];
  for (const f of collectFields(deck)) {
    const m = f.text.match(FLAG_RE);
    if (m) m.forEach((flag) => flags.push({ slide: f.slide, layout: f.layout, field: f.field, flag }));
  }
  return flags;
}

// Every number on a content slide should appear in the source text (the paper). A number
// that does not is a likely fabrication. Table/equation numbers are P1; other content P2.
export function groundNumbers(deck, sourceText) {
  // Rejoin decimals the PDF split across whitespace ("34.\n1" -> "34.1") so a value that IS in
  // the paper still grounds; only fires when a decimal point sits right before the whitespace,
  // so unrelated tokens are not merged.
  const src = mapSuperscripts(sourceText).replace(/(\d+\.)\s+(\d+)/g, "$1$2");
  const findings = [];
  for (const f of collectFields(deck)) {
    if (f.slide === 0 || f.field === "figure.cite" || f.field.startsWith("entries[")) continue; // cites carry years; handled via flags
    const factual = f.field.startsWith("table") || f.layout === "results-table" || f.layout === "equation";
    for (const tok of numberTokens(f.text)) {
      if (tok.length < 2) continue; // skip lone digits (list counters, "N=6"-style single digits)
      if (!src.includes(tok)) {
        findings.push({ slide: f.slide, layout: f.layout, field: f.field, value: tok, severity: factual ? "P1" : "P2" });
      }
    }
  }
  return findings;
}

// Deck-level layout composition: bullet-slide ratio + longest same-layout run. In the
// figure-editor register a deck that is mostly pure-bullet slides is "telling, not showing";
// the aesthetics rubric flags > ~1/3 bullets, and 3+ identical layouts in a row reads monotonous.
export function layoutMix(deck) {
  const slides = (deck.slides || []);
  const counts = {};
  let maxRun = 0, run = 0, prev = null;
  for (const s of slides) {
    const l = s.layout || "?";
    counts[l] = (counts[l] || 0) + 1;
    run = l === prev ? run + 1 : 1;
    if (run > maxRun) maxRun = run;
    prev = l;
  }
  const bullets = counts.bullets || 0;
  return { slides: slides.length, bullets,
           bulletRatio: slides.length ? bullets / slides.length : 0, maxRun, counts };
}

// Inline-emphasis discipline: "designed, not decorated". `key` reuses the base accent, so it is
// free; the rule caps the ADDED colored roles (metric / pos / warn) at <=2 per slide. A slide over
// the cap reads as rainbow highlighting and is nudged (P3) in qa_report. `maxAdded` is the ceiling.
export function emphasisAudit(deck, { maxAdded = 2 } = {}) {
  const ADDED = new Set(["metric", "pos", "warn"]);
  const perSlide = new Map();
  for (const f of collectFields(deck)) {
    const roles = perSlide.get(f.slide) || new Set();
    for (const r of rolesIn(f.text)) roles.add(r);
    perSlide.set(f.slide, roles);
  }
  const slides = [...perSlide.entries()].map(([slide, roles]) => {
    const added = [...roles].filter((r) => ADDED.has(r));
    return { slide, roles: [...roles].sort(), added: added.sort() };
  });
  return { slides, over: slides.filter((s) => s.added.length > maxAdded).map((s) => s.slide), maxAdded };
}

// ---- emoji decoration (visual AI-slop tell; see aesthetics-review.md blocklist) ----
// Emoji as icons/bullets never belong in the figure-editor register — semantic emphasis roles
// and monoline structure carry meaning instead. \p{Extended_Pictographic} covers the emoji
// blocks without touching math (≤ ± ·), arrows (→), CJK, or integrity-flag text.
const EMOJI_RE = /\p{Extended_Pictographic}/gu;

export function emojiAudit(deck) {
  const out = [];
  for (const f of collectFields(deck)) {
    const chars = [...new Set(f.text.match(EMOJI_RE) || [])];
    if (chars.length) out.push({ slide: f.slide, layout: f.layout, field: f.field, chars });
  }
  return out;
}

// ---- figure crowding: the figure is the protagonist, text must not squeeze it ----
// On the fixed 1080px stage an assertion-evidence slide has ~600px left for the figure AFTER
// title, caption, and annotation. Every extra caption/annotation line costs the figure ~45px of
// height. Past this combined-length threshold the figure visibly shrinks below protagonist
// size — the detail belongs in speaker_notes instead. (Threshold is chars of stripped text;
// CJK says more per char, so CJK decks sit comfortably below it.)
export function figureCrowding(deck, { maxChars = 240 } = {}) {
  const strip = (s) => String(s ?? "").replace(/==(?:[kpw]\|)?(.+?)==/g, "$1");
  const out = [];
  (deck.slides || []).forEach((s, i) => {
    if (s.layout !== "assertion-evidence" || !s.figure) return;
    const chars = strip(s.figure.caption).length + strip(s.annotation).length;
    if (chars > maxChars) out.push({ slide: i + 1, chars, maxChars });
  });
  return out;
}

// ---- placeholder leakage (a Consistency "kill" in the aesthetics rubric) ----
// An unfilled template token like "<presenter>, <date>" shipping on a cover reads as a broken
// deck. The vocabulary is curated (not a generic <...> match) so real NLP tokens that belong in
// slides — <eos>, <pad>, <unk>, <s> — never false-positive.
const PLACEHOLDER_VOCAB = [
  "presenter", "presenters", "date", "name", "author", "authors", "venue", "affiliation",
  "institute", "email", "todo", "tbd", "placeholder", "your name",
  "主讲人", "日期", "姓名", "单位", "汇报人",
];
const PLACEHOLDER_RE = new RegExp(
  `&lt;\\s*(?:${PLACEHOLDER_VOCAB.join("|")})\\s*&gt;`, "gi"
);

export function findPlaceholders(html) {
  return [...String(html ?? "").matchAll(PLACEHOLDER_RE)].map((m) =>
    "<" + m[0].replace(/&lt;|&gt;/g, "").trim() + ">"
  );
}

// ---- static checks on a BUILT deck (deck.html string) ----
export function extractDataLayouts(html) {
  return [...html.matchAll(/data-layout="([^"]+)"/g)].map((m) => m[1]);
}
export function countKatexErrors(html) {
  return (html.match(/katex-error/g) || []).length;
}
export function extractImgSrcs(html) {
  return [...html.matchAll(/<img[^>]+src="([^"]+)"/g)].map((m) => m[1]);
}
// leftover "$...$" in rendered output => a math field that was placed where KaTeX didn't run.
export function findUnrenderedMath(html) {
  // strip out KaTeX/MathML spans first, then look for $...$ pairs in remaining text
  const stripped = html.replace(/<span class="katex[\s\S]*?<\/span><\/span>/g, "").replace(/<[^>]+>/g, " ");
  const m = stripped.match(/\$[^$\n]{1,80}\$/g);
  return m || [];
}
