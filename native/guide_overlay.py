"""The ring on the real screen while MicMic guides her through something, step by step.

The server works out each step (savta/actions/guide.py) and names the control to
point at with its accessibility frame. This draws a soft green ring around that frame
in a window of its own: borderless, transparent, above ordinary windows, and blind to
the mouse, so every click goes straight through to the control under it and her app
never loses focus. No frame, no ring: a ring in the wrong place is worse than none.

GuideFollower polls GET /api/guide/state (the same way the page's guide card does)
and keeps three things in step with it: the ring, the strip (shown when a guide starts
or moves to a new step, if Settings says the strip is what appears, and moved to the
bottom of the screen when the ring would sit under it), and nothing else. It never
activates MicMic.

Coordinates. Accessibility frames have their origin at the top left of the main
screen with y growing downward; AppKit's have it at the bottom left with y growing
upward. to_cocoa() is the one conversion, pure so it is tested without a display.
"""
from __future__ import annotations

import json
import threading
import urllib.request

import AppKit
import Foundation
import objc
from PyObjCTools import AppHelper

GREEN_LIGHT = (0x2d / 255, 0x8b / 255, 0x6b / 255)   # the brand's --live, light
GREEN_DARK = (0x57 / 255, 0xc6 / 255, 0x9d / 255)    # and dark
PAD = 14.0              # room around the control for the glow
RADIUS = 10.0
LINE = 3.0
POLL_ACTIVE = 0.25
POLL_IDLE = 1.5


def to_cocoa(ring: dict, main_height: float):
    """(x, y, w, h) in AppKit screen coordinates for an accessibility-space ring."""
    x, y, w, h = float(ring["x"]), float(ring["y"]), float(ring["w"]), float(ring["h"])
    return (x, main_height - (y + h), w, h)


def intersects(a, b) -> bool:
    if not a or not b:
        return False
    return not (a[0] + a[2] <= b[0] or b[0] + b[2] <= a[0]
                or a[1] + a[3] <= b[1] or b[1] + b[3] <= a[1])


def _dark() -> bool:
    try:
        name = AppKit.NSApp.effectiveAppearance().bestMatchFromAppearancesWithNames_(
            [AppKit.NSAppearanceNameAqua, AppKit.NSAppearanceNameDarkAqua])
        return name == AppKit.NSAppearanceNameDarkAqua
    except Exception:  # noqa: BLE001
        return False


def _reduce_motion() -> bool:
    try:
        return bool(AppKit.NSWorkspace.sharedWorkspace()
                    .accessibilityDisplayShouldReduceMotion())
    except Exception:  # noqa: BLE001
        return False


class RingView(AppKit.NSView):
    """A rounded ring with a soft glow, drawn with layers so the pulse costs the CPU
    nothing: Core Animation runs it."""

    def initWithFrame_(self, frame):
        self = objc.super(RingView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.setWantsLayer_(True)
        self._ring = None
        self._build()
        return self

    def isFlipped(self):
        return True

    def _build(self):
        import Quartz
        r, g, b = GREEN_DARK if _dark() else GREEN_LIGHT
        color = Quartz.CGColorCreateSRGB(r, g, b, 1.0)
        ring = Quartz.CAShapeLayer.layer()
        ring.setFillColor_(Quartz.CGColorCreateSRGB(r, g, b, 0.07))
        ring.setStrokeColor_(color)
        ring.setLineWidth_(LINE)
        ring.setShadowColor_(color)
        ring.setShadowOpacity_(0.85)
        ring.setShadowRadius_(9.0)
        ring.setShadowOffset_(Foundation.NSMakeSize(0, 0))
        self.layer().addSublayer_(ring)
        self._ring = ring
        self.layout_ring()
        if not _reduce_motion():
            pulse = Quartz.CABasicAnimation.animationWithKeyPath_("shadowRadius")
            pulse.setFromValue_(6.0)
            pulse.setToValue_(14.0)
            pulse.setDuration_(1.1)
            pulse.setAutoreverses_(True)
            pulse.setRepeatCount_(1e9)
            pulse.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(
                Quartz.kCAMediaTimingFunctionEaseInEaseOut))
            ring.addAnimation_forKey_(pulse, "pulse")

    def layout_ring(self):
        import Quartz
        if self._ring is None:
            return
        b = self.bounds()
        inner = Foundation.NSInsetRect(b, PAD - LINE, PAD - LINE)
        path = Quartz.CGPathCreateWithRoundedRect(inner, RADIUS, RADIUS, None)
        self._ring.setFrame_(b)
        self._ring.setPath_(path)

    def setFrameSize_(self, size):
        objc.super(RingView, self).setFrameSize_(size)
        self.layout_ring()


