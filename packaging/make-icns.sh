#!/usr/bin/env bash
# Regenerate website/electron/icon.icns and icon-nightly.icns from their
# 1024px source PNGs using iconutil (#3647).
#
# electron-builder's own .icns synthesis from a bare PNG (`mac.icon:
# icon.png`) writes PNG-encoded payloads into the legacy 16pt/32pt icns slots
# (icp4/icp5). macOS decodes those two slots as raw ARGB, so it paints the
# compressed PNG byte stream as pixels -- coloured static in Spotlight rows
# and Finder list view, the two places those slots are actually used. Every
# larger slot (128pt+) is PNG-decoded, so the defect is invisible at icon-view
# sizes. iconutil writes the correct ARGB-prefixed payloads for the small
# slots (ic04/ic05) and PNG for the rest, matching what Apple's own icon
# tooling produces -- the fix is shipping an iconutil-built .icns instead of
# handing electron-builder a raw PNG.
#
# macOS-only: iconutil ships with the Xcode Command Line Tools. Not run as
# part of build-desktop.sh -- the committed .icns files ARE the build input
# (see website/electron/package.json's mac.icon and build-desktop.sh's
# nightly icon override), so a Linux CI/build host never needs this script.
# Re-run it by hand after changing icon.png / icon-nightly.png and commit the
# regenerated .icns files alongside.
set -euo pipefail

if ! command -v iconutil >/dev/null 2>&1; then
  echo "make-icns.sh: iconutil not found (requires macOS + Xcode Command Line Tools)" >&2
  exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ELECTRON_DIR="$HERE/../website/electron"

make_icns() {
  local src="$1" out="$2"
  local work iconset
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' RETURN
  iconset="$work/icon.iconset"
  mkdir -p "$iconset"
  # Standard 10-entry iconset: 5 base sizes x {1x, 2x}. iconutil requires
  # these exact filenames; sips resamples each from the 1024px source.
  local size double
  for size in 16 32 128 256 512; do
    sips -z "$size" "$size" "$src" --out "$iconset/icon_${size}x${size}.png" >/dev/null
    double=$((size * 2))
    sips -z "$double" "$double" "$src" --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
  done
  iconutil -c icns "$iconset" -o "$out"
  echo "Wrote $out"
}

make_icns "$ELECTRON_DIR/icon.png" "$ELECTRON_DIR/icon.icns"
make_icns "$ELECTRON_DIR/icon-nightly.png" "$ELECTRON_DIR/icon-nightly.icns"
