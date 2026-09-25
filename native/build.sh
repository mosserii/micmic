#!/usr/bin/env bash
# Assemble MicMic.app. Idempotent: safe to run as many times as you like.
#
# The bundle is the whole point. macOS will not let a plain python3 process touch
# the microphone or the speech recogniser — it kills it with SIGABRT the moment it
# asks. The same script inside a folder called MicMic.app, with an Info.plist that
# declares why it wants those things, gets the normal permission dialogs instead.
# No Xcode, no py2app, no code signing certificate involved.
#
# listener.py deliberately lives OUTSIDE the bundle. macOS ties a permission grant
# for an unsigned app to the bundle's contents, so editing a file inside the bundle
# can make macOS treat it as a different app and ask for permission all over again.
# Keeping the code outside means you can edit the listener freely.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HERE/MicMic.app"
BUNDLE_ID="com.betterfly.micmic"
VENV="$HERE/.venv"

# dev is the default and behaves exactly as it always has. --release builds the
# self-contained, notarizable bundle into native/dist/ instead; see release_build.
MODE=dev
IDENTITY="-"
NOTARIZE=0
KEYCHAIN_PROFILE="micmic"
BUILD_VERSION=1
SHORT_VERSION=1.0
for a in "$@"; do
  case "$a" in
    --release)            MODE=release ;;
    --notarize)           MODE=release; NOTARIZE=1 ;;
    --dmg)                MODE=release; NOTARIZE=1; DMG=1 ;;
    --identity=*)         IDENTITY="${a#*=}" ;;
    --keychain-profile=*) KEYCHAIN_PROFILE="${a#*=}" ;;
    --version=*)          SHORT_VERSION="${a#*=}" ;;
    -h|--help)            sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done


# Submit to Apple and stop the build unless Apple says Accepted. notarytool --wait
# can exit 0 with "status: Invalid", and a missing keychain profile used to print an
# error and let the build carry on and report success.
notarize() {
  local out rc=0
  # "|| rc=$?": under set -e a failing substitution would end the script before
  # Apple's own words were printed (it exited 69 with nothing to read).
  out=$(xcrun notarytool submit "$1" --keychain-profile "$KEYCHAIN_PROFILE" --wait 2>&1) || rc=$?
  echo "$out" | sed 's/^/    /'
  if [ "$rc" -ne 0 ] || ! echo "$out" | grep -q "status: Accepted"; then
    echo "  (notarytool exit $rc)"
    echo "  FAILED: Apple did not accept $(basename "$1")."
    echo "  If the profile is missing: xcrun notarytool store-credentials $KEYCHAIN_PROFILE"
    exit 1
  fi
}