class GuideOverlay:
    """One ring window for the app's life. Every method may be called from any
    thread; the work is done on the main thread."""

    def __init__(self) -> None:
        self._shown = None
        self.window = None

    def _build(self) -> None:
        rect = Foundation.NSMakeRect(-10000, -10000, 40, 40)
        w = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, AppKit.NSWindowStyleMaskBorderless, AppKit.NSBackingStoreBuffered, False)
        w.setOpaque_(False)
        w.setBackgroundColor_(AppKit.NSColor.clearColor())
        w.setHasShadow_(False)
        # Clicks go through to her app: the ring is only ever a picture.
        w.setIgnoresMouseEvents_(True)
        # Above her windows, below the strip (NSStatusWindowLevel), which holds the words.
        w.setLevel_(AppKit.NSStatusWindowLevel - 1)
        w.setReleasedWhenClosed_(False)
        w.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
            | AppKit.NSWindowCollectionBehaviorStationary
            | AppKit.NSWindowCollectionBehaviorIgnoresCycle)
        w.setAnimationBehavior_(AppKit.NSWindowAnimationBehaviorNone)
        view = RingView.alloc().initWithFrame_(Foundation.NSMakeRect(0, 0, 40, 40))
        view.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        w.setContentView_(view)
        self.window = w

    @staticmethod
    def _main(fn) -> None:
        if Foundation.NSThread.isMainThread():
            fn()
        else:
            AppHelper.callAfter(fn)

    def show(self, ring: dict) -> None:
        """Ring the control at this accessibility-space frame."""
        key = (ring["x"], ring["y"], ring["w"], ring["h"])

        def go():
            if self.window is None:
                self._build()
            screens = AppKit.NSScreen.screens()
            if not screens:
                return
            x, y, w, h = to_cocoa(ring, screens[0].frame().size.height)
            frame = Foundation.NSMakeRect(x - PAD, y - PAD, w + 2 * PAD, h + 2 * PAD)
            first = self._shown is None
            self.window.setFrame_display_(frame, True)
            if first:
                self.window.setAlphaValue_(0.0)
                # orderFrontRegardless: never makeKey, never activate. Her app stays
                # the frontmost one, which is what "this" is read from.
                self.window.orderFrontRegardless()

                def fade(ctx):
                    ctx.setDuration_(0.18)
                    self.window.animator().setAlphaValue_(1.0)
                AppKit.NSAnimationContext.runAnimationGroup_completionHandler_(fade, None)
            self._shown = key
        self._main(go)

    def hide(self) -> None:
        def go():
            if self.window is not None and self._shown is not None:
                self.window.orderOut_(None)
            self._shown = None
        self._main(go)

    def showing(self):
        return self._shown


def _get_state(server: str) -> dict | None:
    try:
        with urllib.request.urlopen(server.rstrip("/") + "/api/guide/state", timeout=2) as r:
            return json.loads(r.read() or b"{}")
    except Exception:  # noqa: BLE001
        return None


class GuideFollower:
    """Keeps the ring and the strip in step with the server's guide.

    `display` is a callable giving "bar", "panel" or "none" (Listener.display_mode),
    read on every change so a Settings change applies at once."""

    def __init__(self, server: str, overlay, bar=None, panel=None, display=None,
                 get_state=None, log=None) -> None:
        self.server = server
        self.overlay = overlay
        self.bar = bar
        self.panel = panel
        self.display = display or (lambda: "bar")
        self.get_state = get_state or (lambda: _get_state(self.server))
        self.log = log or (lambda m: None)
        self._last = None             # (id, n, status) last acted on
        self._ring = None
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="micmic-guide-follow")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            st = self.get_state()
            try:
                self.apply(st)
            except Exception as e:  # noqa: BLE001  the ring must never take the app down
                self.log(f"guide overlay: {e!r}")
            active = bool(st and st.get("active"))
            self._stop.wait(POLL_ACTIVE if active else POLL_IDLE)

    def apply(self, st: dict | None) -> None:
        """One state from the server, acted on. Split from the loop so it is tested
        with made-up states."""
        if not st:
            return                      # the server is away: leave things as they are
        active = bool(st.get("active"))
        ring = st.get("ring") if active and st.get("status") == "step" else None
        if ring and all(k in ring for k in ("x", "y", "w", "h")):
            if ring != self._ring:
                self.overlay.show(ring)
                self._ring = ring
        elif self._ring is not None:
            self.overlay.hide()
            self._ring = None
        key = (st.get("id"), st.get("n"), st.get("status"))
        if active and key != self._last:
            # A guide started, or moved on a step: make sure its words are in view.
            mode = self.display()
            if mode == "bar" and self.bar is not None and not self.bar.is_visible():
                self.bar.show()
            elif mode == "panel" and self.panel is not None and self._last is None:
                self.panel.show(activate=False)
        self._last = key if active else None
        self._avoid(ring, active)

    def _avoid(self, ring, active: bool) -> None:
        """The strip moves to the other edge of the screen while the ring would be
        under it, and stays there between steps so it does not jump back and forth."""
        if self.bar is None or not hasattr(self.bar, "set_edge"):
            return
        if not ring:
            if not active:
                self.bar.set_edge("top")
            return
        try:
            screens = AppKit.NSScreen.screens()
            main_h = screens[0].frame().size.height if screens else 0
            frame = self.bar.frame() if hasattr(self.bar, "frame") else None
        except Exception:  # noqa: BLE001
            return
        if frame is None or not main_h:
            return
        r = to_cocoa(ring, main_h)
        r = (r[0] - PAD, r[1] - PAD, r[2] + 2 * PAD, r[3] + 2 * PAD)
        f = (frame.origin.x, frame.origin.y, frame.size.width, frame.size.height)
        if intersects(r, f):
            self.bar.set_edge("top" if getattr(self.bar, "_edge", "top") == "bottom" else "bottom")
