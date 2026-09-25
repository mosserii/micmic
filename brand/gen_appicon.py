"""AppIcon.icns from micmic-icon.png, on Apple's macOS icon grid.

The artwork is a full-bleed rounded square. macOS icons are not: the shape sits in
824 of the 1024 points with transparent margin around it (room for the shadow), so
full bleed looked a size larger than Chrome or WhatsApp next to it in the Dock.
Run with native/.venv/bin/python3 (it needs AppKit)."""
import pathlib, subprocess, tempfile
import AppKit, Foundation

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE / "micmic-icon.png"
GRID = 824 / 1024          # the rounded square's share of the canvas

def render(px: int, out: pathlib.Path) -> None:
    src = AppKit.NSImage.alloc().initWithContentsOfFile_(str(SRC))
    rep = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, px, px, 8, 4, True, False, AppKit.NSDeviceRGBColorSpace, 0, 0)
    ctx = AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    AppKit.NSGraphicsContext.saveGraphicsState()
    AppKit.NSGraphicsContext.setCurrentContext_(ctx)
    ctx.setImageInterpolation_(AppKit.NSImageInterpolationHigh)
    side = px * GRID
    origin = (px - side) / 2
    shadow = AppKit.NSShadow.alloc().init()
    shadow.setShadowOffset_(Foundation.NSMakeSize(0, -px * 0.010))
    shadow.setShadowBlurRadius_(px * 0.022)
    shadow.setShadowColor_(AppKit.NSColor.colorWithCalibratedWhite_alpha_(0, 0.30))
    shadow.set()
    # Nudge up by the shadow's offset so shape + shadow read as centred, like Apple's.
    src.drawInRect_fromRect_operation_fraction_(
        Foundation.NSMakeRect(origin, origin + px * 0.006, side, side),
        Foundation.NSZeroRect, AppKit.NSCompositingOperationSourceOver, 1.0)
    AppKit.NSGraphicsContext.restoreGraphicsState()
    png = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
    png.writeToFile_atomically_(str(out), True)

with tempfile.TemporaryDirectory() as tmp:
    iconset = pathlib.Path(tmp) / "AppIcon.iconset"
    iconset.mkdir()
    for pt in (16, 32, 128, 256, 512):
        render(pt, iconset / f"icon_{pt}x{pt}.png")
        render(pt * 2, iconset / f"icon_{pt}x{pt}@2x.png")
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(HERE / "AppIcon.icns")], check=True)
    render(1024, HERE / "micmic-appicon-1024.png")
print("wrote AppIcon.icns and micmic-appicon-1024.png")
