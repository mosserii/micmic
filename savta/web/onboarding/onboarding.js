/* MicMic's first-run onboarding: two short screens inside the app's own window.

   index.html loads this only in ?panel=1 and only when /api/config says first_run;
   it then shows only if /api/onboarding agrees (not finished or skipped yet). It uses
   the page's own globals ($, t, LANG, CFG, closeLang) and its COPY table, adds no
   element with an id, and takes everything it added away again when it closes, so the
   window afterwards is exactly the window without it.

     0  Only when MicMic runs from the disk image (or a translocated copy): "Move MicMic
        to Applications", with Move and reopen, and Not now (savta/install_place.py).
        Set aside unless MICMIC_MOVE_SCREEN=1 (the disk image window already says it).
     1  Four questions, "1 of 4" to "4 of 4", one at a time in one frame: the language
        she will speak (choosing it starts its local model's download), then Microphone,
        Speech Recognition and Accessibility, each with one line saying why, one button
        that asks macOS for exactly that one (nothing is asked before she clicks), and
        "Not now". Its answer is polled from the server. Nothing here names a key: the
        owner's 1.2.4 fresh install read a "Hold the key and talk" demo shown before the
        microphone was allowed as "try it now", pressed it, and nothing happened.
        When the copy on the disk image just put this one in Applications by itself
        (savta/install_place.relocate_at_launch), the language screen says so in one
        small line: "MicMic moved itself to Applications."
        Under the language screen's Continue, one line: "By continuing, you agree to the
        Terms of Use and the Privacy Policy." Each name is a button that opens that page
        in her browser (POST /api/onboarding/legal {open}); the window never navigates.
        Continuing records her agreement ({action:"accept"}; Done and Skip record it on
        the server too). Not a screen of its own: the owner wants fewer screens.
     2  Try it for real, and how: hold her key (or tap it) and say "What time is it?".
        Done when the server sees the next utterance. Without the microphone or speech,
        it says what still works (typing) instead. Without Accessibility the key cannot
        reach MicMic, so it leads with the circle and names no key. When another
        dictation app (Handy) already uses MicMic's key, one line says so and offers a
        free one (savta/dictation_apps.py). */
