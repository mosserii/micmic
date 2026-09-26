"""The MicMic window: the browser UI, in a small floating panel of its own.

Why this exists. The app is LSUIElement, so its only visible state was one character
in the menu bar. You spoke, nothing moved for two or three seconds, and there was no
way to tell "thinking" from "broken" — the same problem the browser orb was built to
solve, on the surface people actually use.

Why it is a WKWebView and not hand-drawn AppKit. savta/web/index.html already is the
design: the drifting blobs, the glass, the orb, the spinner, the cards, the transcript,
four languages with the right text direction for each. Redrawing that with NSBezierPath
would be a large pile of code whose only possible outcome is looking slightly worse
than the thing it copies, and drifting away from it on every future change. Hosting the
real page means there is exactly one UI and it cannot fall out of step.

The panel does not listen. It loads the page with ?panel=1, which leaves the microphone
to the native listener — two recognisers on one microphone fight — and instead mirrors
the listener's state, so speaking the wake word moves the orb in here too.
"""
from __future__ import annotations

import AppKit
import Foundation
import objc
import WebKit

W, H = 380, 560


# Objective-C class names are global to the process, so every NSObject subclass in
# native/ carries a unique MicMic prefix: bar.py once had a second "_Bridge", and the
# panel failed to build with "overriding existing Objective-C class".
class MicMicPanelBridge(AppKit.NSObject, protocols=[objc.protocolNamed("WKScriptMessageHandler")]):
    """The page's way to reach the listener. In panel mode the page has no microphone of
    its own (the listener owns it), so a click on the orb used to do nothing at all.
    Now it posts "listen" here and the listener arms, exactly as "Listen now" does.
    It also posts "drag" when a press on its background starts to move: see
    MicMicPanel._drag_window."""

    def initWithCallback_(self, callback):
        self = objc.super(MicMicPanelBridge, self).init()
        if self is None:
            return None
        self._callback = callback
        self._drag = None
        return self

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        body = str(message.body())
        if body == "listen" and self._callback is not None:
            self._callback()
        elif body == "drag" and self._drag is not None:
            self._drag()


