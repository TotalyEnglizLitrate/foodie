"""Render QR codes as terminal block characters."""

from __future__ import annotations

import segno


def render_qr(data: str, border: int = 2) -> str:
    """Return ``data`` as a QR code drawn with half-block characters.

    Two module rows share one text row (▀ ▄ █), so the code stays roughly
    square. Dark modules are the filled glyphs; pair the output with a light
    background (black on white) so scanners see the usual polarity.
    """
    code = segno.make(data, error="m")
    size = len(code.matrix) + 2 * border
    grid = [[False] * size for _ in range(size)]
    for y, row in enumerate(code.matrix):
        for x, module in enumerate(row):
            grid[y + border][x + border] = bool(module)
    if size % 2:
        grid.append([False] * size)
    glyphs = {(False, False): " ", (True, False): "▀", (False, True): "▄", (True, True): "█"}
    return "\n".join(
        "".join(glyphs[(top, bottom)] for top, bottom in zip(grid[y], grid[y + 1]))
        for y in range(0, len(grid), 2)
    )
