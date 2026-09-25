"""A throwaway window app that the route suite launches inside a bundle named MicMic,
so _front_is_me() can be exercised against a real frontmost process with that name.

It shows only synthetic text, writes its own pid to argv[1], and runs until the suite
terminates it by that pid. It never reads anything.
"""
import os
import sys

from AppKit import (NSApplication, NSWindow, NSTextField, NSBackingStoreBuffered,
                    NSMakeRect, NSRunningApplication, NSApplicationActivateIgnoringOtherApps)

app = NSApplication.sharedApplication()
app.setActivationPolicy_(0)
app.finishLaunching()
win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
    NSMakeRect(120, 160, 420, 200), 15, NSBackingStoreBuffered, False)
win.setTitle_("MicMic helper window")
tf = NSTextField.alloc().initWithFrame_(NSMakeRect(20, 80, 380, 24))
tf.setStringValue_("HELPER_SYNTHETIC_TEXT only")
tf.setEditable_(False)
win.contentView().addSubview_(tf)
win.makeKeyAndOrderFront_(None)
NSRunningApplication.currentApplication().activateWithOptions_(
    NSApplicationActivateIgnoringOtherApps)
if len(sys.argv) > 1:
    with open(sys.argv[1], "w") as fh:
        fh.write(str(os.getpid()))
app.run()
