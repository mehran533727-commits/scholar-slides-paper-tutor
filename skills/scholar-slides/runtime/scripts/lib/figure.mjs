// Reused source figure: the real bbox-cropped image + caption + mandatory provenance cite.
// fit defaults to "contain" — a scientific figure is NEVER cropped/cover-fit.
import { escapeHtml } from "./escape.mjs";
import { probeImageSize } from "./img_size.mjs";
import { normalizeResourceUri, resolveResourcePath } from "./resource_path.mjs";

// A banner-shaped crop (aspect >= WIDE_AR) starved by the height-driven default gets
// width-driven sizing instead: the build probes the PNG and hands the ratio to CSS
// (--fig-ar), which sizes the image to the wide-figure width budget (--fig-wide-w).
const WIDE_AR = 2.2;

export function renderFigure(f, ctx) {
  if (!f || !f.src) throw new Error("renderFigure: figure needs a src");
  const src = normalizeResourceUri(f.src);
  const cite = f.cite ? ` <span class="cite">[${escapeHtml(f.cite)}]</span>` : "";
  const caption = f.caption ? `<figcaption>${escapeHtml(f.caption)}${cite}</figcaption>` : "";
  const alt = escapeHtml(f.alt || f.caption || "figure");
  const fit = f.fit === "cover" ? "cover" : "contain";
  let wide = "";
  const style = [];
  if (ctx?.baseDir) {
    const size = probeImageSize(resolveResourcePath(ctx.baseDir, src));
    const ar = size ? size.w / size.h : 0;
    if (ar >= WIDE_AR) {
      wide = ` class="wide"`;
      style.push(`--fig-ar:${ar.toFixed(2)}`);
    }
  }
  style.push(`object-fit:${fit}`);
  return (
    `<figure class="s-figure">` +
    `<img src="${escapeHtml(src)}" alt="${alt}"${wide} style="${style.join("; ")}">` +
    `${caption}</figure>`
  );
}
