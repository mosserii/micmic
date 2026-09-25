# MicMic.app

This folder turns MicMic into a real Mac app: a menu-bar item, a global hotkey, the
bar that appears near the top of the screen, and a small window for music, video and
Settings. There is no browser anywhere.

```
native/
  build.sh                   builds MicMic.app (dev) or a signed, notarized release
  launcher.c                 the release build's main executable (links libpython)
  listener.py                the ears: hotkey, microphone, Apple speech, one HTTP POST
  bar.py                     the bar: a non-activating panel that never steals focus
  panel.py                   the window: the same web UI in a floating panel
  listener.env               optional settings (see the end of this file)
  com.micmic.listener.plist  optional: start at login
```

The listener knows nothing about the assistant. It turns speech into text and POSTs
it to the local server (`python -m savta.server`, `127.0.0.1:8799`), which owns every
decision. The bar and the window both host `savta/web/index.html`, so there is exactly
one UI.

## Build and run

```bash
uv sync                  # the server's environment, at the repo root
native/build.sh          # builds native/MicMic.app, about 15 seconds the first time
open native/MicMic.app
```

`build.sh` is idempotent. It makes the bundle, installs PyObjC into `native/.venv`,
and ad-hoc signs the result. The app starts its own server if none is running.

`native/build.sh --release` builds a self-contained bundle into `native/dist/` with its
own relocatable Python. `--notarize` and `--dmg` add Apple notarization and a disk
image; they need a Developer ID identity (`--identity=...`) and a `notarytool` keychain
profile.

**Do not move `MicMic.app` after granting permissions.** macOS ties the microphone
permission to where the app is. `listener.py` lives outside the bundle so it can be
edited without macOS asking again.

## Permissions

The first run asks for these. MicMic needs all of them to do its job:

- **Microphone** and **Speech Recognition**: to hear you.
- **Accessibility**: for the global hotkey, and to read and press controls in other
  apps when you ask it to.
- **Screen Recording**: only for questions about your screen that need a picture of
  the window in front.

Fix any of them in **System Settings > Privacy & Security**.

## Talking to it

**Hold right Option**, speak, let go. The bar shows your words as you speak, then
what MicMic did, with Undo when it can be put back. Change the hotkey in Settings,
or pin one with `MICMIC_HOTKEY` in `listener.env`: `right-option`, `right-command`,
`right-control`, `right-shift`, or a combination such as `cmd+shift+m`.

While a message is counting down before it is sent, you do not need the hotkey to
stop it. Say "no", "stop" or "wait".

Speech recognition is Apple's. English runs on the Mac. Some languages, Hebrew among
them, are sent to Apple for recognition and need **Keyboard > Dictation** switched on
once, and an internet connection.

## Check that it is working

```bash
tail -f native/micmic-listener.log
```

The first lines after a launch should read:

```
speech authorization status = 3 (3 = authorized)
microphone access granted = True
audio engine running
```

Then every request shows as `→` (what was sent) and `←` (what MicMic answered). `→`
with no `←` means the server is not answering; see `native/micmic-server.log`.

`native/.venv/bin/python3 native/listener.py --check` runs the listener's own offline
checks (hotkey parsing, the tap detector, wake-word matching) with no microphone.

## Start at login

```bash
sed "s#__MICMIC_NATIVE__#$PWD/native#g" native/com.micmic.listener.plist \
    > ~/Library/LaunchAgents/com.micmic.listener.plist
launchctl load -w ~/Library/LaunchAgents/com.micmic.listener.plist
```

Run from the repo root. Grant the permissions by opening the app by hand once first.
To undo: `launchctl unload -w ~/Library/LaunchAgents/com.micmic.listener.plist`.

## Settings (`listener.env`)

One `NAME=value` per line. An app opened from Finder inherits nothing from a terminal,
so this file is the way to set these. Restart the app after changing it.

```
MICMIC_SERVER=http://127.0.0.1:8799   where the MicMic server listens
MICMIC_HOTKEY=right-option            pins the hotkey, whatever Settings says
MICMIC_LOCALE=en-US                   the speech recognizer's language
MICMIC_ALT_LOCALE=en-US               the second language read after you let go
MICMIC_DEBUG=1                        log every partial transcript
MICMIC_ON_DEVICE=0                    do not require on-device recognition
MICMIC_PYTHON=/path/to/python3        a different Python with PyObjC installed
MICMIC_ALLOW_CALL=1                   allow real phone calls (off by default)
```

## Known limits

- Apple returns one sentence per recognition session, so pause briefly between
  requests.
- Apple runs one recognition language at a time. After you let go, a second
  recognizer (`MICMIC_ALT_LOCALE`, English by default) reads the same audio and the
  more confident transcript wins, so you can speak English or your own language
  without switching anything.
- Stopping a long spoken answer quiets MicMic's side at once, but the system voice can
  finish its current sentence.
