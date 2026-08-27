"""Tesseract layout-block classification helpers shared between
cropped_files_2/route_translate.py and scripts/hash_and_embed_report_true.py.

Pulled out on its own (2026-08-27) so hash_and_embed_report_true.py -- which
dashboard/dedup.py imports for the upload-dedup embedding path -- doesn't
drag cropped_files_2/ (and transitively cropped_files/, ~1.1GB combined of
dev-experiment scratch output) into the production Docker image just to
reach these 5 names.
"""
from __future__ import annotations

from tesserocr import PT

IMAGE_BLOCK_TYPES = {PT.FLOWING_IMAGE, PT.HEADING_IMAGE, PT.PULLOUT_IMAGE}
TABLE_BLOCK_TYPES = {PT.TABLE}
IGNORE_BLOCK_TYPES = {PT.HORZ_LINE, PT.VERT_LINE, PT.NOISE}
MIN_BLOCK_DIM_PX = 10


def _drop_blocks_nested_in_image(
    blocks: list[tuple[tuple[int, int, int, int], int]], containment_threshold: float = 0.85,
) -> list[tuple[tuple[int, int, int, int], int]]:
    """
    Tesseract's layout engine can return a giant near-full-page block
    alongside smaller sub-blocks that sit entirely inside it (observed on
    cover pages with a photo/graphic background: the whole panel comes back
    as one PULLOUT_IMAGE, while the title/caption text within it also gets
    its own smaller block). Since image-type blocks now get OCR'd too (see
    build_transcript_and_images), keeping both means the same text is
    transcribed twice. Drop the smaller block when it's mostly contained
    inside a larger block that was classified as an image.
    """
    def area(b: tuple[int, int, int, int]) -> int:
        x1, y1, x2, y2 = b
        return max(0, x2 - x1) * max(0, y2 - y1)

    def containment_ratio(inner: tuple[int, int, int, int], outer: tuple[int, int, int, int]) -> float:
        ix1, iy1, ix2, iy2 = inner
        ox1, oy1, ox2, oy2 = outer
        x1, y1 = max(ix1, ox1), max(iy1, oy1)
        x2, y2 = min(ix2, ox2), min(iy2, oy2)
        if x2 <= x1 or y2 <= y1:
            return 0.0
        inter_area = (x2 - x1) * (y2 - y1)
        inner_area = area(inner)
        return inter_area / inner_area if inner_area else 0.0

    kept = []
    for i, (bbox, btype) in enumerate(blocks):
        nested_in_image = any(
            j != i and obtype == "image" and area(obbox) > area(bbox)
            and containment_ratio(bbox, obbox) >= containment_threshold
            for j, (obbox, obtype) in enumerate(blocks)
        )
        if not nested_in_image:
            kept.append((bbox, btype))
    return kept
