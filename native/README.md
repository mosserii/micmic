# MicMic without Chrome

This folder turns MicMic into a small Mac app that listens all the time, with no
browser window anywhere. It sits in the menu bar as a dot. When it hears **מיקמיק**
it sends what she said next to the MicMic that is already running, exactly as the
web page did.

Nothing else about MicMic changes. This is only a new pair of ears.

```
native/
  MicMic.app/          the app you double-click (built by build.sh, do not move it)
  listener.py          the ears: microphone, speech, wake word, one HTTP POST
  build.sh             rebuilds MicMic.app from scratch, safe to run any time
  com.zohar.micmic.plist   optional: start it automatically at login
  listener.env         optional settings (see the end of this file)
  micmic-listener.log  what it heard and what it did
```

---

## Set it up once

```bash
cd native
./build.sh
open MicMic.app
```

`build.sh` makes the app and installs what Python needs (about 15 seconds, ~30 MB).
`open MicMic.app` starts it.

### The two boxes macOS will show

The first time MicMic.app runs, macOS asks twice. **Both must be allowed** or it
cannot hear anything:

1. **"MicMic would like to access the microphone."** → click **Allow**
2. **"MicMic would like to use speech recognition."** → click **OK**

If you clicked the wrong thing, or the boxes never appeared, you can fix it by hand:

-  **Apple menu → System Settings → Privacy & Security → Microphone** → switch
   **MicMic** on.
-  **Apple menu → System Settings → Privacy & Security → Speech Recognition** →
   switch **MicMic** on.

Answer once and macOS remembers. It does not ask again.

### One more switch, and this one matters for Hebrew

**Apple menu → System Settings → Keyboard → Dictation → turn it On.** Say yes to
the box that appears.

Here is the plain version of why. Apple can turn English speech into words entirely
inside the Mac, with no internet. It cannot do that for Hebrew — there is no Hebrew
model on the machine. Every Hebrew sentence is sent to Apple, turned into words
there, and sent back. That path only works on a Mac where Dictation has been
switched on at least once, because that is where you agree to let Apple do it. Until
you do, Hebrew can come back with an error that just says "Retry" and nothing works,
while English keeps working fine and makes it look like the app is broken.

So: **turn Dictation on, and keep the Mac on the internet.** Hebrew needs both.
(On the Mac this was built on, Hebrew worked — see "What was actually tested".)

---

## Using it

Just open `MicMic.app`. It brings its own server up if one is not already running
(see `MicMic.app/Contents/MacOS/MicMic` — it probes `/api/health` and starts
`python3 -m savta.server` itself before handing off to the listener), so there is
nothing else to run by hand any more.

Then just talk:

> **"מיקמיק, תתקשרי לדנה"**

Say it as one sentence, the wake word and the request together. That works best.

Saying only **"מיקמיק"**, waiting, and then speaking also works: after the wake word
it stays open for about seven seconds and takes the next thing you say whole. You
will hear a short tone when that window opens and another when it closes.

While a message is counting down before it is sent, you do **not** need the wake
word to stop it. Just say **"ביטול"** (or stop, or no, or wait). It is listening for
exactly that during those six seconds.

### Push-to-talk: the way in when the wake word fails