# ---------------------------------------------------------------- release build
# A notarizable, self-contained MicMic.app, built into native/dist/.
#
# Everything the dev build deliberately keeps OUTSIDE the bundle has to come inside
# for this one. Apple will not notarize a bundle that runs code from elsewhere, and
# a signed bundle is sealed, so writable state moves to Application Support instead
# (savta/paths.py decides that, by noticing it is running from a .app).
#
# The dev build above is untouched by any of this and stays the fast way to work.
release_build() {
  local PROJECT DIST RAPP RES PYSRC PYBIN TS
  PROJECT="$(cd "$HERE/.." && pwd)"
  # MICMIC_DIST: build somewhere else while a copy in dist/ is still running.
  DIST="${MICMIC_DIST:-$HERE/dist}"
  RAPP="$DIST/MicMic.app"
  RES="$RAPP/Contents/Resources"

  command -v uv    >/dev/null || { echo "  uv is required for a release build"; exit 1; }
  command -v clang >/dev/null || { echo "  clang is required — xcode-select --install"; exit 1; }

  echo "release build -> $RAPP"
  echo "  identity:  $IDENTITY"
  rm -rf "$RAPP"; mkdir -p "$RAPP/Contents/MacOS" "$RES/app"

  # 1. the interpreter, relocated into the bundle ------------------------------
  # uv ships python-build-standalone, which is built to be moved. A Homebrew or
  # system python is not, and would leave absolute paths baked into the bundle.
  uv python install 3.11 >/dev/null 2>&1 || true
  PYSRC="$(ls -d "${UV_PYTHON_INSTALL_DIR:-$HOME/.local/share/uv/python}"/cpython-3.11.*-macos-* 2>/dev/null | head -1)"
  [ -n "$PYSRC" ] || { echo "  no uv-managed CPython 3.11 found"; exit 1; }
  echo "  interpreter: ${PYSRC##*/}"
  cp -R "$PYSRC" "$RES/python"
  chmod -R u+w "$RES/python"
  PYBIN="$RES/python/bin/python3.11"
  # Do this BEFORE linking, so clang records @rpath rather than the absolute path
  # of the copy it was built from.
  install_name_tool -id @rpath/libpython3.11.dylib "$RES/python/lib/libpython3.11.dylib"

  # 2. dependencies, flat, straight from the lockfile --------------------------
  echo "  resolving dependencies from uv.lock"
  ( cd "$PROJECT" && uv export --no-hashes --no-dev --no-emit-project -o "$DIST/requirements.txt" ) >/dev/null
  # the listener needs two pyobjc frameworks that the server does not
  printf 'pyobjc-framework-Speech\npyobjc-framework-AVFoundation\npyobjc-framework-WebKit\n' >> "$DIST/requirements.txt"
  uv pip install --quiet --python "$PYBIN" --target "$RES/lib" -r "$DIST/requirements.txt"

  # 3. the app's own code ------------------------------------------------------
  cp -R "$PROJECT/savta" "$RES/app/savta"
  cp "$PROJECT/LICENSE" "$PROJECT/THIRD_PARTY_NOTICES.md" "$RES/"
  # The cloud this build signs devices up on (free requests, then Pro). Only the
  # official release passes one; any other build has none and runs on the builder's
  # own keys (savta/account.py), never on someone else's metered cloud.
  printf '%s\n' "${MICMIC_CLOUD_URL:-}" > "$RES/app/savta/cloud_url.txt"
  if [ -n "${MICMIC_CLOUD_URL:-}" ]; then
    echo "  cloud:       $MICMIC_CLOUD_URL"
  else
    echo "  cloud:       none. This build uses its own keys (TYPESAFE_API_KEY, GEMINI_API_KEY)."
    echo "               The official release sets MICMIC_CLOUD_URL."
  fi
  cp "$HERE/listener.py" "$RES/app/listener.py"
  cp "$HERE/panel.py"    "$RES/app/panel.py"
  cp "$HERE/bar.py"      "$RES/app/bar.py"
  cp "$HERE/../brand/AppIcon.icns" "$RES/AppIcon.icns"
  [ -f "$PROJECT/micmic.config.json" ] && cp "$PROJECT/micmic.config.json" "$RES/app/"
  find "$RES/app" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
  # Nothing personal and no credential travels in a shipped bundle. profile.json,
  # memory.json and trace.jsonl live at the project root and are simply not copied;
  # these two are belt and braces in case someone put one inside the package.
  rm -f "$RES/app/.env.local" "$RES/app/savta/.env.local"

  # 4. the launcher ------------------------------------------------------------
  echo "  compiling launcher.c"
  # macOS 13 and later, as Info.plist says: left to default, clang stamped the
  # launcher with THIS Mac's version (15.0) and the app would not open on 13 or 14.
  clang -O2 -Wall -mmacosx-version-min=13.0 -o "$RAPP/Contents/MacOS/MicMic" "$HERE/launcher.c" \
    -I"$RES/python/include/python3.11" \
    -L"$RES/python/lib" -lpython3.11 \
    -Wl,-rpath,@executable_path/../Resources/python/lib

  # 4b. prune -------------------------------------------------------------
  # Everything below is confirmed unused by a runtime import trace that exercises
  # the server, every action module, and the listener's PyObjC frameworks (see
  # tests/size/test_bundle_contents.py). Nothing here is removed on a guess.
  echo "  pruning unused packages and files"

  # PyObjC's own test suite. 15 MB, never imported by anything shipped.
  rm -rf "$RES/lib/PyObjCTest"
  # Third-party test directories that travel along with a pip install (e.g.
  # greenlet/tests). Not the app's own tests -- those never leave $PROJECT.
  find "$RES/lib" -type d \( -name "tests" -o -name "test" \) -prune -exec rm -rf {} + \
    2>/dev/null || true

  # Playwright's bundled Node runtime is the single biggest thing in the bundle
  # (~116 MB) for a feature (driving a real browser) most sessions never touch.
  # Keep driver/package -- the JS driver itself, ~13 MB, and what actually talks
  # to the browser once a node binary exists. savta/actions/web.py fetches a
  # matching official node build into Application Support the first time a web
  # task actually runs, verified against a pinned sha256
  # (ensure_playwright_driver()), and points PLAYWRIGHT_NODEJS_PATH at it.
  rm -f "$RES/lib/playwright/driver/node"

  # The relocated interpreter carries its own pip/setuptools (for a `pip install`
  # nobody runs from inside a shipped app) and stdlib pieces this menu-bar app
  # has no path to: a GUI toolkit — Tk is not even wired up, no _tkinter.so ships
  # in this interpreter at all — the 2-to-3 porter, ensurepip's bundled wheels,
  # the interactive help system's data tables, and the deprecated distutils.
  rm -rf "$RES/python/lib/python3.11/site-packages/pip" \
         "$RES/python/lib/python3.11/site-packages/setuptools" \
         "$RES/python/lib/python3.11/site-packages/pkg_resources" \
         "$RES/python/lib/python3.11/site-packages/_distutils_hack" \
         "$RES/python/lib/python3.11/site-packages"/pip-*.dist-info \
         "$RES/python/lib/python3.11/site-packages"/setuptools-*.dist-info \
         "$RES/python/lib/python3.11/site-packages/distutils-precedence.pth"
  rm -rf "$RES/python/lib/tcl8.6" "$RES/python/lib/tk8.6" "$RES/python/lib/tcl8" \
         "$RES/python/lib/itcl4.2.4" "$RES/python/lib/thread2.8.9"
  rm -rf "$RES/python/lib/python3.11/tkinter" \
         "$RES/python/lib/python3.11/turtledemo" \
         "$RES/python/lib/python3.11/turtle.py" \
         "$RES/python/lib/python3.11/idlelib" \
         "$RES/python/lib/python3.11/lib2to3" \
         "$RES/python/lib/python3.11/ensurepip" \
         "$RES/python/lib/python3.11/pydoc_data" \
         "$RES/python/lib/python3.11/distutils" \
         "$RES/python/lib/python3.11/config-3.11-darwin"
  # C headers were only needed to compile the launcher just above; nothing at
  # runtime links against them.
  rm -rf "$RES/python/include"

  # 5. Info.plist --------------------------------------------------------------
  cat > "$RAPP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>MicMic</string>
  <key>CFBundleDisplayName</key><string>MicMic</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleExecutable</key><string>MicMic</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>CFBundleVersion</key><string>$BUILD_VERSION</string>
  <key>CFBundleShortVersionString</key><string>$SHORT_VERSION</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>MicMic listens for your voice so you can talk to your computer instead of typing.</string>
  <key>NSSpeechRecognitionUsageDescription</key><string>MicMic turns what you say into words so it can do what you ask.</string>
  <key>NSAppleEventsUsageDescription</key><string>MicMic sends your messages and opens your apps for you.</string>
</dict></plist>
PLIST

  # 6. entitlements ------------------------------------------------------------
  # disable-library-validation: PyObjC dlopens ~170 extension modules, and without
  # this the hardened runtime refuses every one of them.
  cat > "$DIST/entitlements.plist" <<'ENT'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>com.apple.security.cs.disable-library-validation</key><true/>
  <key>com.apple.security.device.audio-input</key><true/>
  <key>com.apple.security.automation.apple-events</key><true/>
</dict></plist>
ENT

  # 7. sign, inside out --------------------------------------------------------
  # Nested code must be sealed before the thing that contains it, or the outer
  # signature seals a hash that is about to change.
  TS=(--timestamp); [ "$IDENTITY" = "-" ] && TS=(--timestamp=none)
  # codesign says "replacing existing signature" on stderr for almost every file,
  # which is noise. Swallowing ALL of stderr to hide it also hid the one line that
  # matters when a signature genuinely fails, so filter by content, never wholesale.
  _sign() {
    local out rc
    out="$(codesign --force --options runtime "${TS[@]}" \
             --entitlements "$DIST/entitlements.plist" --sign "$IDENTITY" "$@" 2>&1)"; rc=$?
    if [ $rc -ne 0 ]; then
      echo "  codesign FAILED: $*" >&2
      printf '%s\n' "$out" | sed 's/^/    /' >&2
      return $rc
    fi
    printf '%s\n' "$out" | grep -v "replacing existing signature" | grep . | sed 's/^/    /' || true
  }

  # Process substitution, not a pipe: a `while read` on the right of a pipe runs in a
  # subshell, where a failed signature could not stop the build and we would notarize
  # a bundle we had already failed to sign.
  local n f
  n="$(find "$RAPP" \( -name '*.so' -o -name '*.dylib' \) | wc -l | tr -d ' ')"
  echo "  signing $n native libraries"
  while IFS= read -r -d '' f; do
    _sign "$f"
  done < <(find "$RAPP" \( -name '*.so' -o -name '*.dylib' \) -print0)
  echo "  signing bundled executables"
  while IFS= read -r -d '' f; do
    if file "$f" | grep -q Mach-O; then _sign "$f"; fi
  done < <(find "$RAPP/Contents/Resources" -type f -perm -u+x -print0)
  _sign "$RAPP/Contents/MacOS/MicMic"
  _sign "$RAPP"

  echo "  checking every binary runs on macOS 13"
  too_new=$(find "$RAPP/Contents" -type f \( -perm -111 -o -name "*.so" -o -name "*.dylib" \) \
    -exec sh -c 'v=$(otool -l "$1" 2>/dev/null | awk "/LC_BUILD_VERSION/{f=1} f&&/minos/{print \$2; exit}"); [ -n "$v" ] && [ "${v%%.*}" -gt 13 ] && echo "$1 ($v)"' _ {} \;)
  if [ -n "$too_new" ]; then
    echo "  FAILED: these need a newer macOS than the 13.0 Info.plist promises:"; echo "$too_new" | sed 's/^/    /'
    exit 1
  fi

  # Under the hardened runtime a missing entitlement is silent: no microphone, no
  # messages, and nothing in the log. Refuse to ship a bundle without them.
  ents="$(codesign -d --entitlements - --xml "$RAPP" 2>/dev/null)"
  for e in device.audio-input automation.apple-events cs.disable-library-validation; do
    echo "$ents" | grep -q "com.apple.security.$e" \
      || { echo "  FAILED: the app is missing the com.apple.security.$e entitlement"; exit 1; }
  done
  echo "  entitlements: microphone, Apple Events, library validation"

  echo "  verifying"
  codesign --verify --deep --strict --verbose=2 "$RAPP" 2>&1 | sed 's/^/    /'
  codesign -d --verbose=2 "$RAPP" 2>&1 | grep -E 'Identifier|flags' | sed 's/^/    /'

  # 8. notarize ----------------------------------------------------------------
  if [ "$NOTARIZE" = "1" ]; then
    [ "$IDENTITY" = "-" ] && { echo "  cannot notarize an ad-hoc signature — pass --identity"; exit 1; }
    echo "  submitting to Apple (this takes a few minutes)"
    ditto -c -k --keepParent "$RAPP" "$DIST/MicMic.zip"
    notarize "$DIST/MicMic.zip"
    xcrun stapler staple "$RAPP" || { echo "  FAILED: stapling the app"; exit 1; }
    xcrun stapler validate "$RAPP" && echo "  stapled"
    spctl -a -vvv -t exec "$RAPP" 2>&1 | sed 's/^/    /'
  else
    echo "  not notarized (pass --notarize once a Developer ID identity is in place)"
  fi

  # 9. the download ------------------------------------------------------------
  # What people actually get from the website: a disk image with the app and an
  # Applications shortcut to drag it onto. The image is signed, notarized and stapled
  # on its own too, so Gatekeeper is satisfied offline before the app is even opened.
  if [ "${DMG:-0}" = "1" ]; then
    echo "  building the disk image"
    command -v uvx >/dev/null || { echo "  FAILED: the disk image needs uv (uvx runs dmgbuild)"; exit 1; }
    rm -f "$DIST/MicMic.dmg"
    # The window it opens to (background, icon places, volume icon) is native/dmg/.
    # dmgbuild writes Finder's view settings straight into the image, so there is no
    # Finder scripting, which needs Automation permission and races the mount.
    # ULFO (lzfse) over UDZO (zlib): measured on the ad-hoc build, UDZO 24 MB vs
    # ULFO 21 MB vs ULMO (lzma) 16 MB. ULMO's extra few MB cost nearly 3x the copy
    # time out of the mounted image (lzma has no cheap random-access decompression)
    # and 4-6x the build time; ULFO copies and mounts the same as UDZO for less
    # size, with no downside worth paying for. lzfse has worked in hdiutil since
    # OS X 10.11, well under this app's macOS 13 floor. Format and HFS+ are set in
    # dmg/dmgbuild-settings.py.
    uvx --from dmgbuild==1.6.7 dmgbuild -s "$HERE/dmg/dmgbuild-settings.py" \
      -D app="$RAPP" "MicMic" "$DIST/MicMic.dmg" >/dev/null
    # dmgbuild copies the app in with ditto and exits 0 even when that copy fails
    # (seen: "Operation not permitted" on /Volumes/MicMic 1/MicMic.app), leaving an
    # image with no app that Apple would notarize all the same. Open it somewhere
    # private and check the app is in it, whole and still validly signed.
    MNT="$(mktemp -d)"
    hdiutil attach -nobrowse -readonly -mountpoint "$MNT" "$DIST/MicMic.dmg" >/dev/null
    dmg_ok=1
    codesign --verify --deep --strict "$MNT/MicMic.app" 2>&1 | sed 's/^/    /' || dmg_ok=0
    [ -L "$MNT/Applications" ] && [ -f "$MNT/.background.tiff" ] || dmg_ok=0
    hdiutil detach "$MNT" >/dev/null && rmdir "$MNT"
    [ "$dmg_ok" = 1 ] || { echo "  FAILED: the disk image does not hold a valid MicMic.app, Applications and its background"; exit 1; }
    echo "  disk image holds the signed app"
    codesign --force --sign "$IDENTITY" --timestamp "$DIST/MicMic.dmg"
    notarize "$DIST/MicMic.dmg"
    xcrun stapler staple "$DIST/MicMic.dmg" || { echo "  FAILED: stapling the disk image"; exit 1; }
    xcrun stapler validate "$DIST/MicMic.dmg" && echo "  disk image stapled"
    spctl -a -vvv -t open --context context:primary-signature "$DIST/MicMic.dmg" 2>&1 | sed 's/^/    /'
    shasum -a 256 "$DIST/MicMic.dmg" | sed 's/^/    sha256 /'
    echo "  $DIST/MicMic.dmg  ($(du -sh "$DIST/MicMic.dmg" | cut -f1))"
  fi

  echo
  echo "done.  $RAPP  ($(du -sh "$RAPP" | cut -f1))"
}

