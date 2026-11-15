r"""Generates icon.png and icon.ico for GameShot (run: python make_icon.py)."""
import os
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))


def draw_icon(n=1024):
    """Screenshot crop-marks around a camera lens, on a blue gradient tile."""
    bg = Image.new("RGBA", (n, n))
    d = ImageDraw.Draw(bg)
    top, bottom = (52, 112, 255), (14, 22, 72)
    for y in range(n):
        t = y / (n - 1)
        d.line([(0, y), (n, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,))
    mask = Image.new("L", (n, n), 0)
    m = int(n * 0.02)
    ImageDraw.Draw(mask).rounded_rectangle([m, m, n - m, n - m], radius=int(n * 0.22), fill=255)
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    img.paste(bg, (0, 0), mask)
    d = ImageDraw.Draw(img)
    a, b = int(n * 0.20), int(n * 0.80)
    L, t = int(n * 0.21), int(n * 0.065)
    white = (245, 248, 255, 255)
    r = t // 2
    for rect in (
        (a, a, a + L, a + t), (a, a, a + t, a + L),
        (b - L, a, b, a + t), (b - t, a, b, a + L),
        (a, b - t, a + L, b), (a, b - L, a + t, b),
        (b - L, b - t, b, b), (b - t, b - L, b, b),
    ):
        d.rounded_rectangle(rect, radius=r, fill=white)
    c = n // 2
    for rad, col in ((0.19, (70, 225, 255, 255)), (0.12, (12, 20, 58, 255)), (0.075, (40, 120, 255, 255))):
        R = int(n * rad)
        d.ellipse([c - R, c - R, c + R, c + R], fill=col)
    R = int(n * 0.03)
    ox, oy = int(n * 0.03), int(n * 0.035)
    d.ellipse([c - ox - R, c - oy - R, c - ox + R, c - oy + R], fill=(255, 255, 255, 235))
    return img


if __name__ == "__main__":
    big = draw_icon(1024)
    big.resize((512, 512), Image.LANCZOS).save(os.path.join(HERE, "icon.png"))
    sizes = [16, 24, 32, 48, 64, 128, 256]
    frames = [big.resize((s, s), Image.LANCZOS) for s in sizes]
    frames[-1].save(os.path.join(HERE, "icon.ico"), format="ICO",
                    sizes=[(s, s) for s in sizes], append_images=frames[:-1])
    print("Wrote icon.png and icon.ico")
