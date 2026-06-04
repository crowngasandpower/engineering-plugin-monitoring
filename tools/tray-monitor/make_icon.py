"""
Generate crown_monitoring.ico — Crown Gas & Power monitoring tray app icon.

Design: dark navy circle, gold crown (3 peaks), green ECG pulse line.
Produces a multi-resolution ICO (16 / 32 / 48 / 64 / 128 / 256 px).
"""

from PIL import Image, ImageDraw


NAVY   = (13,  31,  60)
NAVY_B = (28,  60, 100)
GOLD   = (210, 155,  20)
GOLD_L = (245, 200,  55)
GREEN  = (76,  195,  80)
WHITE  = (255, 255, 255)


def _icon(size: int) -> Image.Image:
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)

    def p(x: float, y: float) -> tuple[int, int]:
        return (round(x * s), round(y * s))

    # ── background circle ──────────────────────────────────────────────────
    m = max(1, round(s * 0.03))
    bw = max(1, s // 28)
    d.ellipse([m, m, s - m, s - m], fill=NAVY, outline=NAVY_B, width=bw)

    # ── crown ──────────────────────────────────────────────────────────────
    # Three-peaked crown polygon
    crown = [
        p(0.13, 0.70),   # bottom-left
        p(0.13, 0.44),   # left wall
        p(0.27, 0.26),   # left peak
        p(0.38, 0.43),   # valley
        p(0.50, 0.16),   # centre peak
        p(0.62, 0.43),   # valley
        p(0.73, 0.26),   # right peak
        p(0.87, 0.44),   # right wall
        p(0.87, 0.70),   # bottom-right
    ]
    ow = max(1, s // 22)
    d.polygon(crown, fill=GOLD, outline=GOLD_L, width=ow)

    # Base band beneath crown
    bx0, by0 = p(0.13, 0.70)
    bx1, by1 = p(0.87, 0.80)
    d.rectangle([bx0, by0, bx1, by1], fill=GOLD, outline=GOLD_L, width=ow)

    # Jewel dots on crown (omit at tiny sizes)
    if s >= 48:
        r = max(2, s // 20)
        for cx, cy in [(0.27, 0.30), (0.50, 0.22), (0.73, 0.30)]:
            x, y = p(cx, cy)
            d.ellipse([x - r, y - r, x + r, y + r], fill=WHITE)

    # ── monitoring pulse line ──────────────────────────────────────────────
    # ECG-style heartbeat cutting across the crown base
    pulse = [
        p(0.10, 0.75),
        p(0.30, 0.75),
        p(0.38, 0.62),   # up
        p(0.46, 0.84),   # down
        p(0.54, 0.75),   # back
        p(0.90, 0.75),
    ]
    lw = max(2, s // 18)
    for i in range(len(pulse) - 1):
        d.line([pulse[i], pulse[i + 1]], fill=GREEN, width=lw)

    return img


def main() -> None:
    sizes  = [256, 128, 64, 48, 32, 16]
    images = [_icon(s) for s in sizes]
    out    = "crown_monitoring.ico"
    images[0].save(
        out,
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=images[1:],
    )
    print(f"Written {out}  ({', '.join(str(s) for s in sizes)} px)")


if __name__ == "__main__":
    main()
