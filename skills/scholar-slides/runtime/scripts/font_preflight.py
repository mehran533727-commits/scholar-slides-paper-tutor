#!/usr/bin/env python3
"""Detect a locally installed CJK font with glyph coverage for Chinese deck output."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Callable, Iterable


PREFERRED_CJK_FONTS = (
    "Noto Sans CJK SC",
    "Noto Serif CJK SC",
    "Source Han Sans SC",
    "Source Han Serif SC",
    "Microsoft YaHei",
    "SimSun",
    # Common Linux fallback used by the supported WSL runtime. It is selected only after
    # platform-specific CJK families and still undergoes exact glyph-coverage verification.
    "WenQuanYi Zen Hei",
)
SYSTEM_CJK_FALLBACKS = (
    ("WenQuanYi Zen Hei", "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
    ("Unifont", "/usr/share/fonts/truetype/unifont/unifont_sample.ttf"),
)
SAMPLE_TEXT = "中文论文汇报"


def require_python_311() -> None:
    """Keep direct calls and Node-selected interpreters on the declared Python baseline."""
    import sys

    if sys.version_info < (3, 11):
        raise RuntimeError(f"Python 3.11+ is required; found {sys.version.split()[0]}")


def select_cjk_font(families: Iterable[str], supports: Callable[[str], bool]) -> str | None:
    """Choose the highest-priority installed family that covers the Chinese sample text."""
    installed = {str(family).casefold(): str(family) for family in families}
    for preferred in PREFERRED_CJK_FONTS:
        family = installed.get(preferred.casefold())
        if family and supports(family):
            return family
    return None


def apply_cjk_font(rc_params, family: str) -> None:
    """Put the verified CJK family ahead of Matplotlib's normal sans-serif fallbacks."""
    existing = list(rc_params.get("font.sans-serif", []))
    rc_params["font.sans-serif"] = [family, *[name for name in existing if name != family]]
    rc_params["axes.unicode_minus"] = False


def detect_cjk_font(text: str = SAMPLE_TEXT) -> str | None:
    try:
        from matplotlib import font_manager
        from matplotlib.ft2font import FT2Font
    except ImportError as exc:
        raise RuntimeError("matplotlib is required for CJK font preflight; run the installer first") from exc

    families = sorted({entry.name for entry in font_manager.fontManager.ttflist if entry.name})

    def supports(family: str) -> bool:
        try:
            font_path = font_manager.findfont(family, fallback_to_default=False)
            face = FT2Font(font_path)
            return all(face.get_char_index(ord(char)) for char in text)
        except (OSError, ValueError):
            return False

    selected = select_cjk_font(families, supports)
    if selected:
        return selected
    # Matplotlib's cached font list can lag behind fontconfig in a WSL image.  Probe the
    # well-known system fallback files directly so a verified, installed CJK family is not
    # mistaken for a missing font; exact glyph coverage remains mandatory.
    if os.environ.get("SCHOLAR_SLIDES_DISABLE_SYSTEM_CJK_FALLBACK") not in {"1", "true", "yes"}:
        for family, candidate in SYSTEM_CJK_FALLBACKS:
            if not Path(candidate).is_file():
                continue
            try:
                face = FT2Font(candidate)
                if all(face.get_char_index(ord(char)) for char in text):
                    return family
            except (OSError, ValueError):
                continue
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check CJK font coverage for Chinese scholarly decks.")
    parser.add_argument("--language", choices=("auto", "zh", "en"), default="auto")
    parser.add_argument("--require-cjk", action="store_true", help="exit non-zero when no usable CJK font is found")
    parser.add_argument("--json", action="store_true", help="emit a machine-readable result")
    parser.add_argument("--text", default=SAMPLE_TEXT, help="exact CJK text whose glyph coverage is required")
    args = parser.parse_args(argv)
    text = args.text or SAMPLE_TEXT

    try:
        require_python_311()
    except RuntimeError as exc:
        if args.json:
            print(json.dumps({"required": args.language != "en", "family": None, "covered_text": text, "error": str(exc)}, ensure_ascii=False))
        else:
            print(str(exc))
        return 2

    if args.language == "en":
        result = {"required": False, "family": None, "covered_text": text}
    else:
        try:
            family = detect_cjk_font(text)
        except RuntimeError as exc:
            if args.json:
                print(json.dumps({"required": True, "family": None, "covered_text": text, "error": str(exc)}, ensure_ascii=False))
            else:
                print(f"CJK font preflight unavailable: {exc}")
            return 2 if args.require_cjk else 0
        result = {"required": True, "family": family, "covered_text": text}

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    elif result["family"]:
        print(f"CJK font ready: {result['family']} (covers {text})")
    elif result["required"]:
        print("CJK font missing: install Noto Sans/Serif CJK, Source Han, Microsoft YaHei, or SimSun before final Chinese export.")
    else:
        print("CJK font preflight not required for an English-only deck.")
    return 2 if args.require_cjk and not result["family"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