class MicMicPanel:
    """Owns the window. One instance, built by the listener's delegate."""

    def __init__(self, server: str, on_listen=None) -> None:
        self.server = server.rstrip("/")

        style = (AppKit.NSWindowStyleMaskTitled
                 | AppKit.NSWindowStyleMaskClosable
                 | AppKit.NSWindowStyleMaskMiniaturizable   # the yellow button
                 | AppKit.NSWindowStyleMaskFullSizeContentView
                 | AppKit.NSWindowStyleMaskNonactivatingPanel)
        self.panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            Foundation.NSMakeRect(0, 0, W, H), style, AppKit.NSBackingStoreBuffered, False)
        self.panel.setTitle_("MicMic")
        self.panel.setTitlebarAppearsTransparent_(True)
        self.panel.setTitleVisibility_(AppKit.NSWindowTitleHidden)
        self.panel.setMovableByWindowBackground_(True)
        # Floating and on every Space: a status light you cannot see behind the window
        # you are working in is not a status light.
        self.panel.setLevel_(AppKit.NSFloatingWindowLevel)
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setBecomesKeyOnlyIfNeeded_(False)   # the typing box needs real keys
        self.panel.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)
        # Closing hides; the menu item reopens the same object rather than rebuilding.
        self.panel.setReleasedWhenClosed_(False)
        # Minimise is real: she asked for it, and it sends the window to the Dock like
        # any other app. Zoom stays hidden — the panel has one size, so the green
        # button could only ever be a grey dot that does nothing.
        for kind in (AppKit.NSWindowZoomButton,):
            button = self.panel.standardWindowButton_(kind)
            if button is not None:
                button.setHidden_(True)

        cfg = WebKit.WKWebViewConfiguration.alloc().init()
        # Held on self: the content controller keeps its own reference, but an object
        # nobody on the Python side holds is one refactor away from being collected.
        self._bridge = MicMicPanelBridge.alloc().initWithCallback_(on_listen)
        self._bridge._drag = self._drag_window
        cfg.userContentController().addScriptMessageHandler_name_(self._bridge, "micmic")
        self.web = WebKit.WKWebView.alloc().initWithFrame_configuration_(
            Foundation.NSMakeRect(0, 0, W, H), cfg)
        self.web.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        # The page paints its own background; letting the web view draw one too puts an
        # opaque rectangle over the window's rounded corners.
        try:
            self.web.setValue_forKey_(False, "drawsBackground")
        except Exception:  # noqa: BLE001
            pass
        self.panel.setContentView_(self.web)

        self._loaded = False
        self._placed = False

        # The last left press inside this window, for _drag_window. The drag is anchored
        # on the press itself, not on the move that made it a drag, so the spot she
        # grabbed stays under the pointer instead of the window trailing it.
        self._press = None

        def note(event):
            if event.windowNumber() == self.panel.windowNumber():
                self._press = event
            return event
        self._monitor = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            AppKit.NSEventMaskLeftMouseDown, note)

    # -------------------------------------------------------------- state
    def set_state(self, state: str, text: str = "") -> None:
        """Mirror the native listener's state onto the page's orb.

        The page exposes window.panelState for exactly this. Calls before the page has
        finished loading are dropped rather than queued: the next state change is never
        more than a moment away, and a queue would replay a stale one over a fresh one.
        """
        if not self._loaded:
            return
        js = "window.panelState && window.panelState(%s, %s);" % (
            _js_string(state), _js_string(text))
        self.web.evaluateJavaScript_completionHandler_(js, None)

    def set_heard(self, text: str) -> None:
        if not self._loaded:
            return
        self.web.evaluateJavaScript_completionHandler_(
            "window.panelHeard && window.panelHeard(%s);" % _js_string(text), None)

    def set_reply(self, text: str) -> None:
        if not self._loaded:
            return
        self.web.evaluateJavaScript_completionHandler_(
            "window.panelReply && window.panelReply(%s);" % _js_string(text), None)

    # -------------------------------------------------------------- window
    def show(self, activate: bool = True) -> None:
        if not self._placed:
            self._place_top_right()
            self._placed = True
        if not self._loaded:
            self.load()
        # Minimised to the Dock, ordering it front does nothing visible; it has to be
        # brought back first. This is what a click on the Dock icon ends up calling.
        if self.panel.isMiniaturized():
            self.panel.deminiaturize_(None)
        self.panel.orderFrontRegardless()
        if activate:
            AppKit.NSApp().activateIgnoringOtherApps_(True)

    def toggle(self) -> None:
        if self.panel.isVisible():
            self.panel.orderOut_(None)
        else:
            self.show()

    def load(self) -> None:
        url = Foundation.NSURL.URLWithString_(self.server + "/?panel=1")
        self.web.loadRequest_(Foundation.NSURLRequest.requestWithURL_(url))
        self._loaded = True

    def reload(self) -> None:
        """Called once the server is known to be up: the panel often opens first."""
        if self._loaded:
            self.web.reload_(None)
        else:
            self.load()

    def _drag_window(self) -> None:
        """Move the window with the pointer, from a press on the page's background.

        setMovableByWindowBackground_ is not enough on its own: the web view takes every
        mouse event, so AppKit never sees a press on "background". The page reports when
        a press on its background has moved (never on a control, the typing box or
        text), and the window server takes the drag from here until the button is up.
        A message that arrives after the button is already up does nothing, or the
        window would follow a pointer nobody is holding.
        """
        if not _left_button_down():
            return
        event = self._press
        if event is None:
            # No press recorded (it should always be): the move that triggered this is
            # the next best anchor.
            event = AppKit.NSApp().currentEvent()
            if not (event is not None and event.type() in _PRESS_TYPES
                    and event.windowNumber() == self.panel.windowNumber()):
                return
        self.panel.performWindowDragWithEvent_(event)

    def _place_top_right(self) -> None:
        """Out of the way by default. The middle of the screen is where a dialog goes,
        and this is not a dialog — it is meant to be visible while you work."""
        screen = AppKit.NSScreen.mainScreen()
        if screen is None:
            return
        vis = screen.visibleFrame()
        self.panel.setFrameOrigin_(Foundation.NSMakePoint(
            vis.origin.x + vis.size.width - W - 24,
            vis.origin.y + vis.size.height - H - 24))


_PRESS_TYPES = (AppKit.NSEventTypeLeftMouseDown, AppKit.NSEventTypeLeftMouseDragged)


def _left_button_down() -> bool:
    return bool(AppKit.NSEvent.pressedMouseButtons() & 1)


def _js_string(s: str) -> str:
    """A JSON string literal, so a quote or a newline in a reply cannot break the
    statement it is spliced into."""
    import json
    return json.dumps(s or "")
