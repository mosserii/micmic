"""The MicMic bar: a slim strip near the top of the screen for the two-second action.

Why this exists. The goal is "Spotlight for doing things": tap a hotkey anywhere, speak,
it's done. The only window used to be the 380x560 panel, which is the right size for
music and browsing and far too much for "mute the TV". The bar shows her words live as
she speaks, then the result with Undo, then fades out on its own.

Why it is the same web page again. panel.py explains it: savta/web/index.html already
is the design, in four languages with the right text direction and gendered Hebrew.
The bar loads it with ?bar=1, which lays the same orb and type out as one horizontal
strip. There is still exactly one UI.

The one rule that shapes everything below: the bar must NEVER take focus from the app
she is working in. The next feature acts on "this", meaning whatever is on screen, by
reading the frontmost app. If showing the bar activated MicMic, the frontmost app
would become MicMic and "summarize this" would summarize the bar. So: a non-activating
NSPanel, ordered in with orderFrontRegardless, never activateIgnoringOtherApps, and it
only becomes key if she clicks into it herself.

Threading. Every public method may be called from any thread (the listener's speech
callbacks arrive on Apple's queue); the work is always done on the main thread, in
call order.
"""
from __future__ import annotations

import json
import threading
import time

import AppKit
import Foundation
import objc
import WebKit
from PyObjCTools import AppHelper

W = 640                 # Spotlight's width, near enough; narrower screens get less
MIN_H = 64              # one line: orb and words
MAX_H = 240             # the page clamps long answers well before this
RADIUS = 20             # must match html.barmode body's border-radius in index.html
TOP_FRACTION = 0.12     # how far below the menu bar, as a share of the visible height
TOP_MIN = 40
SIDE_MARGIN = 16

# How long things stay up. A result stays long enough to read at an unhurried pace
# (about 16 characters a second) and longer again when there is an Undo to reach for.
RESULT_MIN, RESULT_MAX = 4.5, 14.0
RESULT_UNDO_EXTRA = 2.5
ERROR_HOLD = 6.0
IDLE_HOLD = 0.6
HOVER_GRACE = 1.2       # after the pointer leaves, before it fades
TOUCH_HOLD = 4.0        # after she presses something in the bar
# listening/thinking/acting are closed by the listener. This only exists so that a
# listener that forgets (a crash between "thinking" and the answer) cannot leave a strip
# parked over the top of her screen for the rest of the day.
STALE_HOLD = 45.0
FADE_OUT = 0.30
HIDE_FADE = 0.15
TICK = 0.1
HEARD_INTERVAL = 1 / 30  # the recogniser can fire far faster than anyone can read
WARM_SECONDS = 0.35
RETRY_SECONDS = 2.0
ESC_KEY_CODE = 53

STATES = ("idle", "listening", "thinking", "acting", "error")


class MicMicBarPanel(AppKit.NSPanel):
    """Borderless panels refuse key status by default, which would make the Undo button
    and Esc unreachable even after she clicks into the bar. Key yes, main never: being
    key in a non-activating panel does not activate the app."""

    def canBecomeKeyWindow(self):
        return True

    def canBecomeMainWindow(self):
        return False


class MicMicBarWebView(WebKit.WKWebView):
    """The bar is never the key window when it appears, and a web view swallows the
    first click on an inactive window by default: Undo would take two clicks."""

    def acceptsFirstMouse_(self, event):
        return True


class MicMicBarBridge(AppKit.NSObject, protocols=[objc.protocolNamed("WKScriptMessageHandler")]):
    """The page's messages: "dismiss", "height:<px>", "touch"."""

    def initWithOwner_(self, owner):
        self = objc.super(MicMicBarBridge, self).init()
        if self is None:
            return None
        self._owner = owner
        return self

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        try:
            self._owner._on_message(str(message.body()))
        except Exception:  # noqa: BLE001  a bad message must never take the app down
            pass


class MicMicBarNav(AppKit.NSObject, protocols=[objc.protocolNamed("WKNavigationDelegate")]):
    """Knowing when the page is really loaded is what makes the bar warm: state sent
    before that is replayed, not dropped, and a failed load is retried."""

    def initWithOwner_(self, owner):
        self = objc.super(MicMicBarNav, self).init()
        if self is None:
            return None
        self._owner = owner
        return self

    def webView_didFinishNavigation_(self, web, nav):
        self._owner._on_loaded()

    def webView_didFailNavigation_withError_(self, web, nav, error):
        self._owner._on_load_failed()

    def webView_didFailProvisionalNavigation_withError_(self, web, nav, error):
        # The usual case at launch: the bar is built before the server is listening.
        self._owner._on_load_failed()

    def webViewWebContentProcessDidTerminate_(self, web):
        # A crashed web process leaves a blank strip; reload rather than show that.
        self._owner._on_load_failed()


