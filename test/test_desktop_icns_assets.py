"""Regression guard for the shipped macOS `.icns` icons (#3647).

Background
----------
``website/electron/package.json`` used to point ``mac.icon`` at a bare
``icon.png``, so electron-builder synthesized the ``.icns`` itself. Its
synthesis writes **PNG** payloads into the legacy 16pt/32pt icns slots
(``icp4``/``icp5``). macOS decodes those two slots as raw **ARGB**, so it
paints the compressed PNG byte stream as pixels -- coloured static in
Spotlight rows and Finder list view (every larger slot is PNG-decoded, so the
defect is invisible at icon-view sizes). ``iconutil`` (run via
``packaging/make-icns.sh``) writes the correct ARGB-prefixed payloads for the
small slots (``ic04``/``ic05``) instead.

Why this test exists
---------------------
The fix is two committed binary ``.icns`` files plus two one-line config
changes (``mac.icon`` in ``package.json``, the nightly override in
``build-desktop.sh``). Nothing exercises the *bytes* of what actually ships,
so a well-meaning "regenerate the icon" that goes back through
electron-builder's own PNG->icns path -- or a config edit that points ``mac.icon``
back at a ``.png`` -- would reintroduce the defect with every other test still
green. This parses the committed bundles with pure byte parsing (no macOS
dependency), so it runs on Linux CI too.
"""
from __future__ import annotations

import json
import re
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ELECTRON_DIR = ROOT / "website" / "electron"
BUILD_DESKTOP_SH = ROOT / "packaging" / "build-desktop.sh"

# The two small legacy slots macOS decodes as raw ARGB, and their PNG-decoded
# replacements. A payload in one of the FIRST two whose magic is PNG (not
# ARGB) is exactly the "coloured static" bug.
_BROKEN_SMALL_SLOTS = ("icp4", "icp5")
_CORRECT_SMALL_SLOTS = ("ic04", "ic05")
# One PNG-decoded slot per shipped icns, so a fully-empty/corrupt file (e.g. a
# regeneration that silently produced 11 bytes) doesn't pass by having no
# large slots to check.
_EXPECTED_LARGE_SLOT = "ic07"  # 128pt


def _icns_slots(path: Path) -> dict[str, bytes]:
    """Parse an icns file into ``{slot_type: first_4_payload_bytes}``.

    icns format: 8-byte header (``icns`` magic + total size as
    big-endian uint32), then back-to-back chunks of ``4s I`` (type, size
    INCLUDING this 8-byte chunk header).
    """
    data = path.read_bytes()
    assert data[:4] == b"icns", f"{path}: not an icns file (bad magic)"
    total_size = struct.unpack(">I", data[4:8])[0]
    assert total_size == len(data), f"{path}: header size {total_size} != file size {len(data)}"
    slots: dict[str, bytes] = {}
    off = 8
    while off < len(data):
        slot_type, chunk_size = struct.unpack(">4sI", data[off : off + 8])
        assert chunk_size >= 8, f"{path}: slot {slot_type!r} has impossible size {chunk_size}"
        slots[slot_type.decode("ascii")] = data[off + 8 : off + 12]
        off += chunk_size
    assert off == len(data), f"{path}: trailing bytes after last chunk"
    return slots


class TestIcnsAssetsDoNotRegressToColouredStatic:
    @pytest.mark.parametrize("name", ["icon.icns", "icon-nightly.icns"])
    def test_no_legacy_png_in_argb_slots(self, name: str) -> None:
        path = ELECTRON_DIR / name
        assert path.exists(), f"{path} is missing — regenerate with packaging/make-icns.sh"
        slots = _icns_slots(path)

        for broken in _BROKEN_SMALL_SLOTS:
            assert broken not in slots, (
                f"{name}: {broken!r} present — electron-builder's own PNG->icns "
                "synthesis wrote this legacy slot again. Regenerate with "
                "packaging/make-icns.sh and point mac.icon at the .icns file."
            )

        for correct, magic in zip(_CORRECT_SMALL_SLOTS, (b"ARGB", b"ARGB")):
            assert correct in slots, f"{name}: required small-size slot {correct!r} is missing"
            assert slots[correct] == magic, (
                f"{name}: {correct!r} payload starts with {slots[correct]!r}, "
                f"expected {magic!r} — this slot must be raw ARGB, not PNG, "
                "or macOS renders it as static"
            )

        assert _EXPECTED_LARGE_SLOT in slots, f"{name}: missing {_EXPECTED_LARGE_SLOT!r} slot"
        assert slots[_EXPECTED_LARGE_SLOT].startswith(b"\x89PNG"), (
            f"{name}: {_EXPECTED_LARGE_SLOT!r} should be PNG-encoded"
        )

    def test_source_pngs_still_exist(self) -> None:
        """The .icns files are derived artifacts; make-icns.sh needs a source
        PNG to regenerate from if the artwork ever changes."""
        assert (ELECTRON_DIR / "icon.png").exists()
        assert (ELECTRON_DIR / "icon-nightly.png").exists()


class TestMacIconConfigPointsAtIcns:
    def test_package_json_mac_icon_is_icns(self) -> None:
        pkg = json.loads((ELECTRON_DIR / "package.json").read_text(encoding="utf-8"))
        mac_icon = pkg["build"]["mac"]["icon"]
        assert mac_icon == "icon.icns", (
            f"mac.icon is {mac_icon!r} — pointing it at a .png hands icns "
            "synthesis back to electron-builder, reintroducing #3647"
        )

    def test_nightly_override_in_build_desktop_is_icns(self) -> None:
        script = BUILD_DESKTOP_SH.read_text(encoding="utf-8")
        match = re.search(r'"-c\.mac\.icon=([^"]+)"', script)
        assert match, "build-desktop.sh no longer overrides -c.mac.icon for nightly"
        assert match.group(1) == "icon-nightly.icns", (
            f"nightly mac.icon override is {match.group(1)!r}, not icon-nightly.icns "
            "— reintroduces #3647 for nightly builds"
        )