if [ "$MODE" = "release" ]; then
  release_build
  exit 0
fi

echo "building $APP"

# ---------------------------------------------------------------- 1. python env
if [ ! -x "$VENV/bin/python3" ]; then
  echo "  creating venv at $VENV"
  if command -v uv >/dev/null 2>&1; then
    uv venv "$VENV" --python 3.11
  else
    python3 -m venv "$VENV"
  fi
fi

if ! "$VENV/bin/python3" -c "import Speech, AVFoundation" >/dev/null 2>&1; then
  echo "  installing pyobjc (Speech + AVFoundation)"
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$VENV/bin/python3" \
      pyobjc-framework-Speech pyobjc-framework-AVFoundation
  else
    "$VENV/bin/python3" -m pip install --quiet \
      pyobjc-framework-Speech pyobjc-framework-AVFoundation
  fi
fi
"$VENV/bin/python3" -c "import Speech, AVFoundation" \
  || { echo "  pyobjc is not importable — stopping"; exit 1; }
echo "  python ok: $VENV/bin/python3"

# ---------------------------------------------------------------- 2. bundle tree
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# Write a file only when its contents actually change. Rewriting an identical file
# still changes the bundle's modification time, and an unsigned bundle that looks
# modified can lose its microphone permission.
write_if_changed() {   # write_if_changed <path> <<< contents on stdin
  local dest="$1" tmp
  tmp="$(mktemp)"
  cat > "$tmp"
  if [ -f "$dest" ] && cmp -s "$tmp" "$dest"; then
    rm -f "$tmp"
    echo "  unchanged: ${dest#$HERE/}"
    return 1
  fi
  mv "$tmp" "$dest"
  echo "  wrote:     ${dest#$HERE/}"
  return 0
}