(() => {
  if(window.__onboarding) return;
  window.__onboarding = true;

  const STEP_KEY = "micmic.onboarding.step", PERM_KEY = "micmic.onboarding.perm";
  const MOVE_KEY = "micmic.onboarding.move";
  const REDUCE = matchMedia("(prefers-reduced-motion: reduce)");
  const SPEECH = Object.fromEntries(LANGS.map(l => [l.code, l.speech]));   // index.html's list
  // Step 1, in this order, one question each. `allow` is the button that asks for a
  // permission; `asking` is the line while macOS's own dialog (or, for Accessibility,
  // its pane) is up. The language is first, and is answered by choosing one.
  const PERMS = [
    {k:"microphone",    label:"obMic",    why:"obMicWhy",    icon:"mic",  allow:"obAllowMic",    asking:"obAsked"},
    {k:"speech",        label:"obSpeech", why:"obSpeechWhy", icon:"wave", allow:"obAllowSpeech", asking:"obAsked"},
    {k:"accessibility", label:"obAx",     why:"obAxWhy",     icon:"ax",   allow:"obAllowAx",     asking:"obAxAsked"},
  ];
  const SETUP = [{k:"language", label:"obLangPick", why:"obLangWhy", icon:"globe"}, ...PERMS];
  const SVG = {
    // The REAL brand mark, unmodified (source: brand/menubar/orb-mic.svg, kept
    // in sync by hand): the exact paths from brand/micmic-mark.svg, wrapped in
    // an outer transform mapping them into this 24x24 box. Same bounding box
    // as the mic glyph it replaces, so every rule sizing/coloring
    // .ob-mcore/.ob-ocore below keeps working unchanged.
    mic:'<svg viewBox="0 0 24 24" aria-hidden="true" fill="currentColor"><g transform="translate(-3.604362,-3.597242) scale(0.01523844)"><g transform="translate(0.000000,2048.000000) scale(0.100000,-0.100000)"><path d="M8120 16399 c-247 -32 -522 -135 -729 -273 -387 -259 -651 -656 -752 -1131 l-24 -110 0 -2095 0 -2095 23 -108 c51 -242 158 -493 289 -677 83 -116 250 -291 358 -375 204 -159 442 -270 705 -327 93 -20 132 -22 335 -22 199 0 243 3 329 22 460 102 838 359 1098 749 118 176 217 421 260 647 22 111 22 115 26 2106 3 2073 3 2107 -39 2308 -167 812 -882 1399 -1694 1391 -66 -1 -149 -5 -185 -10z"/><path d="M11960 16400 c-495 -68 -937 -343 -1211 -752 -171 -257 -267 -525 -299 -837 -14 -141 -14 -3884 0 -4036 29 -299 121 -571 273 -803 263 -401 642 -663 1107 -764 92 -19 133 -22 330 -22 252 0 336 12 539 80 653 217 1131 843 1169 1531 4 66 6 1000 6 2075 -1 1802 -3 1961 -19 2057 -46 282 -153 539 -318 771 -77 107 -266 299 -375 378 -203 149 -436 251 -691 303 -111 22 -406 33 -511 19z"/><path d="M5275 13021 c-150 -54 -249 -174 -275 -333 -13 -83 -13 -1862 0 -2063 27 -400 122 -745 306 -1107 489 -962 1578 -1749 2964 -2143 405 -115 920 -209 1313 -240 l87 -7 0 -1114 0 -1113 -777 -3 -778 -3 -65 -23 c-176 -64 -308 -193 -378 -370 -24 -61 -26 -82 -30 -244 l-4 -178 2601 0 2601 0 0 150 c0 180 -9 229 -63 339 -76 156 -214 268 -383 311 -74 19 -113 20 -831 20 l-753 0 0 1115 c0 1114 0 1115 20 1115 106 0 558 60 793 106 1223 236 2275 764 2982 1495 274 283 509 627 647 947 137 318 205 605 228 958 14 221 13 1992 -1 2069 -24 131 -109 242 -227 298 l-67 32 -502 3 -503 3 0 -411 0 -410 235 0 235 0 0 -784 c0 -858 -2 -905 -59 -1127 -69 -274 -224 -574 -427 -829 -95 -120 -349 -370 -479 -474 -143 -114 -415 -294 -585 -388 -634 -352 -1405 -578 -2260 -665 -247 -25 -919 -25 -1170 0 -554 55 -1088 170 -1549 333 -645 230 -1237 591 -1622 991 -312 325 -516 672 -610 1042 -56 221 -59 269 -59 1122 l0 779 235 0 235 0 0 410 0 410 -487 0 c-445 -1 -492 -2 -538 -19z"/></g></g></svg>',
    wave:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4.5 10v4M8.25 7v10M12 3.5v17M15.75 7v10M19.5 10v4" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round"/></svg>',
    ax:'<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="4.4" r="2.2"/><path d="M4.1 8.4a1.1 1.1 0 0 1 1.3-.9l6.6 1.3 6.6-1.3a1.1 1.1 0 1 1 .4 2.2l-4.9 1v3.2l2 6.4a1.1 1.1 0 0 1-2.1.7L12 15.4l-2 5.6a1.1 1.1 0 0 1-2.1-.7l2-6.4v-3.2l-4.9-1a1.1 1.1 0 0 1-.9-1.3z"/></svg>',
    check:'<svg viewBox="0 0 24 24" aria-hidden="true"><path class="ck" d="M5 12.6l4.3 4.3L19 7.2"/></svg>',
    globe:'<svg viewBox="0 0 24 24" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M3.4 9h17.2M3.4 15h17.2M12 3c-2.7 2.5-4 5.5-4 9s1.3 6.5 4 9c2.7-2.5 4-5.5 4-9s-1.3-6.5-4-9z"/></g></svg>',
    // the Applications folder: a folder with the "A" of its Finder icon
    folder:'<svg viewBox="0 0 24 24" aria-hidden="true"><path class="fo" d="M2.5 6.2c0-1.1.9-2 2-2h4.6c.6 0 1.1.3 1.5.7l1.3 1.5h7.6c1.1 0 2 .9 2 2v9.4c0 1.1-.9 2-2 2h-15c-1.1 0-2-.9-2-2z"/><path class="fa" d="M9.2 16.6 12 9.6l2.8 7M10.3 14h3.4"/></svg>',
    arrow:'<svg viewBox="0 0 24 12" aria-hidden="true"><path d="M1 6h19M15 1.5 20.5 6 15 10.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  };

  let root = null, step = 0, perms = null, succeeded = false, closing = false;
  let permTimer = null, turnTimer = null, advanceTimer = null, closeTimer = null;
  // Step 1's own position (0 the language, then 1, 2, 3: which permission is on screen),
  // and which ones she has clicked for on this run: until she clicks, nothing is asked.
  let pi = 0, asked = {}, pentered = false;
  let orig = {};
  // The local model for her language (savta/asr_models.py): downloaded in the
  // background from the moment a language is chosen. asrTimer re-schedules itself
  // only while it is still downloading, from wherever go() last called pollAsr().
  let asrStatus = null, asrTimer = null, asrInstalled = false, asrAt = 0, asrBusy = false;
  // Step 0: whether this copy runs from the disk image, and how the move is going
  // ("", "moving", "moved", "failed"). Step 2: the dictation app on her key, if any.
  let move = {needed:false}, moveState = "", clash = null, clashOpen = false;
  // This copy was just put in Applications by the one on the disk image, silently
  // (savta/install_place.relocate_at_launch): one small line on the first screen.
  let movedItself = false;
  const MOVED_ITSELF = {en:"MicMic moved itself to Applications.",
                        he:"MicMic עברה בעצמה לתיקיית היישומים.",
                        ar:"MicMic انتقل بنفسه إلى مجلد التطبيقات.",
                        ru:"MicMic сам переместился в «Программы»."};
  // Keyboard or pointer, whichever she used last: a focus ring painted on a button she
  // never tabbed to reads as a second border, so focus follows the keyboard only.
  let byKey = false;
  const q = sel => root.querySelector(sel);
  const qa = sel => [...root.querySelectorAll(sel)];
  const post = (url, body) => fetch(url, {method:"POST",
    headers:{"Content-Type":"application/json"}, body:JSON.stringify(body || {})});

  // ------------------------------------------------------------------ markup
  function sparks(){
    const colours = ["var(--live)", "var(--blob-a)", "var(--blob-b)", "var(--blob-c)", "var(--warm)"];
    let out = "";
    for(let i = 0; i < 14; i++){
      const a = Math.round(i * (360 / 14) + (i % 2 ? 9 : -6));
      const d = 86 + (i * 37 % 5) * 9;
      out += `<i style="--a:${a}deg;--d:${d}px;--c:${colours[i % colours.length]};--t:${(i % 4) * 30}ms"></i>`;
    }
    return out;
  }

  function markup(){
    return `
    <div class="ob-top"></div>
    <div class="ob-steps">
      <section class="ob-step" data-n="0">
        <div class="ob-hero">
          <div class="ob-movepic" aria-hidden="true">
            <span class="ob-mapp">${SVG.mic}</span>
            <span class="ob-marrow">${SVG.arrow}</span>
            <span class="ob-mfolder">${SVG.folder}</span>
          </div>
        </div>
        <h2 class="ob-title" tabindex="-1" data-t="obMoveTitle"></h2>
        <p class="ob-sub ob-movesub" data-t="obMoveSub"></p>
        <p class="ob-pdo"><button type="button" class="ob-movego" data-t="obMoveGo"></button></p>
        <p class="ob-hint ob-movefail" hidden data-t="obMoveFail"></p>
      </section>

      <section class="ob-step" data-n="1">
        <div class="ob-hero">
          <div class="ob-pcard" data-k="language" data-s="ask" role="img">
            <span class="ob-pring" aria-hidden="true"></span>
            ${SETUP.map(p => `<span class="ob-pglyph" data-k="${p.k}" aria-hidden="true">${SVG[p.icon]}</span>`).join("")}
            <span class="ob-ptick" aria-hidden="true">${SVG.check}</span>
          </div>
        </div>
        <p class="ob-pof"><span class="ob-pdots" aria-hidden="true">${SETUP.map((p, i) =>
          `<i data-k="${p.k}">${i + 1}</i>`).join("")}</span><span class="ob-pofn" data-t="obPermOf"></span></p>
        <h2 class="ob-title" tabindex="-1" data-t="obLangPick"></h2>
        <p class="ob-sub" data-t="obLangWhy"></p>
        <div class="ob-pdo">
          <div class="ob-lopts" role="radiogroup" data-aria="obLangPick">${LANGS.map(l => `
            <button type="button" role="radio" class="ob-lopt" data-code="${l.code}" aria-checked="false">${l.name}</button>`).join("")}
          </div>
          <button type="button" class="ob-allow" data-t="obAllowMic" hidden></button>
        </div>
        <p class="ob-asr" hidden></p>
        <p class="ob-movednote" hidden></p>
      </section>

      <section class="ob-step" data-n="2">
        <div class="ob-hero">
          <button type="button" class="ob-orb" data-aria="obListen">
            <span class="ob-halo"></span><span class="ob-rip"></span><span class="ob-rip"></span>
            <span class="ob-shell"></span><span class="ob-oring"></span>
            <span class="ob-ocore">${SVG.mic}</span>
            <span class="ob-ocheck">${SVG.check}</span>
            <span class="ob-sparks">${sparks()}</span>
            <span class="ob-orbt" data-t="obClickTalk"></span>
          </button>
        </div>
        <p class="ob-asr" hidden></p>
        <h2 class="ob-title" tabindex="-1" data-t="ob3Title"></h2>
        <p class="ob-sub" data-t="ob3Sub"></p>
        <p class="ob-quote"><span class="ob-qcheck">${SVG.check}</span><span class="ob-qtext" data-t="ob3Phrase"></span></p>
        <p class="ob-hint ob-howto" hidden data-t="obHow"></p>
        <p class="ob-hint ob-nokey" hidden><span class="ob-bkey" style="display:none"></span><span data-t="obKeyNeedsAx"></span>
          <button type="button" class="ob-on ob-axon" data-pane="accessibility" data-t="obTurnOn"></button></p>
        <div class="ob-clash" hidden>
          <p class="ob-clash-t"></p>
          <p class="ob-clash-do"><button type="button" class="ob-on ob-clash-use"></button>
            <button type="button" class="ob-on ob-clash-other" data-t="obClashOther"></button></p>
          <p class="ob-clash-keys" hidden></p>
        </div>
        <p class="ob-hint ob-nohear" hidden></p>
      </section>
    </div>
    <div class="ob-foot">
      <button type="button" class="ob-skip" data-t="obSkip"></button>
      <span class="ob-act">
        <button type="button" class="ob-next" data-go="next" data-t="obNext" hidden></button>
        <button type="button" class="ob-ghost" data-go="later" data-t="obNotNow" hidden></button>
        <span class="ob-pill" hidden><i></i><span data-t="obWaiting"></span></span>
        <button type="button" class="ob-next" data-go="done" data-t="obDone" hidden></button>
      </span>
    </div>
    <p class="ob-legal"></p>
    <p class="ob-sr" aria-live="polite"></p>`;
  }

  // ------------------------------------------------------------------ copy
  // The copy is written for ⌥, the shortcut a new Mac starts with. Anything else she
  // has picked is the key these screens name instead.
  // Fn is a double tap and a combination is a press, not a key held on the right, so
  // those get their own sentences.
  const TOGGLE = {
    fn:    {ob3Sub:"ob3SubFn", obHow:"obHowFn"},
    combo: {ob3Sub:"ob3SubPress", obHow:"obHowPress"},
  };
  function say(k){
    const spec = window.hotkeySpec ? window.hotkeySpec() : "right-option";
    const kind = spec === "fn-fn" ? "fn" : /^right-/.test(spec) ? null : "combo";
    if(kind && k in TOGGLE[kind]) k = TOGGLE[kind][k];
    const s = t(k), g = glyph();
    return g === "⌥" || typeof s !== "string" ? s : s.split("⌥").join(g);
  }
  function glyph(){ return window.hotkeyGlyph ? window.hotkeyGlyph() : "⌥"; }

  // A line that names her key, with the key drawn as a key: "Hold [⌘] and say".
  // The key itself always reads left to right (⌘⇧M), whatever the language.
  function keyed(el, text){
    const g = glyph(), i = typeof text === "string" ? text.indexOf(g) : -1;
    if(i < 0){ el.textContent = text; return; }
    const cap = document.createElement("kbd");
    cap.className = "ob-kcap";
    cap.textContent = g.replace(/[\u2066-\u2069]/g, "");
    el.replaceChildren(text.slice(0, i), cap, text.slice(i + g.length));
  }

  function paint(){
    if(!root) return;
    for(const el of qa("[data-t]")) el.textContent = say(el.dataset.t);
    for(const el of qa("[data-aria]")) el.setAttribute("aria-label", t(el.dataset.aria));
    root.setAttribute("aria-label", t("obDialog"));
    paintPerms();
    paintLangPicks();
    paintAsrLine();
    paintMove();
    paintLegal();
  }
  window.onboardingLanguage = paint;

  // ------------------------------------------------------------------ 1a. the Terms
  // One line under Continue on the language screen, the two names as link buttons.
  // Kept in the layout on the other screens (only made invisible), so nothing above
  // it moves when she goes on.
  function paintLegal(){
    const el = q(".ob-legal");
    // Rebuilt only when the words change (her language): paintPerms runs on every
    // permission poll, and a rebuild would take the focus off a link she tabbed to.
    const words = [t("obTerms"), t("obTermsLink"), t("obPrivacyLink")].join("|");
    if(el.dataset.words !== words){
      el.dataset.words = words;
      const parts = String(t("obTerms")).split(/(\{terms\}|\{privacy\})/);
      el.replaceChildren(...parts.filter(Boolean).map(part => {
        const doc = part === "{terms}" ? "terms" : part === "{privacy}" ? "privacy" : null;
        if(!doc) return document.createTextNode(part);
        const b = document.createElement("button");
        b.type = "button"; b.className = "ob-legal-link"; b.dataset.doc = doc;
        b.textContent = t(doc === "terms" ? "obTermsLink" : "obPrivacyLink");
        return b;
      }));
    }
    const on = step === 1 && pi === 0;
    el.classList.toggle("on", on);
    el.setAttribute("aria-hidden", on ? "false" : "true");
    el.inert = !on;
  }

  function openLegal(doc){ post("/api/onboarding/legal", {open: doc}).catch(()=>{}); }

  // She went on from the screen that carries the line: that is her agreement. Sent
  // once per run; the server keeps the first timestamp for a version anyway.
  let termsSent = false;
  function acceptTerms(){
    if(termsSent) return;
    termsSent = true;
    post("/api/onboarding/legal", {action: "accept"}).catch(()=>{ termsSent = false; });
  }

  // ------------------------------------------------------------------ 1. language
  // The first of step 1's four questions. She picks it once, here, and MicMic listens
  // and answers only in it from then on (product decision: no more silently defaulting
  // to English). The choices are the page's own LANGS (index.html): a language is
  // offered here the day it is added there.
  // The owner's decision (2026-09-28): the local model for the chosen language downloads
  // automatically from here; Apple hears every turn until it is ready, and Settings >
  // Better recognition has Cancel and Try again. A language Apple covers asks for nothing.
  function paintLangPicks(){
    for(const b of qa(".ob-lopt")) b.setAttribute("aria-checked", b.dataset.code === LANG ? "true" : "false");
  }

  function pickLanguage(code){
    if(code === LANG){ paintLangPicks(); return; }
    const speech = SPEECH[code];
    applyLanguage(code);
    paintLangPicks();
    asrInstalled = true;
    // The download is for the language the server has saved, so it waits for the save.
    (speech ? post("/api/settings", {language_hint: speech}) : Promise.resolve())
      .catch(()=>{})
      .then(() => post("/api/asr", {action: "install"}))
      .catch(()=>{})
      .then(pollAsr);
  }

  // The first-run download: started the moment a language is settled on. A change
  // already triggers it above; this covers moving on from the language with the
  // preselected one untouched, so the download still starts, exactly once.
  function ensureAsrInstall(){
    if(asrInstalled) return;
    asrInstalled = true;
    // Polled once the server has the job: a poll sent alongside the POST saw "none" and
    // never looked again, so the line stayed empty until Try it.
    post("/api/asr", {action: "install"}).catch(()=>{}).then(pollAsr);
  }

  // ------------------------------------------------------------------ 1b. its status
  // Fed by /api/asr, shown under the language choice and again on Try it while it is
  // still running. Never blocks moving on: it is just a line of text.
  function asrText(){
    if(!asrStatus) return "";
    if(asrStatus.state === "downloading"){
      const pct = asrStatus.total ? Math.floor((asrStatus.done || 0) / asrStatus.total * 100) : 0;
      return t("obAsrDownloading").replace("{pct}", pct);
    }
    if(asrStatus.state === "ready") return t("obAsrReady");
    if(asrStatus.state === "failed") return t("obAsrFailed");
    return "";                                    // apple or none: nothing to say
  }

  function paintAsrLine(){
    const text = asrText();
    // Step 1's line belongs to the language: the permission questions stay one line each.
    for(const el of qa(".ob-asr")){
      el.textContent = text;
      el.hidden = !text || (!!el.closest('.ob-step[data-n="1"]') && pi !== 0);
    }
    const note = q(".ob-movednote");
    note.textContent = MOVED_ITSELF[LANG] || MOVED_ITSELF.en;
    note.hidden = !movedItself || step !== 1 || pi !== 0;
  }

  // The owner's fresh install (2026-09-30): "downloading 93%" stayed on Try it while
  // /api/asr already said "ready". The line was a chain of polls, each scheduling the
  // next, and one request that never came back (the window was behind System Settings
  // for the Accessibility pane) ended the chain for good. Now no request may take more
  // than a few seconds, and the step's own timers and coming back to the window start
  // a new poll whenever the last one is stale.
  const ASR_TIMEOUT = 4000, ASR_STALE = 3000;
  async function pollAsr(){
    if(asrBusy && Date.now() - asrAt < ASR_TIMEOUT + 500) return;   // one at a time
    asrBusy = true; asrAt = Date.now();
    const ctl = window.AbortController ? new AbortController() : null;
    const kill = ctl ? setTimeout(() => ctl.abort(), ASR_TIMEOUT) : 0;
    try{
      const r = await fetch("/api/asr", ctl ? {cache:"no-store", signal:ctl.signal} : {cache:"no-store"});
      if(r.ok) asrStatus = await r.json();
    }catch(_){}
    clearTimeout(kill);
    asrBusy = false; asrAt = Date.now();
    if(!root) return;
    paintAsrLine();
    clearTimeout(asrTimer); asrTimer = null;
    if(asrStatus && asrStatus.state === "downloading") asrTimer = setTimeout(pollAsr, 1000);
  }
  // Called by the step timers: a download still showing, and nothing heard for a while.
  function asrWatch(){
    if(asrStatus && asrStatus.state === "downloading" && Date.now() - asrAt > ASR_STALE) pollAsr();
  }

  // ------------------------------------------------------------------ 1c. permissions
  // One at a time, in order, after the language. Nothing is asked until she clicks: the
  // button posts /api/permissions/request, the app asks macOS for exactly that one, and
  // its answer comes back through /api/permissions, polled while this step is up.
  //   ask      never asked (or Accessibility off): the why line, the Allow button
  //   asking   she clicked: macOS's dialog (or the Accessibility pane) is up
  //   granted  a tick, "On", and on to the next by itself
  //   denied   she said no: "You can turn it on later in Settings", Open Settings
  //   unknown  this Mac cannot be asked from here: say so, and let her go on
  function pstate(k){
    if(!perms) return "checking";
    const s = perms[k] || "unknown";
    if(s === "granted") return "granted";
    // Accessibility has no "refused": off is all macOS reports until it is on.
    if(k !== "accessibility" && s === "denied") return "denied";
    if(s === "unknown") return asked[k] ? "unknown" : "ask";
    return asked[k] ? "asking" : "ask";
  }

  function canHear(){
    return !perms || (["granted", "unknown"].includes(perms.microphone)
                      && ["granted", "unknown"].includes(perms.speech));
  }

  function paintPerms(){
    if(!root) return;
    const p = SETUP[pi], lang = p.k === "language";
    // The language is always answered (one is preselected): its picture is the plain one.
    const st = lang ? "ask" : pstate(p.k);
    const card = q(".ob-pcard");
    card.dataset.k = p.k; card.dataset.s = st;
    const s1 = q('.ob-step[data-n="1"]');
    const title = s1.querySelector(".ob-title"), sub = s1.querySelector(".ob-sub");
    const btn = q(".ob-allow");
    title.dataset.t = p.label;
    sub.dataset.t = lang ? p.why : st === "granted" ? "obPermOn" : st === "denied" ? "obPermDenied"
      : st === "asking" ? p.asking : st === "unknown" ? "obUnknown" : p.why;
    if(!lang) btn.dataset.t = st === "denied" || (st === "asking" && p.k === "accessibility") ? "obOpenSettings" : p.allow;
    for(const el of [title, sub, btn]) el.textContent = say(el.dataset.t);
    q(".ob-lopts").hidden = !lang;
    btn.hidden = lang || st === "granted" || st === "checking" || st === "unknown";
    btn.classList.toggle("ob-soft", st !== "ask");
    btn.disabled = st === "asking" && p.k !== "accessibility";
    q(".ob-pofn").textContent = t("obPermOf").replace("{n}", pi + 1);
    qa(".ob-pdots i").forEach((d, i) => {
      const k = SETUP[i].k, ds = k === "language" ? "granted" : pstate(k);
      d.className = i === pi ? "on" : i < pi && ds === "granted" ? "done" : i < pi ? "skip" : "";
    });
    const name = (LANGS.find(l => l.code === LANG) || {}).name || LANG;
    card.setAttribute("aria-label", lang ? `${t(p.label)} ${name}`
                                         : `${t(p.label)}: ${t(st === "granted" ? "obOn" : "obOff")}`);
    paintAsrLine();
    paintLegal();
    // Try it's footer too: Done instead of waiting depends on the answer just polled.
    if(step >= 1) footer();
    paintTry();
  }

  // Try it: how to talk, and what to say. Without the microphone or speech, MicMic
  // cannot hear her, so it says what still works (typing in the window) instead of
  // waiting for a turn that cannot come. Without Accessibility the key cannot reach
  // MicMic (the owner's fresh install said "Hold ⌥ and say" with it off), so the
  // circle leads and no key is named.
  function paintTry(){
    const s2 = q('.ob-step[data-n="2"]'), line = q(".ob-nohear");
    const off = !canHear() && !succeeded;
    line.hidden = !off;
    if(off) line.textContent = t(perms.microphone === "granted" || perms.microphone === "unknown" ? "obNoSpeech" : "obNoMic");
    const title = s2.querySelector(".ob-title"), sub = s2.querySelector(".ob-sub");
    if(!succeeded){
      title.dataset.t = off ? "obTypeTitle" : "ob3Title";
      title.textContent = say(title.dataset.t);
      sub.dataset.t = keyWorks() ? "ob3Sub" : "ob3SubClick";
      keyed(sub, say(sub.dataset.t));
    }
    sub.hidden = off; q(".ob-quote").hidden = off;
    q(".ob-nokey").hidden = !perms || keyWorks() || succeeded || !canHear();
    paintBackupKey();
    paintClash();
    // Hold it, or tap it: said once the key can work, and not while another app's use
    // of that key is the thing to settle first.
    q(".ob-howto").hidden = !keyWorks() || succeeded || off || clashOn();
  }

  function keyWorks(){ return !!perms && perms.accessibility === "granted"; }

  // Accessibility off, and the backup key (⌃⌥ M, no permission needed) works: the
  // hint teaches that key, then says Accessibility would make her own key work alone.
  // Otherwise the old line. index.html's backupKeyLines() decides, for Settings too.
  function paintBackupKey(){
    const bl = window.backupKeyLines ? window.backupKeyLines(perms) : null;
    const talk = q(".ob-nokey .ob-bkey"), line = q('.ob-nokey [data-t="obKeyNeedsAx"]');
    talk.style.display = bl ? "block" : "none";
    // Two lines where there was one: a little wider, so step 3 still fits the window.
    talk.parentNode.style.maxWidth = bl ? "40ch" : "";
    talk.textContent = bl ? bl.talk : "";
    line.textContent = bl ? bl.ax : say("obKeyNeedsAx");
  }

  async function pollPerms(){
    try{
      const r = await fetch("/api/permissions", {cache:"no-store"});
      if(r.ok) perms = await r.json();
    }catch(_){}
    if(!root) return;
    if(step === 1) settlePerm(); else paintPerms();
  }

  // Where step 1 stands after each answer: a permission already on when its screen
  // comes up is passed over (never the language: she always sees that one); one that
  // just turned on shows its tick, then moves on.
  function settlePerm(){
    if(!perms){ paintPerms(); return; }
    let first = false;
    if(!pentered){
      pentered = first = true;
      while(pi > 0 && pi < SETUP.length && perms[SETUP[pi].k] === "granted") pi++;
      if(pi >= SETUP.length){ pi = SETUP.length - 1; savePi(); go(2); return; }
      savePi();
    }
    const was = q(".ob-pcard").dataset.s;
    paintPerms();
    // The screen's action was not known until its state was: by keyboard, focus moves
    // from wherever go() left it to the Allow button, so Enter asks, not "Not now".
    if(first && byKey){ const a = primary(); if(a) a.focus({preventScroll:true}); }
    if(pi > 0 && pstate(SETUP[pi].k) === "granted" && !advanceTimer){
      if(was !== "granted") announce(`${t(SETUP[pi].label)}: ${t("obOn")}`);
      advanceTimer = setTimeout(() => { advanceTimer = null; if(step === 1) nextPerm(); },
                                REDUCE.matches ? 500 : 950);
    }
  }

  function savePi(){ try{ sessionStorage.setItem(PERM_KEY, String(pi)); }catch(_){} }

  function nextPerm(){
    clearTimeout(advanceTimer); advanceTimer = null;
    if(pi === 0){ ensureAsrInstall(); acceptTerms(); }   // moving on from the language settles it
    let n = pi + 1;
    while(n < SETUP.length && perms && perms[SETUP[n].k] === "granted") n++;
    if(n >= SETUP.length){ go(2); return; }
    pi = n; savePi();
    const s1 = q('.ob-step[data-n="1"]');
    for(const el of s1.querySelectorAll(".ob-title,.ob-sub,.ob-pcard")){
      el.classList.remove("ob-swap"); void el.offsetWidth; el.classList.add("ob-swap");
    }
    paintPerms();
    footer();
    requestAnimationFrame(() => {
      if(!root) return;
      const act = primary();
      (byKey && act ? act : s1.querySelector(".ob-title")).focus({preventScroll:true});
    });
  }

  async function allow(){
    const p = SETUP[pi];
    asked[p.k] = true;
    paintPerms();
    announce(say(p.asking));
    try{ await post("/api/permissions/request", {k:p.k}); }catch(_){}
    pollPerms();
  }

  // ------------------------------------------------------------------ 2. try it
  async function arm(){
    try{ await post("/api/onboarding", {action:"try"}); }catch(_){}
    // The words she is about to say have to be heard in the language she is reading.
    // Changed only when it differs from what the server is using, which on a new Mac
    // is the shipped default.
    try{
      const want = SPEECH[LANG];
      const c = await (await fetch("/api/config", {cache:"no-store"})).json();
      if(want && c.language_hint !== want) await post("/api/settings", {language_hint:want});
    }catch(_){}
  }

  async function pollTurn(){
    if(succeeded || step !== 2) return;
    try{
      const d = await (await fetch("/api/onboarding", {cache:"no-store"})).json();
      if(d && d.turn && d.turn.heard && step === 2 && !succeeded) success(d.turn.heard);
      else if(d && "clash" in d) setClash(d.clash);
    }catch(_){}
  }

  function live(state){
    if(!root || step !== 2 || succeeded) return;
    const s = ["thinking", "listening"].includes(state) ? state : "idle";
    q(".ob-orb").dataset.live = s;
    const pill = q(".ob-pill");
    pill.dataset.live = s;
    const label = pill.querySelector("span");
    label.dataset.t = {thinking:"think", listening:"ready"}[s] || "obWaiting";
    label.textContent = say(label.dataset.t);
  }

  function success(heard){
    succeeded = true;
    clearInterval(turnTimer); turnTimer = null;
    const s2 = q('.ob-step[data-n="2"]');
    delete q(".ob-orb").dataset.live;
    s2.classList.add("ok");
    const title = s2.querySelector(".ob-title"), sub = s2.querySelector(".ob-sub");
    title.dataset.t = "obSetTitle"; sub.dataset.t = "obSetSub";
    const qt = s2.querySelector(".ob-qtext");
    delete qt.dataset.t; qt.textContent = heard;
    for(const el of [title, sub]){
      el.textContent = say(el.dataset.t);
      el.classList.remove("ob-swap"); void el.offsetWidth; el.classList.add("ob-swap");
    }
    q(".ob-nokey").hidden = q(".ob-howto").hidden = true;
    paintClash();
    announce(t("obSetTitle"));
    // Saved now, not on close: the window may be shut from under the celebration.
    post("/api/onboarding", {action:"done"}).catch(()=>{});
    footer();
    if(byKey) q('[data-go="done"]').focus({preventScroll:true});
    closeTimer = setTimeout(() => close(), REDUCE.matches ? 2600 : 3400);
  }

  // ------------------------------------------------------------------ 2b. the key clash
  // Another dictation app she uses already answers MicMic's key (Handy on right Option:
  // pressing it ran Handy, and this step waited for ever). One line says so, with the
  // first free key as a button, and the other free ones one click away. Choosing saves
  // the same "hotkey" setting as Settings' own recorder.
  const keyName = spec => window.hotkeyName ? hotkeyName(spec) : spec;
  const same = (a, b) => String(a || "") === String(b || "");

  function setClash(c){
    const cur = window.hotkeySpec ? window.hotkeySpec() : "right-option";
    // A clash about a key she has just moved away from is already answered.
    const next = c && same(c.key, cur) ? c : null;
    if(JSON.stringify(next) === JSON.stringify(clash)) return;
    clash = next;
    paintTry();
  }

  // Only once the key can work at all: without Accessibility the circle leads and no
  // key is named (the same rule as the key hint), so one thing is said at a time.
  function clashOn(){ return !!clash && !succeeded && canHear() && keyWorks(); }

  function paintClash(){
    if(!root) return;
    const box = q(".ob-clash"), on = clashOn();
    box.hidden = !on;
    q('.ob-step[data-n="2"]').classList.toggle("clash", on);
    if(!on) return;
    const nw = clash.suggest ? keyName(clash.suggest) : "";
    q(".ob-clash-t").textContent = t(nw ? "obClash" : "obClashNone")
      .replace("{key}", keyName(clash.key)).replace("{app}", clash.app).replace("{new}", nw);
    const use = q(".ob-clash-use");
    use.hidden = !nw;
    use.dataset.v = clash.suggest || "";
    use.textContent = t("obClashUse").replace("{new}", nw);
    const others = (clash.free || []).filter(k => k !== clash.suggest);
    const other = q(".ob-clash-other"), keys = q(".ob-clash-keys");
    other.textContent = t("obClashOther");
    other.hidden = clashOpen || !others.length;
    keys.hidden = !clashOpen || !others.length;
    const want = others.join(",");
    if(keys.dataset.keys !== want){
      keys.innerHTML = others.map(k =>
        `<button type="button" class="ob-on ob-clash-key" data-v="${k}"></button>`).join("");
      keys.dataset.keys = want;
    }
    for(const b of keys.querySelectorAll("button")) b.textContent = keyName(b.dataset.v);
  }

  function chooseKey(spec){
    if(!spec) return;
    clash = null; clashOpen = false;
    // The page's own key first, so every line naming it repaints now, then the save
    // (index.html's saveSettings, the same one Settings uses; it trusts the echo).
    if(window.setHotkeySpec) window.setHotkeySpec(spec);
    if(window.saveSettings) window.saveSettings({hotkey: spec});
    else post("/api/settings", {hotkey: spec}).catch(()=>{});
    paint();
    announce(q('.ob-step[data-n="2"] .ob-sub').textContent);
  }

  // ------------------------------------------------------------------ 0. the move
  // Running from the disk image (or a translocated copy): before anything else, MicMic
  // offers to put itself in Applications and reopen from there (savta/install_place.py).
  function paintMove(){
    if(!root) return;
    const sub = q(".ob-movesub"), go_ = q(".ob-movego"), fail = q(".ob-movefail");
    sub.dataset.t = move.kind === "translocated" ? "obMoveSubT" : "obMoveSub";
    sub.textContent = t(sub.dataset.t);
    go_.dataset.t = moveState === "moving" ? "obMoving" : moveState === "moved" ? "obMoved" : "obMoveGo";
    go_.textContent = t(go_.dataset.t);
    go_.disabled = moveState === "moving" || moveState === "moved";
    go_.classList.toggle("ob-soft", go_.disabled);
    fail.hidden = moveState !== "failed";
  }

  async function doMove(){
    if(moveState === "moving" || moveState === "moved") return;
    moveState = "moving"; paintMove(); footer();
    announce(t("obMoving"));
    let d = null;
    try{ d = await (await post("/api/install/move", {})).json(); }catch(_){}
    if(!root) return;
    moveState = d && d.ok ? "moved" : "failed";
    paintMove(); footer();
    announce(t(moveState === "moved" ? "obMoved" : "obMoveFail"));
  }

  function notNowMove(){
    try{ sessionStorage.setItem(MOVE_KEY, "later"); }catch(_){}
    move = {needed:false};
    go(1);
  }

  // ------------------------------------------------------------------ flow
  function footer(){
    const show = (sel, on) => { const el = q(sel); if(el.hidden === on) el.hidden = !on; };
    // Step 1: Continue from the language. On a permission, "Not now" while it can still
    // be asked; once it is answered (or cannot be checked), a plain Continue.
    // Try it without a way to hear her: Done, not a wait.
    const perm = step === 1 && pi > 0;
    const decided = perm && ["granted", "denied", "unknown"].includes(pstate(SETUP[pi].k));
    show('[data-go="next"].ob-next', step === 1 && (!perm || decided));
    // Step 0: "Not now" is the only other way on; Skip would end the whole setup.
    show(".ob-ghost", (perm && !decided) || (step === 0 && moveState !== "moved"));
    show(".ob-pill", step === 2 && !succeeded && canHear());
    show('[data-go="done"]', step === 2 && (succeeded || !canHear()));
    q(".ob-skip").style.visibility = succeeded || step === 0 ? "hidden" : "";
  }

  // The screen's one action, for Enter and for focus: on a permission still to be
  // asked it is the Allow button, not "Not now".
  function primary(){
    if(!root) return null;
    if(step === 0){
      const m = q(".ob-movego");
      if(!m.disabled && !m.hidden) return m;
    }
    if(step === 1 && pi > 0){
      const a = q(".ob-allow");
      if(!a.hidden && !a.disabled && pstate(SETUP[pi].k) === "ask") return a;
    }
    return qa(".ob-act button").find(b => !b.hidden);
  }

  function announce(text){
    const live = root && root.querySelector(":scope > .ob-sr");
    if(live){ live.textContent = ""; setTimeout(() => { live.textContent = text; }, 60); }
  }

  function go(n){
    if(!root || closing) return;
    const leavingLang = step === 1 && pi === 0;
    step = Math.max(move.needed ? 0 : 1, Math.min(2, n));
    if(leavingLang && step !== 1){ ensureAsrInstall(); acceptTerms(); }
    try{ sessionStorage.setItem(STEP_KEY, String(step)); }catch(_){}
    for(const s of qa(".ob-step")){
      const k = Number(s.dataset.n);
      s.classList.toggle("on", k === step);
      s.classList.toggle("past", k < step);
      s.setAttribute("aria-hidden", k === step ? "false" : "true");
      s.inert = k !== step;
    }
    if(step === 1) pentered = false;
    paint();
    clearInterval(permTimer); permTimer = null;
    clearInterval(turnTimer); turnTimer = null;
    clearTimeout(advanceTimer); advanceTimer = null;
    if(step >= 1){
      pollPerms();
      permTimer = setInterval(pollPerms, step === 1 ? 700 : 2500);
    }
    if(step === 2){
      arm();
      turnTimer = setInterval(() => { pollTurn(); asrWatch(); }, 700);
      live("idle");
    }
    if(step >= 1) pollAsr();
    footer();
    // Focus where the next press should land: the one action, else the headline so a
    // screen reader starts at the top of the new screen.
    requestAnimationFrame(() => {
      if(!root) return;
      const act = primary();
      (byKey && act ? act : q(".ob-step.on .ob-title")).focus({preventScroll:true});
    });
  }

  async function finish(kind){
    if(closing) return;
    try{ await post("/api/onboarding", {action:kind}); }catch(_){}
    close();
  }

  function close(){
    if(!root || closing) return;
    closing = true;
    [permTimer, turnTimer].forEach(clearInterval);
    [advanceTimer, closeTimer, asrTimer].forEach(clearTimeout);
    try{ sessionStorage.removeItem(STEP_KEY); sessionStorage.removeItem(PERM_KEY); }catch(_){}
    if(orig.state !== undefined) window.panelState = orig.state;
    if(orig.heard !== undefined) window.panelHeard = orig.heard;
    removeEventListener("keydown", onKeyCapture, true);
    document.removeEventListener("keydown", swallow);
    removeEventListener("pointerdown", onPointer, true);
    // The language pill slides home rather than jumping when the gear comes back.
    // "+" and the gear wait until it has passed: shown at once, the pill slid across
    // the gear on its way.
    const lang = $("lang"), after = [$("gear"), $("newchat")];
    lang.style.transition = "right .45s cubic-bezier(.22,1,.36,1)";
    after.forEach(el => { el.style.transition = "opacity .3s ease"; el.style.opacity = "0"; });
    root.classList.add("out");
    document.body.classList.remove("onboarding");
    const r = root;
    setTimeout(() => {
      r.remove();
      lang.style.transition = "";
      after.forEach(el => { el.style.opacity = ""; });
      setTimeout(() => after.forEach(el => { el.style.transition = ""; }), 320);
      root = null;
      window.onboardingLanguage = null;
      try{ $("typebox").focus(); }catch(_){}
    }, 480);
  }

  // ------------------------------------------------------------------ keys
  function focusables(){
    const list = [$("langbtn")];
    if($("lang").dataset.open === "1") list.push(...$("langmenu").querySelectorAll("button"));
    list.push(...qa(".ob-step.on button, .ob-foot button, .ob-legal.on button").filter(b =>
      !b.hidden && b.offsetParent !== null && getComputedStyle(b).visibility !== "hidden"));
    return list;
  }

  function onKeyCapture(e){
    if(!root || closing) return;
    byKey = true;
    // The language radiogroup: arrow keys move the selection, wrapping, and each move
    // picks the language it lands on, the way any native radiogroup behaves. Left/Right
    // follow the reading direction, not the physical key, so RTL is not reversed.
    if(document.activeElement.classList.contains("ob-lopt")
       && ["ArrowLeft","ArrowRight","ArrowUp","ArrowDown"].includes(e.key)){
      e.preventDefault(); e.stopPropagation();
      const opts = qa(".ob-lopt");
      const i = opts.indexOf(document.activeElement);
      const rtl = document.documentElement.dir === "rtl";
      const fwd = e.key === "ArrowDown" ? true : e.key === "ArrowUp" ? false
        : e.key === "ArrowRight" ? !rtl : rtl;
      const n = (i + (fwd ? 1 : -1) + opts.length) % opts.length;
      opts[n].focus();
      pickLanguage(opts[n].dataset.code);
      return;
    }
    // Enter anywhere that is not a control is the screen's one action.
    if(e.key === "Enter" && !(e.target instanceof HTMLButtonElement)){
      const act = primary();
      if(act){ e.preventDefault(); e.stopPropagation(); act.click(); }
      return;
    }
    if(e.key === "Escape"){
      if($("lang").dataset.open === "1") return;     // the menu closes first
      e.preventDefault(); e.stopPropagation();
      if(step === 0){ if(moveState !== "moved") notNowMove(); return; }
      finish(succeeded ? "done" : "skip");
      return;
    }
    if(e.key === "Tab"){
      const list = focusables();
      if(!list.length) return;
      const i = list.indexOf(document.activeElement);
      const n = e.shiftKey ? (i <= 0 ? list.length - 1 : i - 1) : (i + 1) % list.length;
      e.preventDefault();
      list[n].focus();
    }
  }
  // Space and Enter on the page open the microphone and "d" opens the engineer's panel.
  // While this is up, keys belong to it: they still reach the focused button, then stop.
  function swallow(e){ if(root && !closing) e.stopPropagation(); }
  function onPointer(){ byKey = false; }

  // ------------------------------------------------------------------ start
  async function start(){
    let d = null;
    try{ d = await (await fetch("/api/onboarding", {cache:"no-store"})).json(); }catch(_){}
    if(!d || !d.show) return;
    // The server may have just moved a first run's key off one another app uses
    // (onboarding_api.ensure_free_default): every line naming the key names that one.
    try{
      const c = await (await fetch("/api/config", {cache:"no-store"})).json();
      if(c && c.hotkey && window.setHotkeySpec) window.setHotkeySpec(c.hotkey);
    }catch(_){}
    let later = false;
    try{ later = sessionStorage.getItem(MOVE_KEY) === "later"; }catch(_){}
    move = d.move && d.move.needed && !later ? d.move : {needed:false};
    movedItself = !!d.moved_itself;

    const css = document.createElement("link");
    css.rel = "stylesheet"; css.href = "/onboarding/onboarding.css";
    await new Promise(res => { css.onload = css.onerror = res; document.head.appendChild(css); });

    root = document.createElement("div");
    root.className = "ob";
    root.setAttribute("role", "dialog");
    root.setAttribute("aria-modal", "true");
    root.innerHTML = markup();
    document.body.appendChild(root);
    document.body.classList.add("onboarding");

    root.addEventListener("click", e => {
      // A click anywhere on the page arms the microphone; not on these screens.
      e.stopPropagation();
      closeLang();
      const b = e.target.closest("button");
      if(!b) return;
      if(b.dataset.doc){ openLegal(b.dataset.doc); return; }
      if(b.dataset.go === "next") step === 1 ? nextPerm() : go(step + 1);
      else if(b.dataset.go === "later") step === 0 ? notNowMove() : nextPerm();
      else if(b.classList.contains("ob-movego")) doMove();
      else if(b.classList.contains("ob-clash-use") || b.classList.contains("ob-clash-key")) chooseKey(b.dataset.v);
      else if(b.classList.contains("ob-clash-other")){
        clashOpen = true; paintClash();
        const k = q(".ob-clash-key"); if(k && byKey) k.focus({preventScroll:true});
      }
      else if(b.dataset.go === "done") finish("done");
      else if(b.classList.contains("ob-skip")) finish("skip");
      else if(b.classList.contains("ob-lopt")) pickLanguage(b.dataset.code);
      else if(b.classList.contains("ob-allow")) allow();
      // Try it's "Turn on" for Accessibility: the same one request as step 1's
      else if(b.dataset.pane){
        asked[b.dataset.pane] = true;
        post("/api/permissions/request", {k:b.dataset.pane}).catch(()=>{});
      }
      else if(b.classList.contains("ob-orb") && !succeeded){
        // Without Accessibility the key cannot reach MicMic, so the circle is the
        // way in: the same "listen" the page's own orb sends to the listener, which
        // opens a real turn (a tap turn: it sends after a moment of silence).
        try{ window.webkit.messageHandlers.micmic.postMessage("listen"); }catch(_){}
        if(canHear()) live("listening");
      }
    });
    addEventListener("keydown", onKeyCapture, true);
    document.addEventListener("keydown", swallow);
    addEventListener("pointerdown", onPointer, true);

    // The listener drives the panel through these. Watched, never replaced: the page
    // underneath keeps its state for when this closes.
    orig.state = window.panelState;
    orig.heard = window.panelHeard;
    window.panelState = (s, text) => { if(orig.state) orig.state(s, text); live(s); };
    window.panelHeard = text => {
      if(orig.heard) orig.heard(text);
      if(root && step === 2 && !succeeded && text){
        const qt = q(".ob-qtext"); delete qt.dataset.t; qt.textContent = text;
      }
    };

    let resume = 1;
    try{ resume = Number(sessionStorage.getItem(STEP_KEY)) || 1; }catch(_){}
    if(move.needed) resume = 0;              // before any other step, every time
    setClash(d.clash);
    try{ pi = Math.max(0, Math.min(SETUP.length - 1, Number(sessionStorage.getItem(PERM_KEY)) || 0)); }catch(_){}
    go(resume);
    requestAnimationFrame(() => requestAnimationFrame(() => root && root.classList.add("in")));
  }

  start();
})();
