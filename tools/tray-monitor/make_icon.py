"""
Generate crown_monitoring.ico — Crown Gas & Power monitoring tray app icon.

Design: CG&P logo (blue flame + red lightning bolt) on a navy circle,
        with a green ECG pulse line as the monitoring twist.
Produces a multi-resolution ICO (16 / 32 / 48 / 64 / 128 / 256 px).
"""

from PIL import Image, ImageDraw


NAVY    = (13,  31,  60)
NAVY_B  = (28,  60, 100)
BLUE_D  = (20,  80, 185)   # flame dark blue
BLUE_L  = (80, 155, 230)   # flame highlight
RED_D   = (195,  25,  25)  # bolt dark red
RED_L   = (230,  70,  50)  # bolt highlight
GREEN   = (76,  195,  80)  # monitoring pulse


def _icon(size: int) -> Image.Image:
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)

    def p(x: float, y: float) -> tuple[int, int]:
        return (round(x * s), round(y * s))

    # ── background circle ──────────────────────────────────────────────────
    m  = max(1, round(s * 0.03))
    bw = max(1, s // 30)
    d.ellipse([m, m, s - m, s - m], fill=NAVY, outline=NAVY_B, width=bw)

    # ── blue flame (teardrop, tip slightly right of centre) ────────────────
    flame = [
        p(0.54, 0.11),   # tip
        p(0.68, 0.24),   # upper right
        p(0.74, 0.38),   # right
        p(0.72, 0.52),   # lower right
        p(0.62, 0.62),   # bottom right curve
        p(0.50, 0.65),   # bottom centre
        p(0.38, 0.62),   # bottom left curve
        p(0.28, 0.52),   # lower left
        p(0.26, 0.38),   # left
        p(0.32, 0.24),   # upper left
    ]
    d.polygon(flame, fill=BLUE_D)

    # inner highlight — lighter blue, shifted slightly toward tip
    if s >= 32:
        inner = [
            p(0.54, 0.18),
            p(0.63, 0.28),
            p(0.67, 0.40),
            p(0.64, 0.51),
            p(0.54, 0.57),
            p(0.46, 0.57),
            p(0.36, 0.51),
            p(0.33, 0.40),
            p(0.37, 0.28),
            p(0.46, 0.18),
        ]
        d.polygon(inner, fill=BLUE_L)

    # ── red lightning bolt (pointing down, overlaps flame base) ───────────
    # Two-stroke zigzag bolt:  top-right → middle-left → bottom-right
    bolt = [
        p(0.62, 0.46),   # top-right start
        p(0.42, 0.63),   # middle-left notch (top stroke end)
        p(0.52, 0.63),   # notch corner (bridge)
        p(0.38, 0.84),   # bottom tip
        p(0.58, 0.67),   # bottom-left inner
        p(0.48, 0.67),   # bridge inner
        p(0.66, 0.52),   # back to top inner
    ]
    d.polygon(bolt, fill=RED_D)

    # bolt highlight
    if s >= 48:
        bolt_hi = [
            p(0.59, 0.48),
            p(0.45, 0.62),
            p(0.50, 0.62),
            p(0.41, 0.79),
            p(0.54, 0.67),
            p(0.49, 0.67),
            p(0.62, 0.53),
        ]
        d.polygon(bolt_hi, fill=RED_L)

    # ── green monitoring ECG pulse (bottom of circle) ──────────────────────
    if s >= 32:
        pulse = [
            p(0.10, 0.88),
            p(0.28, 0.88),
            p(0.36, 0.76),
            p(0.44, 0.96),
            p(0.52, 0.88),
            p(0.90, 0.88),
        ]
        lw = max(2, s // 20)
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
