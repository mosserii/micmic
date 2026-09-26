#!/usr/bin/env python3
"""Render the system "mic" SF Symbol to a PNG, purely so render.py can place it next
to MicMic's own mark in the preview strip for a side-by-side weight comparison (the
owner asked for this after the first pass read as "00 on a line" at menu-bar size).

    native/.venv/bin/python3 brand/menubar/sf_symbol_ref.py

Needs AppKit (PyObjC), so it runs under native/.venv, not the project's plain `uv run`
venv that renders the SVGs with Playwright. Writes sf-mic-ref.png (18x18) and
sf-mic-ref@2x.png (36x36), black on transparent, regular weight -- the same size and
color convention as mic-idle.png/mic-active.png, so the comparison is apples to apples.
"""
from __future__ import annotations

from pathlib import Path

import AppKit

HERE = Path(__file__).resolve().parent


def render(name: str, size_pt: int, out_path: Path) -> None:
    img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, "ref")
    if img is None:
        raise SystemExit(f"no SF Symbol named {name!r} on this macOS")
    img.setSize_(AppKit.NSMakeSize(size_pt, size_pt))
    rep = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, size_pt, size_pt, 8, 4, True, False, AppKit.NSDeviceRGBColorSpace, 0, 0)
    ctx = AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    AppKit.NSGraphicsContext.setCurrentContext_(ctx)
    # Paint it black: a template NSImage draws in the current fill color when the
    # context is told to treat it that way, which is exactly what a menu bar does.
    AppKit.NSColor.blackColor().set()
    img.setTemplate_(True)
    img.drawInRect_fromRect_operation_fraction_(
        AppKit.NSMakeRect(0, 0, size_pt, size_pt), AppKit.NSZeroRect,
        AppKit.NSCompositingOperationSourceOver, 1.0)
    AppKit.NSGraphicsContext.setCurrentContext_(None)
    data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
    data.writeToFile_atomically_(str(out_path), True)
    print(f"wrote {out_path.relative_to(HERE.parents[1])}")


def main() -> None:
    render("mic", 18, HERE / "sf-mic-ref.png")
    render("mic", 36, HERE / "sf-mic-ref@2x.png")


if __name__ == "__main__":
    main()
