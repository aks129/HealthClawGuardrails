"""Link-preview images must exist where the meta tags say they are.

The og:image and twitter:image URLs are hand-written in two templates and
point at files in two directories. If a file is renamed or deleted, link
previews on LinkedIn, Slack and X silently go blank. This test reads each
template, maps every share-image URL back to a file on disk, and checks the
file is a 1200x630 PNG.
"""

import re
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# template -> (public origin, directory that origin's /static/ is served from)
SITES = {
    "templates/index.html": ("https://healthclaw.io", ROOT / "static"),
    "careagents/templates/landing.html": (
        "https://careagents.cloud",
        ROOT / "careagents" / "static",
    ),
}

TAG = re.compile(
    r'<meta\s+(?:property="og:image"|name="twitter:image")\s+content="([^"]+)"'
)


def _png_size(path: Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    return struct.unpack(">II", head[16:24])


@pytest.mark.parametrize("template", sorted(SITES))
def test_share_image_tags_point_at_real_1200x630_png(template):
    origin, static_dir = SITES[template]
    urls = TAG.findall((ROOT / template).read_text(encoding="utf-8"))

    # Zero tags would make every assertion below vacuous.
    assert len(urls) >= 2, f"{template}: expected og:image and twitter:image"

    for url in urls:
        assert url.startswith(f"{origin}/static/"), f"{template}: {url}"
        path = static_dir / url.removeprefix(f"{origin}/static/")
        assert path.is_file(), f"{template}: {url} -> {path} is missing"
        assert _png_size(path) == (1200, 630), f"{path} is not 1200x630"
