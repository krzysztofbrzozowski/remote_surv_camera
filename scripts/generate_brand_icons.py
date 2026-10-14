"""Regenerate checked-in PNG/ICO assets from favicon.svg (requires Pillow).

The logo consists of rounded rectangles. Draw at 8x resolution before
downsampling to keep the signal mark clear at small tab-icon sizes.
"""
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from PIL import Image, ImageDraw


ICONS = Path(__file__).resolve().parents[1] / "server/surv_server/static/icons"
SVG = ET.parse(ICONS / "favicon.svg").getroot()
NS = {"svg": "http://www.w3.org/2000/svg"}


def render(size):
    scale = size * 8 / 64
    image = Image.new("RGBA", (size * 8, size * 8))
    draw = ImageDraw.Draw(image)

    def rectangle(element, offset=0, factor=1, fill=None):
        x, y, width, height, radius = [float(element.get(key, 0)) for key in ("x", "y", "width", "height", "rx")]
        bounds = [(offset + x * factor) * scale, (offset + y * factor) * scale,
                  (offset + (x + width) * factor) * scale, (offset + (y + height) * factor) * scale]
        draw.rounded_rectangle(bounds, radius=radius * factor * scale,
                               fill=fill or element.get("fill"), outline=element.get("stroke"),
                               width=max(1, round(float(element.get("stroke-width", 1)) * scale)))

    rectangle(SVG.find("svg:rect", NS))
    group = SVG.find("svg:g", NS)
    x, y, factor = map(float, re.findall(r"[\d.]+", group.get("transform")))
    assert x == y, "Icon translation must use equal x/y offsets"
    for element in group:
        rectangle(element, x, factor, group.get("fill"))
    return image.resize((size, size), Image.Resampling.LANCZOS)


if __name__ == "__main__":
    render(256).save(ICONS / "favicon.ico", sizes=[(size, size) for size in (16, 32, 48, 64, 128, 256)])
    render(180).save(ICONS / "apple-touch-icon.png")