CHANGED=0

ICON_SRC="$HERE/../brand/AppIcon.icns"
if [ -f "$ICON_SRC" ] && ! cmp -s "$ICON_SRC" "$APP/Contents/Resources/AppIcon.icns"; then
  cp "$ICON_SRC" "$APP/Contents/Resources/AppIcon.icns"
  echo "  wrote:     MicMic.app/Contents/Resources/AppIcon.icns"
  CHANGED=1
fi

write_if_changed "$APP/Contents/Info.plist" <<PLIST && CHANGED=1
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>                     <string>MicMic</string>
  <key>CFBundleDisplayName</key>              <string>MicMic</string>
  <key>CFBundleIdentifier</key>               <string>$BUNDLE_ID</string>
  <key>CFBundleExecutable</key>               <string>MicMic</string>
  <key>CFBundleIconFile</key>                 <string>AppIcon</string>
  <key>CFBundlePackageType</key>              <string>APPL</string>
  <key>CFBundleShortVersionString</key>       <string>1.0</string>
  <key>CFBundleVersion</key>                  <string>1</string>
  <key>CFBundleInfoDictionaryVersion</key>    <string>6.0</string>
  <key>LSMinimumSystemVersion</key>           <string>13.0</string>

  <!-- No Dock icon, no window: MicMic lives in the menu bar. -->
  <key>LSUIElement</key>                      <true/>

  <!-- These two strings are the reason the bundle exists. macOS shows them in the
       permission dialogs, and refuses to let the process ask at all without them. -->
  <key>NSMicrophoneUsageDescription</key>
  <string>MicMic listens for your voice so you can talk to your computer instead of typing.</string>
  <key>NSSpeechRecognitionUsageDescription</key>
  <string>MicMic turns what you say into words so it can do what you ask.</string>
