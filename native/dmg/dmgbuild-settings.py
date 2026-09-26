# dmgbuild settings for the MicMic download (native/build.sh, step 9).
#
#   uvx --from dmgbuild==1.6.7 python native/dmg/make_dmg.py /path/to/MicMic.app MicMic.dmg
#
# (make_dmg.py runs dmgbuild with this file; see it for why the mount is private.)
#
# dmgbuild writes Finder's .DS_Store itself, so the window looks the same for every
# download without scripting Finder (which needs Automation permission and races the
# mount). It copies the app with ditto, which keeps the signature intact, but it
# carries on if that copy fails, so build.sh mounts the result and checks the app.
#
# The icon positions below are the points background.html draws the slots, the arrow
# and the name plates around. Moving one means moving the other and re-running
# render.py.
import inspect
import os.path

# dmgbuild exec()s this file without __file__; the code object still knows its path.
HERE = os.path.dirname(os.path.abspath(inspect.currentframe().f_code.co_filename))
app = defines["app"]  # noqa: F821 (dmgbuild provides `defines`)
appname = os.path.basename(app)

# ULFO (lzfse) over UDZO (zlib): see the note in build.sh. HFS+ as before.
format = "ULFO"
filesystem = "HFS+"

files = [app]
symlinks = {"Applications": "/Applications"}
# Finder already shows an app without ".app" (hasHiddenExtension is true for any
# bundle without the flag, measured on the built app). Setting the flag anyway puts a
# com.apple.FinderInfo xattr on the bundle, and `codesign --verify --strict` then
# rejects the app ("Finder information, or similar detritus not allowed").
hide_extensions = []
# The volume shows the app's own icon on the desktop and in the Finder sidebar.
icon = os.path.join(app, "Contents", "Resources", "AppIcon.icns")

background = os.path.join(HERE, "background.tiff")
show_status_bar = False
show_tab_view = False
show_toolbar = False
show_pathbar = False
show_sidebar = False
sidebar_width = 0
default_view = "icon-view"
show_icon_preview = False
include_icon_view_settings = True
include_list_view_settings = False

# The window's frame: 660 x 420 points of content (the background's size) plus
# Finder's 28-point title bar. Measured on macOS 15: a 420-high frame left 392.
window_rect = ((200, 140), (660, 448))
arrange_by = None
grid_spacing = 100
scroll_position = (0, 0)
label_pos = "bottom"
icon_size = 128
text_size = 13
icon_locations = {appname: (170, 212), "Applications": (490, 212)}
