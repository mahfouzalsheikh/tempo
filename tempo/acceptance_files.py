"""Image-owned fixtures and bounded SVG structure checks. Never read operator paths."""

import hashlib
import math
import re
import struct
import xml.etree.ElementTree as ET
import zlib

MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024
SVG = "{http://www.w3.org/2000/svg}"
GEOMETRY = {"path", "circle", "ellipse", "rect", "line", "polyline", "polygon"}
ELEMENTS = GEOMETRY | {"svg", "g", "title", "desc", "metadata"}
ATTRIBUTES = {
    "viewBox",
    "width",
    "height",
    "role",
    "aria-label",
    "id",
    "version",
    "x",
    "y",
    "x1",
    "x2",
    "y1",
    "y2",
    "cx",
    "cy",
    "r",
    "rx",
    "ry",
    "d",
    "points",
    "fill",
    "stroke",
    "stroke-width",
    "stroke-linecap",
    "stroke-linejoin",
    "fill-rule",
    "opacity",
    "fill-opacity",
    "stroke-opacity",
    "transform",
    "preserveAspectRatio",
}


def fingerprint(data, filename, media_type):
    return {
        "filename": filename,
        "media_type": media_type,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def fixture(name):
    if name != "png-circle-v1":
        raise ValueError("Unknown upload fixture")
    # Fixed RGB pixels and a single uncompressed DEFLATE block make these exact
    # bytes independent of encoder versions. 96px black circle on white paper.
    raw = b"".join(
        b"\0"
        + b"".join(
            bytes([0 if (x - 48) ** 2 + (y - 48) ** 2 <= 30**2 else 255]) * 3 for x in range(96)
        )
        for y in range(96)
    )
    compressed = (
        b"\x78\x01\x01"
        + struct.pack("<HH", len(raw), len(raw) ^ 0xFFFF)
        + raw
        + struct.pack(">I", zlib.adler32(raw))
    )

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    data = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 96, 96, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", compressed)
        + chunk(b"IEND", b"")
    )
    evidence = fingerprint(data, "circle.png", "image/png") | {"fixture": name}
    return data, evidence


def valid_filename(filename):
    return (
        isinstance(filename, str)
        and 1 <= len(filename) <= 255
        and filename.lower().endswith(".svg")
        and not any(c in filename for c in "/\\")
        and not any(ord(c) < 32 or ord(c) == 127 for c in filename)
    )


def svg_evidence(data, filename):
    """Verify a deliberately narrow passive SVG subset; this is not a visual-quality check."""
    if not 1 <= len(data) <= MAX_DOWNLOAD_BYTES or not valid_filename(filename):
        raise ValueError("SVG download must have a .svg name and be at most 16 MiB")
    source = data.decode("utf-8-sig")
    if "<!" in source or "<?" in re.sub(r"^\s*<\?xml\s+[^?]*\?>", "", source, count=1):
        raise ValueError("SVG declarations, entities, and processing instructions are unsupported")
    root = ET.fromstring(source)
    if root.tag != SVG + "svg":
        raise ValueError("Downloaded bytes are not an SVG document")
    try:
        box = [float(part) for part in re.split(r"[\s,]+", root.attrib["viewBox"].strip())]
        if len(box) != 4 or not all(math.isfinite(n) for n in box) or min(box[2:]) <= 0:
            raise ValueError
    except (KeyError, ValueError) as exc:
        raise ValueError("SVG requires a finite, nonempty viewBox") from exc
    count, geometry = 0, 0
    pending = [(root, 0)]
    while pending:
        element, depth = pending.pop()
        count += 1
        if count > 50000 or depth > 64:
            raise ValueError("SVG structure exceeds validation limits")
        name = element.tag.removeprefix(SVG)
        if element.tag != SVG + name or name not in ELEMENTS:
            raise ValueError("SVG contains unsupported or active elements")
        for key, value in element.attrib.items():
            if key not in ATTRIBUTES or re.search(r"url\s*\(|[\\\x00-\x1f]", value, re.I):
                raise ValueError("SVG contains unsupported attributes or resource references")
        if name in GEOMETRY:
            geometry += 1
        pending.extend((child, depth + 1) for child in element)
    if not geometry:
        raise ValueError("SVG contains no vector geometry")
    return fingerprint(data, filename, "image/svg+xml") | {
        "validator": "svg-v1",
        "geometry_elements": geometry,
    }


def verify_file_evidence(action, evidence):
    if action[0] == "upload":
        if evidence != fixture(action[3])[1]:
            raise ValueError("Upload evidence does not match the reviewed fixture")
        return
    if (
        not isinstance(evidence, dict)
        or set(evidence)
        != {"filename", "media_type", "size", "sha256", "validator", "geometry_elements"}
        or evidence["validator"] != action[3]
        or evidence["media_type"] != "image/svg+xml"
        or not valid_filename(evidence["filename"])
        or type(evidence["size"]) is not int
        or not 1 <= evidence["size"] <= MAX_DOWNLOAD_BYTES
        or type(evidence["geometry_elements"]) is not int
        or not 1 <= evidence["geometry_elements"] <= 50000
        or not isinstance(evidence["sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", evidence["sha256"])
    ):
        raise ValueError("Invalid SVG download evidence")