</dict>
</plist>
PLIST

write_if_changed "$APP/Contents/MacOS/MicMic" <<'LAUNCHER' && CHANGED=1
#!/bin/bash
# MicMic.app's executable. Its only job is to find a python that has PyObjC and
# hand control to ../../../listener.py, which sits outside the bundle on purpose.
set -u
HERE="$(cd "$(dirname "$0")/../../.." && pwd)"   # .../savta/native
PROJECT="$(cd "$HERE/.." && pwd)"                # .../savta
LOG="$HERE/micmic-listener.log"

# Optional settings file. A double-clicked app inherits none of your terminal's
# environment, so this is the only place to put MICMIC_LOCALE or MICMIC_SERVER.
if [ -f "$HERE/listener.env" ]; then
  set -a; . "$HERE/listener.env"; set +a
fi

pick_python() {
  local candidates=()
  [ -n "${MICMIC_PYTHON:-}" ] && candidates+=("$MICMIC_PYTHON")
  candidates+=("$HERE/.venv/bin/python3" "$PROJECT/.venv/bin/python3")
  for p in "${candidates[@]}"; do
    if [ -x "$p" ] && "$p" -c "import Speech, AVFoundation" >/dev/null 2>&1; then
      echo "$p"; return 0
    fi
  done
  return 1
}