def _js(value) -> str:
    """A JSON literal, so a quote, a newline or U+2028 in what she said cannot break
    the statement it is spliced into. ensure_ascii keeps U+2028/9 escaped too."""
    return json.dumps(value)


def _screen_for_point(point, screens):
    """The screen the pointer is on, edges included: at the very top of a screen the
    pointer sits ON its max edge, which NSPointInRect counts as outside."""
    x, y = point.x, point.y
    for s in screens or []:
        f = s.frame()
        if (f.origin.x <= x <= f.origin.x + f.size.width
                and f.origin.y <= y <= f.origin.y + f.size.height):
            return s
    return None


def _bar_frame(visible, height: float):
    """Centred horizontally on the given visible frame, its top a fixed share of the
    way down from the menu bar, like Spotlight. Pure, so placement is testable
    without a second monitor."""
    width = min(W, visible.size.width - 2 * SIDE_MARGIN)
    x = visible.origin.x + (visible.size.width - width) / 2.0
    top = visible.origin.y + visible.size.height - max(TOP_MIN, round(visible.size.height * TOP_FRACTION))
    return Foundation.NSMakeRect(round(x), round(top - height), width, height)


def _result_hold(say: str, has_undo: bool) -> float:
    hold = 3.5 + len(say or "") / 16.0 + (RESULT_UNDO_EXTRA if has_undo else 0.0)
    return max(RESULT_MIN, min(RESULT_MAX, hold))


