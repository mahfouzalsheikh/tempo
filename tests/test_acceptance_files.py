import copy
import struct
import xml.etree.ElementTree as ET
import zlib

import pytest

from tempo.acceptance_contract import (
    digest,
    parse_steps,
    specification,
    verify_report,
    verify_specification,
)
from tempo.acceptance_files import MAX_DOWNLOAD_BYTES, fixture, svg_evidence

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96"><circle r="30"/></svg>'
JOURNEY = (
    'upload label "Photo" png-circle-v1\nexpect text "Ready"\ndownload button "Download SVG" svg-v1'
)


def file_report():
    suite = specification("a" * 64, [{"id": "AC-1"}], {"AC-1": JOURNEY})
    steps = [{"action": action, "status": "passed"} for action in suite["checks"][0]["steps"]]
    steps[0]["file"] = fixture("png-circle-v1")[1]
    steps[-1]["file"] = svg_evidence(SVG, "art.svg")
    report = {
        "schema": 2,
        "runner": "browser-acceptance-v2",
        "suite_digest": digest(suite),
        "artifact_digest": "b" * 64,
        "browser": "test",
        "results": [{"id": "AC-1", "status": "passed", "steps": steps, "error": ""}],
    }
    return suite, report


def test_published_v1_contract_remains_unchanged_and_v2_is_explicit():
    criteria = [{"id": "AC-1"}]
    instructions = {"AC-1": 'expect text "Ready"'}
    original = {
        "schema": 1,
        "runner": "browser-acceptance-v1",
        "brief_digest": "a" * 64,
        "checks": [
            {
                "id": "AC-1",
                "instructions": instructions["AC-1"],
                "steps": [["expect", "text", "Ready"]],
            }
        ],
    }
    assert specification("a" * 64, criteria, instructions) == original
    assert verify_specification(original, digest(original), "a" * 64, criteria) == original
    with pytest.raises(ValueError):
        parse_steps(JOURNEY)
    suite, report = file_report()
    assert verify_specification(suite, digest(suite), "a" * 64, criteria) == suite
    assert verify_report(report, suite, "b" * 64)
    suite["schema"] = 1
    with pytest.raises(ValueError):
        verify_specification(suite, digest(suite), "a" * 64, criteria)


def test_v2_can_click_visible_label_text_without_forcing_hidden_inputs():
    source = 'click text "Circle Shape Art"\nexpect text "Ready"'
    suite = specification("a" * 64, [{"id": "AC-1"}], {"AC-1": source})
    assert suite["schema"] == 2
    assert parse_steps(source, schema=2)[0] == ["click", "text", "Circle Shape Art"]
    with pytest.raises(ValueError):
        parse_steps(source)


def test_v2_slider_keys_are_bounded_keyboard_input():
    source = 'press slider "Detail" Home\nexpect text "Ready"'
    suite = specification("a" * 64, [{"id": "AC-1"}], {"AC-1": source})
    assert suite["schema"] == 2
    assert parse_steps(source, schema=2)[0] == ["press", "slider", "Detail", "Home"]
    for invalid in (source.replace("Home", "Control+L"), source.replace("slider", "button")):
        with pytest.raises(ValueError):
            parse_steps(invalid, schema=2)
    with pytest.raises(ValueError):
        parse_steps(source)


@pytest.mark.parametrize(
    "action",
    [
        'upload label "Photo" /etc/passwd',
        'upload label "Photo" https://example.com/image.png',
        'upload css "input" png-circle-v1',
        'download button "Save" arbitrary-code',
        'download heading "Save" svg-v1',
    ],
)
def test_file_actions_reject_paths_urls_code_and_unknown_contracts(action):
    with pytest.raises(ValueError):
        parse_steps(action + '\nexpect text "Ready"', schema=2)


def test_fixture_is_a_fixed_png_with_actual_circle_pixels():
    data, evidence = fixture("png-circle-v1")
    assert evidence == fixture("png-circle-v1")[1]
    assert evidence["sha256"] == "3ffafa96a7df4d40abd80a5e50ae062e60dbcf6c2b8d557a24d9c7a530f2cf08"
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", data[16:24]) == (96, 96)
    length = struct.unpack(">I", data[33:37])[0]
    pixels = zlib.decompress(data[41 : 41 + length])
    assert pixels[1:4] == b"\xff\xff\xff"
    assert pixels[48 * 289 + 1 + 48 * 3 : 48 * 289 + 4 + 48 * 3] == b"\0\0\0"


@pytest.mark.parametrize(
    "payload",
    [
        b"not an SVG",
        b"\xff",
        b'<html><circle r="5"/></html>',
        b'<!DOCTYPE svg [<!ENTITY x "oops">]>' + SVG,
        b'<?xml-stylesheet href="https://evil.example/"?>' + SVG,
        SVG.replace(b'<circle r="30"/>', b"<script>alert(1)</script>"),
        SVG.replace(b'<circle r="30"/>', b"<foreignObject/>"),
        SVG.replace(b'<circle r="30"/>', b'<image href="https://evil.example/"/>'),
        SVG.replace(b'r="30"', b'r="30" onload="alert(1)"'),
        SVG.replace(b'r="30"', b'r="30" fill="url(https://evil.example/)"'),
        SVG.replace(b'r="30"', b'r="30" style="background: red"'),
        SVG.replace(b"0 0 96 96", b"0 0 0 96"),
        SVG.replace(b"0 0 96 96", b"0 0 inf 96"),
        SVG.replace(b"0 0 96 96", b"0 0 96"),
        SVG.replace(b'<circle r="30"/>', b""),
        SVG.replace(b'<circle r="30"/>', b"<g>" * 65 + b"<circle/>" + b"</g>" * 65),
    ],
)
def test_svg_validator_rejects_non_svg_active_external_empty_or_unbounded_structures(payload):
    with pytest.raises((ValueError, ET.ParseError)):
        svg_evidence(payload, "art.svg")


@pytest.mark.parametrize("filename", ["../art.svg", "art.html", "C:\\art.svg", "art\n.svg"])
def test_download_name_is_bounded_and_never_used_as_a_path(filename):
    with pytest.raises(ValueError):
        svg_evidence(SVG, filename)


def test_download_size_is_bounded():
    with pytest.raises(ValueError):
        svg_evidence(b"x" * (MAX_DOWNLOAD_BYTES + 1), "art.svg")


@pytest.mark.parametrize("change", ["missing", "fixture", "digest", "size", "geometry", "extra"])
def test_passing_file_steps_require_complete_matching_evidence(change):
    suite, report = file_report()
    original = copy.deepcopy(report)
    steps = report["results"][0]["steps"]
    if change == "missing":
        del steps[-1]["file"]
    elif change == "fixture":
        steps[0]["file"]["sha256"] = "c" * 64
    elif change == "digest":
        steps[-1]["file"]["sha256"] = "invalid"
    elif change == "size":
        steps[-1]["file"]["size"] = True
    elif change == "geometry":
        steps[-1]["file"]["geometry_elements"] = 0
    else:
        steps[-1]["file"]["unchecked"] = True
    with pytest.raises(ValueError):
        verify_report(report, suite, "b" * 64)
    assert verify_report(original, suite, "b" * 64)


def test_failed_download_is_recorded_without_inventing_file_evidence():
    suite, report = file_report()
    result = report["results"][0]
    result.update(status="failed", error="SVG contains no vector geometry")
    result["steps"][-1] = {"action": result["steps"][-1]["action"], "status": "failed"}
    assert not verify_report(report, suite, "b" * 64)