Apple has never heard "מיקמיק" before and mishears it often (see "What was actually
tested" below). When it does, the wake word is not the only door in:

-  **Press the hotkey.** Default is **double-tap the Fn/Globe key** — tap it twice
   quickly, the same gesture as opening Dictation, nothing else on the Mac uses it.
   Override it with `MICMIC_HOTKEY` in `listener.env` (see Settings below), e.g.
   `MICMIC_HOTKEY=cmd+shift+m`.
-  **Or click "Listen now"** in the menu bar dot's menu. Same effect, no keyboard
   needed.

Either way, MicMic listens for one sentence with **no wake word required** — the
same mechanism as the cancel window above — and you will hear the same tones.

A global hotkey needs **Accessibility** permission. If it is missing, MicMic says so
in the menu (an extra line appears under the status line) and in the log, with the
exact path: **Apple menu → System Settings → Privacy & Security → Accessibility →
turn MicMic on.** Until then, "Listen now" in the menu still works with no extra
permission.

Note on the default double-tap-Fn: if **System Settings → Keyboard → Press 🌐 key
to** is set to something other than "Do Nothing" (Dictation and Emoji & Symbols are
common defaults), that system action may also fire alongside MicMic's own. If that
clashes, either set that menu to "Do Nothing" or switch to a `cmd+shift+…` style
`MICMIC_HOTKEY`. Not verified against a real Accessibility-permission dialog or a
real Globe key press on this Mac — see "What was actually tested".

### The dot in the menu bar

Click it to see what MicMic is doing right now — waiting, listening, thinking,
counting down — in her own language, not just in English. The same menu has
**Listen now** (push-to-talk, see above), **Pause listening** (useful when guests
are over), **Open MicMic window**, **Show log**, and **Quit**.

---

## Start it automatically every morning

```bash
cp native/com.zohar.micmic.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.zohar.micmic.plist
```

Now it starts whenever she logs in, and restarts itself if it ever stops. To undo:

```bash
launchctl unload -w ~/Library/LaunchAgents/com.zohar.micmic.plist
```

Grant the microphone and speech permissions by running `open MicMic.app` by hand
first, before turning this on. Permission boxes are easier to answer when you are
expecting them.

launchd runs `MicMic.app/Contents/MacOS/MicMic` directly, the same executable
`open MicMic.app` uses, so the server-autostart above applies here too: a login
brings up both the ears and MicMic itself, with no separate step and no Chrome
window.

---

## Check that it is working

**Is it alive?**

```bash
pgrep -fl native/listener.py
```

A line with a process id means yes. Nothing means it is not running.

**Is it hearing?** Watch the log while you talk to it:

```bash
tail -f native/micmic-listener.log
```

Say "מיקמיק, מה השעה". Within a second or two you should see:

```
→ 'מה השעה'
← look_up  'השעה שלוש ועשרה'
```

`→` is what it sent. `←` is what MicMic answered. If you see `→` and no `←`, the
MicMic server is not running. If you see neither, it did not hear the wake word.

**Did it even start correctly?** The first lines of the log after a launch should read:

```
speech authorization status = 3 (3 = authorized)
microphone access granted = True
audio engine running
```

Anything other than `3` and `True` means one of the two permission switches above is
off.

**Is the wake word getting through?** Turn on the detailed log to see every word
Apple heard:

```bash
echo "MICMIC_DEBUG=1" >> native/listener.env
pkill -f native/listener.py && open native/MicMic.app
tail -f native/micmic-listener.log
```

Every line beginning `heard[part]` is Apple's transcript. Say the wake word a few
times and look at how it is written back. Apple has never heard the word "מיקמיק"
before and often writes something close but not identical — on this machine it wrote
**מיקמק** and once **מיקה**. The listener already allows a one-letter miss, but if
her voice consistently produces some other spelling, add that exact spelling to
`wake_words` in `micmic.config.json` and restart the app. That
is the single highest-value thing to tune, and it takes two minutes.

Remove the `MICMIC_DEBUG=1` line when you are done; it makes the log noisy.

---

## What was actually tested, on this Mac, on 2026-09-21

Everything below was tested by playing speech through the speakers into the built-in
microphone — a synthetic voice, not a person. That matters most for the wake word: a
made-up name is exactly where a synthetic voice and a real one differ most. Assume
the rest carries over and the wake word needs rechecking with her.

**Worked, observed directly:**

-  The app gets both permissions. `speech authorization status = 3`,
   `microphone access granted = True`. A plain `python3 listener.py` cannot — it is
   killed instantly. The bundle is what makes the difference.
-  Microphone capture runs continuously: 48 kHz mono, audio flowing into the
   recognizer the whole time.
-  **Hebrew recognition worked**, and did not produce the `Retry` error the earlier
   research warned about.
-  **The whole path worked against the real MicMic.** Spoken "מיק מיק, מה השעה"
   became `{"text": "מה השעה"}` at `127.0.0.1:8799`, and MicMic answered out loud.
   About two seconds from the end of the sentence to the answer.
-  **English worked too**, at `MICMIC_LOCALE=en-US`: "Mike Mike what time is it" →
   `{"text": "what time is it"}`, "Hey mic mic play some music" →
   `{"text": "play some music"}`.
-  **Cancelling a message works without the wake word.** With a send counting down,
   a bare "ביטול" was picked up and delivered five seconds into the six second
   window. Rule 1 survives the move off Chrome.
-  It ignored the room. Music and English chatter playing nearby produced
   transcripts and no action, because no wake word was in them.
-  Wake word matching: 15 written cases pass, including every spelling Apple actually
   produced, and 18 ordinary Hebrew and English sentences produce no false trigger.
   Run it yourself: `./.venv/bin/python3 listener.py --check`.

**Known, and not solved:**

-  **The wake word is the weak link, by a wide margin.** Apple has never heard
   "מיקמיק" and wrote it back four different ways in one afternoon: `מיקמיק`,
   `מיק מיק`, `מיקמק`, `מיקה`, and in English `Mike Mike` and `Mick`. The listener
   now matches the *shape* — the same mi-k syllable twice — plus every spelling
   observed, and a one-letter miss on the long ones. Three of those six still came
   through as one word (`מיקה`) that nothing can match. **Check this with her voice
   first**, using the debug log above, and add whatever Apple writes for her to
   `wake_words` in `micmic.config.json`. Everything *after* the wake word transcribed
   accurately every time.
-  **One language at a time.** With `he-IL` selected, spoken English came back as
   nothing at all. The listener follows `language_hint` in `micmic.config.json`.
   Apple offers no way to run two languages in one session.
-  **On-device Hebrew changed under us.** At 03:05 the recognizer reported
   `supportsOnDeviceRecognition = False` for `he-IL` and logged "No Assistant asset
   for language he-IL"; by 03:16 the same call returned `True` and recognition
   worked with on-device required. macOS appears to have fetched the Hebrew asset in
   between. Do not rely on Hebrew working offline — it was never tested with the
   network off — but it may now be better than the earlier research suggested.
-  Apple hands back **one sentence per listening session** and then goes quiet, with
   no error and no final result. The listener throws the session away and opens a
   fresh one about two seconds after each sentence ends. In practice: pause briefly
   between commands. Continuous talking is not something it can follow.
-  **Interrupting a long answer is a client-side mitigation, not a real fix.** The
   listener now reopens the mic partway through a long spoken answer and listens
   for a stop word, and stays fully deaf for the whole narration before a countdown
   cancel window opens (previously it could reopen mid-sentence and hear its own
   words as a cancellation). But it still cannot actually kill the `say` process
   that is talking — `mac.say()` in `savta/actions/mac.py` is a fire-and-forget
   `Popen` with no handle kept anywhere — so a stop word goes quiet on our end
   while the machine may keep talking for a moment. A real fix needs that server
   change.
-  **Sleep/wake and audio-device-change recovery is new and unverified against
   real hardware events.** The listener now watches
   `AVAudioEngineConfigurationChangeNotification` and `NSWorkspaceDidWakeNotification`,
   and separately rebuilds the engine if no audio buffers arrive for 6 seconds
   while it should be listening — but nobody has actually closed this Mac's lid
   overnight or unplugged headphones while it was running. Watch the log after a
   real sleep/wake for `"rebuilding audio engine"`.
-  Not tested: running for hours, the LaunchAgent at an actual login, a real human
   voice, and the push-to-talk hotkey against a real Accessibility-permission
   dialog (this Mac already had Accessibility trust from a prior grant to the
   terminal, so the "permission missing" path was checked by reading the API's
   return value directly, not by watching the dialog appear).

---

## Settings (`listener.env`)

Optional. One `NAME=value` per line. An app launched by double-clicking inherits
nothing from a terminal, so this file is the only way to set these.

```
MICMIC_SERVER=http://127.0.0.1:8799   where MicMic is listening
MICMIC_LOCALE=he-IL                   overrides language_hint from micmic.config.json
MICMIC_DEBUG=1                        log every transcript
MICMIC_ON_DEVICE=0                    do not force on-device recognition (English only)
MICMIC_PYTHON=/path/to/python3        a different Python with PyObjC installed
MICMIC_HOTKEY=fn-fn                   push-to-talk hotkey. Default: double-tap Fn/Globe.
                                       Or "mod+mod+letter", e.g. MICMIC_HOTKEY=cmd+shift+m
                                       (mods: cmd, shift, opt/alt, ctrl)
```

Restart the app after changing it.

---

## If something is wrong

**It hears nothing at all.** Check the two permission switches. Then check the first
lines of the log for `status = 3` and `granted = True`.

**Hebrew fails but English works.** Dictation has not been turned on, or the Mac is
offline. Look for `203` in the log.

**It sends the wrong words, or sends when nobody spoke.** Turn on `MICMIC_DEBUG=1`
and read the transcripts. If it is triggering on something that sounds like the wake
word, remove the loose spellings from `wake_words` in `micmic.config.json`.

**It answers itself.** It should not — it goes deaf while it is speaking — but if a
reply ever comes back in as a new command, the volume is high enough that the six
second cancel window is hearing the room. Lower the output volume a little.

**The wake word keeps missing and nothing happens.** Use push-to-talk instead —
double-tap Fn/Globe, or click **Listen now** in the menu — while you fix the wake
word using the debug log above. If the hotkey does nothing and the menu shows
"⚠ Hotkey needs Accessibility permission", grant it at **System Settings > Privacy
& Security > Accessibility** and it starts working with no relaunch needed; the
menu item always works regardless.

**Permission or hardware failures are now spoken out loud, in her language.** A
denied microphone/speech permission, a server that is not answering, or a
microphone that would not start each show an alert dialog and say a sentence via
`say` (Hebrew/Arabic/Russian/English, following `language_hint`) instead of only
changing the menu-bar glyph.

**Start over cleanly.**

```bash
pkill -f native/listener.py
cd native && ./build.sh && open MicMic.app
```

**Do not move or rename `MicMic.app`.** macOS ties the microphone permission to
where the app is. Move it and both boxes come back. `listener.py` lives outside the
bundle exactly so it can be edited without that happening.