class MicMicBar:
    """Owns the strip. One instance, built by the listener next to the panel."""

    def __init__(self, server: str) -> None:
        self.server = server.rstrip("/")
        self._lock = threading.Lock()
        self._h = MIN_H
        self._visible = False
        self._ready = False
        self._warmed = False
        self._retry_pending = False
        self._gen = 0                    # bumps on every show; a stale fade must not hide
        self._fade_at = None             # monotonic deadline, None = stay up
        self._shown_pointer = None
        self._pointer_moved = False
        self._timer = None
        self._esc_monitor = None
        self._heard_pending = None
        self._heard_scheduled = False
        # What the page should be showing, kept here so a page that (re)loads late, or
        # after the web process died, is brought up to date instead of left blank.
        self._m_state = ("idle", "")
        self._m_heard = ""
        self._m_result = None            # (say, undo_label) or None
        # Overridable in tests: where the pointer is, in screen coordinates.
        self._pointer = AppKit.NSEvent.mouseLocation

        rect = Foundation.NSMakeRect(-10000, -10000, W, MIN_H)
        style = AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel
        panel = MicMicBarPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, AppKit.NSBackingStoreBuffered, False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(AppKit.NSColor.clearColor())
        panel.setHasShadow_(True)
        # setFloatingPanel_ sets the level itself (to NSFloatingWindowLevel), so it has
        # to come first: the other way round, measured, the bar sat at level 3.
        panel.setFloatingPanel_(True)
        # Above ordinary floating windows (the MicMic panel, a video's picture in
        # picture): the answer to what she just asked must not open behind them.
        panel.setLevel_(AppKit.NSStatusWindowLevel)
        # An NSPanel hides whenever its app deactivates. This app is never active, so
        # left on, the bar could vanish the moment she clicked into it and back out.
        panel.setHidesOnDeactivate_(False)
        panel.setBecomesKeyOnlyIfNeeded_(True)
        panel.setWorksWhenModal_(True)
        panel.setMovable_(False)
        panel.setReleasedWhenClosed_(False)
        # Every Space, and over full-screen apps, and never in Cmd-` cycling.
        panel.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
            | AppKit.NSWindowCollectionBehaviorStationary
            | AppKit.NSWindowCollectionBehaviorIgnoresCycle)
        # The fade is ours; the system's own window animation would double it.
        panel.setAnimationBehavior_(AppKit.NSWindowAnimationBehaviorNone)
        self.panel = panel

        # Real material behind the page, so the strip reads as glass over whatever she
        # is working in rather than as a painted rectangle. The page tints it.
        bounds = Foundation.NSMakeRect(0, 0, W, MIN_H)
        blur = AppKit.NSVisualEffectView.alloc().initWithFrame_(bounds)
        blur.setMaterial_(AppKit.NSVisualEffectMaterialPopover)
        blur.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
        blur.setState_(AppKit.NSVisualEffectStateActive)
        blur.setMaskImage_(_rounded_mask(RADIUS))
        blur.setWantsLayer_(True)
        blur.layer().setCornerRadius_(RADIUS)
        blur.layer().setMasksToBounds_(True)
        blur.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)

        cfg = WebKit.WKWebViewConfiguration.alloc().init()
        # Held on self for the same reason panel.py holds its bridge.
        self._bridge = MicMicBarBridge.alloc().initWithOwner_(self)
        self._nav = MicMicBarNav.alloc().initWithOwner_(self)
        cfg.userContentController().addScriptMessageHandler_name_(self._bridge, "micmicbar")
        web = MicMicBarWebView.alloc().initWithFrame_configuration_(bounds, cfg)
        web.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        try:
            web.setValue_forKey_(False, "drawsBackground")
        except Exception:  # noqa: BLE001
            pass
        try:
            web.setUnderPageBackgroundColor_(AppKit.NSColor.clearColor())
        except Exception:  # noqa: BLE001  macOS 12+ only
            pass
        web.setNavigationDelegate_(self._nav)
        blur.addSubview_(web)
        panel.setContentView_(blur)
        self.web = web

        # Loaded now, not on first show: the whole point of a hotkey bar is that it is
        # already there when she presses the key.
        self.load()

    # ================================================================ public API
    def show(self) -> None:
        """Appear on the pointer's screen WITHOUT taking focus from her app."""
        self._main(self._show)

    def hide(self) -> None:
        self._main(lambda: self._fade_out(HIDE_FADE))

    def is_visible(self) -> bool:
        return self._visible

    def set_state(self, state: str, text: str = "") -> None:
        """"listening" | "thinking" | "acting" | "error" | "idle". "listening" and
        "idle" start a new turn and clear the last one; "idle" also hides the bar."""
        state = state if state in STATES else "idle"
        text = text or ""
        self._main(lambda: self._apply_state(state, text))

    def set_heard(self, text: str) -> None:
        """The live transcript. Safe to call hundreds of times a second: only the
        newest text is sent to the page, at most HEARD_INTERVAL apart."""
        with self._lock:
            self._heard_pending = text or ""
            if self._heard_scheduled:
                return
            self._heard_scheduled = True
        AppHelper.callLater(HEARD_INTERVAL, self._flush_heard)

    def set_result(self, say: str, undo_label: str | None) -> None:
        """The final answer. An Undo button only when undo_label is not None; an empty
        label gets the page's own word for Undo in her language."""
        say = say or ""
        label = None if undo_label is None else str(undo_label)
        self._main(lambda: self._apply_result(say, label))

    # ------------------------------------------------ not part of the contract
    def load(self) -> None:
        url = Foundation.NSURL.URLWithString_(self.server + "/?bar=1")
        self.web.loadRequest_(Foundation.NSURLRequest.requestWithURL_(url))

    def reload(self) -> None:
        """For the listener to call once the server is known to be up, as it does for
        the panel. A failed load also retries on its own."""
        self._main(self._reload)

    def close(self) -> None:
        """Tear down (tests; the app keeps its one bar for life)."""
        def go():
            self._stop_timer()
            self._remove_esc()
            self.panel.orderOut_(None)
            self._visible = False
            try:
                self.web.configuration().userContentController() \
                    .removeScriptMessageHandlerForName_("micmicbar")
            except Exception:  # noqa: BLE001
                pass
        self._main(go)

    # ================================================================ internals
    @staticmethod
    def _main(fn) -> None:
        if Foundation.NSThread.isMainThread():
            fn()
        else:
            AppHelper.callAfter(fn)

    def _eval(self, js: str) -> None:
        if self._ready:
            self.web.evaluateJavaScript_completionHandler_(js, None)

    # ---------------------------------------------------------------- page state
    def _flush_heard(self) -> None:
        with self._lock:
            text = self._heard_pending
            self._heard_pending = None
            self._heard_scheduled = False
        if text is not None:
            self._apply_heard(text)

    def _flush_heard_now(self) -> None:
        """A state or a result must never be overtaken by words that were said before
        it: a late barHeard after barResult would read as a new turn and wipe it."""
        with self._lock:
            text = self._heard_pending
            self._heard_pending = None
        if text is not None:
            self._apply_heard(text)

    def _apply_heard(self, text: str) -> None:
        if text == self._m_heard:
            return
        if self._m_result is not None:
            self._m_result = None        # new words after an answer: the next turn
        self._m_heard = text
        self._eval("window.barHeard && window.barHeard(%s);" % _js(text))
        self._arm()

    def _apply_state(self, state: str, text: str) -> None:
        self._flush_heard_now()
        if state in ("listening", "idle"):
            self._m_heard = ""
            self._m_result = None
        if state == "error":
            self._m_result = None
        self._m_state = (state, text)
        self._eval("window.barState && window.barState(%s, %s);" % (_js(state), _js(text)))
        self._arm()

    def _apply_result(self, say: str, label) -> None:
        self._flush_heard_now()
        self._m_result = (say, label)
        self._m_state = ("acting", "")
        self._eval("window.barResult && window.barResult(%s, %s);" % (_js(say), _js(label)))
        self._arm()

    def _replay(self) -> None:
        state, text = self._m_state
        self._eval("window.barState && window.barState(%s, %s);" % (_js(state), _js(text)))
        if self._m_heard:
            self._eval("window.barHeard && window.barHeard(%s);" % _js(self._m_heard))
        if self._m_result is not None:
            say, label = self._m_result
            self._eval("window.barResult && window.barResult(%s, %s);" % (_js(say), _js(label)))

    # ---------------------------------------------------------------- loading
    def _on_loaded(self) -> None:
        self._ready = True
        self._replay()
        if not self._warmed:
            self._warm()

    def _on_load_failed(self) -> None:
        self._ready = False
        if self._retry_pending:
            return
        self._retry_pending = True

        def retry():
            self._retry_pending = False
            if not self._ready:
                self.load()
        AppHelper.callLater(RETRY_SECONDS, retry)

    def _reload(self) -> None:
        self._ready = False
        self.load()

    def _warm(self) -> None:
        """A web view that has never been on screen has no layers yet, so the first
        show painted a blank strip for a few frames. Order it in once, off screen and
        fully transparent (so it can neither be seen nor clicked), then take it away."""
        self._warmed = True
        if self._visible:
            return
        self.panel.setIgnoresMouseEvents_(True)
        self.panel.setAlphaValue_(0.0)
        self.panel.setFrameOrigin_(Foundation.NSMakePoint(-10000, -10000))
        self.panel.orderFrontRegardless()
        gen = self._gen

        def done():
            self.panel.setIgnoresMouseEvents_(False)
            if gen == self._gen and not self._visible:
                self.panel.orderOut_(None)
        AppHelper.callLater(WARM_SECONDS, done)

    # ---------------------------------------------------------------- window
    def _show(self) -> None:
        self._gen += 1
        self._place()
        self.panel.setIgnoresMouseEvents_(False)
        # A fade still running from the last hide must not finish the job now.
        AppKit.NSAnimationContext.beginGrouping()
        AppKit.NSAnimationContext.currentContext().setDuration_(0.0)
        self.panel.animator().setAlphaValue_(1.0)
        AppKit.NSAnimationContext.endGrouping()
        self.panel.setAlphaValue_(1.0)
        # orderFrontRegardless, never makeKeyAndOrderFront / activateIgnoringOtherApps:
        # either of those would make MicMic the frontmost app. See the module docstring.
        self.panel.orderFrontRegardless()
        self._visible = True
        self._shown_pointer = self._pointer_xy()
        self._pointer_moved = False
        self._start_timer()
        self._install_esc()
        self._arm(from_show=True)

    def _place(self) -> None:
        screens = AppKit.NSScreen.screens()
        screen = _screen_for_point(self._pointer(), screens) or AppKit.NSScreen.mainScreen()
        if screen is None:
            return
        self.panel.setFrame_display_(_bar_frame(screen.visibleFrame(), self._h), False)

    def _set_height(self, h: float) -> None:
        h = int(max(MIN_H, min(MAX_H, round(h))))
        if h == self._h:
            return
        self._h = h
        f = self.panel.frame()
        top = f.origin.y + f.size.height          # grow downwards, top edge fixed
        self.panel.setFrame_display_(
            Foundation.NSMakeRect(f.origin.x, top - h, f.size.width, h), self._visible)
        self.panel.invalidateShadow()

    def _fade_out(self, duration: float = FADE_OUT) -> None:
        self._fade_at = None
        if not self._visible:
            return
        self._visible = False
        gen = self._gen

        def changes(ctx):
            ctx.setDuration_(duration)
            self.panel.animator().setAlphaValue_(0.0)

        def done():
            if gen != self._gen:
                return                       # shown again while fading: stay up
            self.panel.orderOut_(None)
            self._stop_timer()
            self._remove_esc()
            # Start the next show from a clean page, never from the last answer.
            self._m_state, self._m_heard, self._m_result = ("idle", ""), "", None
            self._eval("window.barState && window.barState('idle', '');")
        AppKit.NSAnimationContext.runAnimationGroup_completionHandler_(changes, done)

    # ---------------------------------------------------------------- fading
    def _arm(self, from_show: bool = False) -> None:
        """Decide when the bar goes away, from what it is showing."""
        now = time.monotonic()
        state = self._m_state[0]
        if self._m_result is not None:
            say, label = self._m_result
            self._fade_at = now + _result_hold(say, label is not None)
        elif state == "error":
            self._fade_at = now + ERROR_HOLD
        elif state == "idle":
            # show() before the first set_state is the normal order of calls; only an
            # explicit idle means "nothing is happening, go".
            self._fade_at = None if from_show else now + IDLE_HOLD
        else:
            self._fade_at = now + STALE_HOLD

    def _pointer_xy(self):
        p = self._pointer()
        return (float(p.x), float(p.y))

    def _pointer_inside(self, xy) -> bool:
        f = self.panel.frame()
        return (f.origin.x <= xy[0] <= f.origin.x + f.size.width
                and f.origin.y <= xy[1] <= f.origin.y + f.size.height)

    def _hovering(self) -> bool:
        """Hover holds the bar, but only a pointer she has moved. One that happened to
        be parked at the top of the screen when the bar appeared under it is not her
        reading it, and would otherwise keep it up forever."""
        xy = self._pointer_xy()
        if xy != self._shown_pointer:
            self._pointer_moved = True
        return self._pointer_moved and self._pointer_inside(xy)

    def _tick(self, _timer=None) -> None:
        if not self._visible or self._fade_at is None:
            return
        now = time.monotonic()
        if self._hovering():
            self._fade_at = max(self._fade_at, now + HOVER_GRACE)
            return
        if now >= self._fade_at:
            self._fade_out(FADE_OUT)

    def _start_timer(self) -> None:
        if self._timer is not None:
            return
        self._timer = Foundation.NSTimer.timerWithTimeInterval_repeats_block_(
            TICK, True, self._tick)
        # Common modes, or the countdown stops while she holds a menu open.
        Foundation.NSRunLoop.mainRunLoop().addTimer_forMode_(
            self._timer, Foundation.NSRunLoopCommonModes)

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None

    # ---------------------------------------------------------------- messages
    def _on_message(self, body: str) -> None:
        if body == "dismiss":
            self._fade_out(HIDE_FADE)
        elif body.startswith("height:"):
            try:
                self._set_height(float(body.split(":", 1)[1]))
            except ValueError:
                pass
        elif body == "touch":
            if self._fade_at is not None:
                self._fade_at = max(self._fade_at, time.monotonic() + TOUCH_HOLD)

    # ---------------------------------------------------------------- Esc
    def _install_esc(self) -> None:
        """Esc dismisses even though the bar is not the key window. A global monitor
        sees the key without taking it from her app (it needs the Accessibility grant
        the listener's hotkey already has). When she has clicked into the bar, the
        page sees Esc itself and posts "dismiss"."""
        if self._esc_monitor is not None:
            return
        self._esc_monitor = AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            AppKit.NSEventMaskKeyDown, self._on_global_key)

    def _remove_esc(self) -> None:
        if self._esc_monitor is not None:
            AppKit.NSEvent.removeMonitor_(self._esc_monitor)
            self._esc_monitor = None

    def _on_global_key(self, event) -> None:
        if event.keyCode() == ESC_KEY_CODE and self._visible:
            self._fade_out(HIDE_FADE)


def _rounded_mask(radius: float):
    """A stretchable rounded-rect mask for the material, so the blur has the same
    corners as the page painted on top of it."""
    side = radius * 2 + 1

    def draw(rect):
        AppKit.NSColor.blackColor().set()
        AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            rect, radius, radius).fill()
        return True
    img = AppKit.NSImage.imageWithSize_flipped_drawingHandler_(
        Foundation.NSMakeSize(side, side), False, draw)
    img.setCapInsets_(AppKit.NSEdgeInsets(radius, radius, radius, radius))
    img.setResizingMode_(AppKit.NSImageResizingModeStretch)
    return img