PY="$(pick_python)" || {
  echo "$(date '+%F %T')  no python with pyobjc found. Run native/build.sh." >> "$LOG"
  osascript -e 'display alert "MicMic cannot start" message "Run native/build.sh once, then open MicMic again."' >/dev/null 2>&1
  exit 1
}

# The listener is only a microphone: every answer comes from the MicMic server. Opening
# the app with no server running produced total silence, and there is no way for a user
# to tell that from a broken microphone. So the app brings its own server up.
PORT="${MICMIC_PORT:-8799}"
SERVER_LOG="$HERE/micmic-server.log"
if ! curl -sf -m 2 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
  echo "$(date '+%F %T')  no server on $PORT — starting one" >> "$LOG"
  # The server needs the project root on the path and its own python; it does not need
  # pyobjc, so prefer the project venv and fall back to whatever python3 is around.
  SRV_PY="$PROJECT/.venv/bin/python3"
  [ -x "$SRV_PY" ] || SRV_PY="$(command -v python3)"
  ( cd "$PROJECT" && nohup "$SRV_PY" -m savta.server >> "$SERVER_LOG" 2>&1 & )
  for _ in $(seq 1 40); do
    curl -sf -m 2 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && break
    sleep 0.25
  done
  if curl -sf -m 2 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    echo "$(date '+%F %T')  server is up on $PORT" >> "$LOG"
  else
    echo "$(date '+%F %T')  server did not come up; see $SERVER_LOG" >> "$LOG"
    osascript -e 'display alert "MicMic cannot reach its server" message "The listener will keep running, but nothing will answer until the server starts. See native/micmic-server.log."' >/dev/null 2>&1
  fi
fi

cd "$HERE"
exec "$PY" -u "$HERE/listener.py" >> "$LOG" 2>&1
LAUNCHER

chmod +x "$APP/Contents/MacOS/MicMic"
[ -f "$HERE/listener.py" ] || { echo "  listener.py is missing"; exit 1; }

# ---------------------------------------------------------------- 3. register
if [ "$CHANGED" = "1" ]; then
  # An ad-hoc signature gives the bundle a stable identity for the permission
  # system. Harmless if codesign is unavailable.
  codesign --force --sign - "$APP" >/dev/null 2>&1 \
    && echo "  ad-hoc signed" || echo "  not signed (fine — it still works)"
  /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
    -f "$APP" >/dev/null 2>&1 || true
fi

echo
echo "done.  open $APP"
echo "       tail -f $HERE/micmic-listener.log"
