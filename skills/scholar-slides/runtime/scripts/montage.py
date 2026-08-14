#!/usr/bin/env python3
"""Build a compact, deterministic montage from completed review screenshots."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw


BACKGROUND = "#15171b"
LABEL_HEIGHT = 24


class MontageError(RuntimeError):
    """Review screenshots cannot safely form a montage."""


def build_montage(
    screenshots: Iterable[Path],
    output_path: Path,
    *,
    columns: int = 4,
    thumbnail_width: int = 320,
    gap: int = 16,
) -> dict[str, int | str]:
    """Compose already-rendered slides without cropping or re-rendering them."""
    slides = [Path(path) for path in screenshots]
    if not slides:
        raise MontageError("cannot create a montage without slide PNGs")
    if columns < 1 or thumbnail_width < 1 or gap < 0:
        raise MontageError("montage dimensions must be positive")
    rendered: list[Image.Image] = []
    try:
        for slide in slides:
            with Image.open(slide) as source:
                source.verify()
            with Image.open(slide) as source:
                image = source.convert("RGB")
                width, height = image.size
                if width < 1 or height < 1:
                    raise MontageError(f"review slide has invalid dimensions: {slide}")
                target_height = round(thumbnail_width * height / width)
                rendered.append(image.resize((thumbnail_width, target_height), Image.Resampling.LANCZOS))
    except (OSError, ValueError) as exc:
        raise MontageError(f"review slide PNG cannot be read: {exc}") from exc
    heights = {image.height for image in rendered}
    if len(heights) != 1:
        raise MontageError("review slide PNGs do not have a consistent aspect ratio")
    thumbnail_height = heights.pop()
    rows = (len(rendered) + columns - 1) // columns
    width = columns * thumbnail_width + (columns + 1) * gap
    cell_height = thumbnail_height + LABEL_HEIGHT
    height = rows * cell_height + (rows + 1) * gap
    montage = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(montage)
    for index, image in enumerate(rendered):
        row, column = divmod(index, columns)
        x = gap + column * (thumbnail_width + gap)
        y = gap + row * (cell_height + gap)
        montage.paste(image, (x, y))
        label = str(index + 1)
        label_box = draw.textbbox((0, 0), label)
        label_x = x + (thumbnail_width - (label_box[2] - label_box[0])) // 2
        draw.text((label_x, y + thumbnail_height + 5), label, fill="#f7f8fa")
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    try:
        montage.save(temporary, format="PNG")
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "columns": columns,
        "rows": rows,
        "thumbnail_width": thumbnail_width,
        "gap": gap,
        "background": BACKGROUND,
    }
