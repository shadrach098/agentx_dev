"""The agentx orb in the terminal: the app window's dot sphere drawn with braille dots, 6 characters by 3 lines
(12 by 12 dots), in the brand's lime. It turns slowly; while thinking its surface bubbles (dots push out past the
edge and settle back), and while a step runs two rings of dots light up and turn on their own axes. ``CUBE_MARK``
is the still cube (seen corner-on, a hexagon) shown when agentx is waiting for you.
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import List, Sequence, Tuple

COLS, ROWS = 6, 3                       # characters
WIDE, TALL = COLS * 2, ROWS * 4         # braille dots
# the bit of each dot in a braille character, by (column, row) inside the character
_BITS = {(0, 0): 0x01, (0, 1): 0x02, (0, 2): 0x04, (0, 3): 0x40, (1, 0): 0x08, (1, 1): 0x10, (1, 2): 0x20, (1, 3): 0x80}
CUBE_MARK = "⬢"                      # one character: the hexagon a cube makes seen corner-on


@lru_cache(maxsize=None)
def _dots(rings: int = 7) -> Tuple[Tuple[float, float, float, int], ...]:
    """Dots on a sphere in rings, like lines of latitude: (x, y, z, ring)."""
    out = []
    for i in range(rings):
        lat = -math.pi / 2 + (i + 0.5) * math.pi / rings
        r, y = math.cos(lat), math.sin(lat)
        n = max(4, round(rings * 2 * r))
        for j in range(n):
            lon = j / n * 2 * math.pi + (i % 2) * math.pi / n
            out.append((r * math.cos(lon), y, r * math.sin(lon), i))
    return tuple(out)


def _rot(x: float, y: float, z: float, axis: Sequence[float], a: float) -> Tuple[float, float, float]:
    ax, ay, az = axis
    c, s = math.cos(a), math.sin(a)
    d = (ax * x + ay * y + az * z) * (1 - c)
    return (x * c + (ay * z - az * y) * s + ax * d, y * c + (az * x - ax * z) * s + ay * d,
            z * c + (ax * y - ay * x) * s + az * d)


def _bubble(x: float, y: float, z: float, t: float) -> float:
    n = (math.sin(5.3 * x + 1.9 * t) * math.sin(4.7 * y - 1.4 * t + 1.3) +
         0.7 * math.sin(6.1 * (x - z) + 2.3 * t + 2.1) * math.sin(5.6 * y + 1.7 * t))
    v = max(0.0, n / 1.3)
    return v * v


RINGS = ((2, (1.0, 0.0, 0.0), 1.6), (4, (0.5, 0.0, 0.87), -2.1))   # (ring, axis, speed): the rings that turn


def frame(state: str, t: float) -> List[List[Tuple[str, bool]]]:
    """The orb at time ``t`` (seconds) as ``ROWS`` lines of ``(character, lit)`` cells; ``lit`` cells hold a dot
    that is lifted (thinking) or in a turning ring (working) and are drawn brighter."""
    grid = [[0] * COLS for _ in range(ROWS)]
    lit = [[False] * COLS for _ in range(ROWS)]
    spin = t * (0.9 if state == "working" else 0.6)
    tilt = -0.35
    rings = {ring: (axis, speed) for ring, axis, speed in RINGS} if state == "working" else {}
    for x, y, z, ring in _dots():
        bright = False
        if ring in rings:
            axis, speed = rings[ring]
            x, y, z = _rot(x, y, z, axis, t * speed)
            bright = True
        scale = 1.0
        if state == "thinking":
            bump = min(1.0, _bubble(x, y, z, t) * 1.6)
            scale += 0.22 * bump
            bright = bump > 0.35
        x, y, z = _rot(x * scale, y * scale, z * scale, (0.0, 1.0, 0.0), spin)
        x, y, z = _rot(x, y, z, (1.0, 0.0, 0.0), tilt)
        if z < -0.15 and not bright:                   # the far side stays out: the dots read as a surface
            continue
        col = round((x * 0.86 + 1) / 2 * (WIDE - 1))
        row = round((1 - (y * 0.86 + 1) / 2) * (TALL - 1))
        if not (0 <= col < WIDE and 0 <= row < TALL):
            continue
        cell_row, cell_col = row // 4, col // 2
        grid[cell_row][cell_col] |= _BITS[(col % 2, row % 4)]
        if bright:
            lit[cell_row][cell_col] = True
    return [[(chr(0x2800 + grid[r][c]), lit[r][c]) for c in range(COLS)] for r in range(ROWS)]


def frame_text(state: str, t: float) -> List[str]:
    """The orb as plain lines (no colours), for tests and logs."""
    return ["".join(ch for ch, _ in line) for line in frame(state, t)]
