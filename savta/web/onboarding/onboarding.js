/* MicMic's first-run onboarding: three short screens inside the app's own window.

   index.html loads this only in ?panel=1 and only when /api/config says first_run;
   it then shows only if /api/onboarding agrees (not finished or skipped yet). It uses
   the page's own globals ($, t, LANG, CFG, closeLang) and its COPY table, adds no
   element with an id, and takes everything it added away again when it closes, so the
   window afterwards is exactly the window without it.

     1  Hold the right Option key and talk: an animated bar and keyboard.
     2  Microphone, Speech Recognition, Accessibility, polled live from the server.
     3  Try it for real: done when the server sees the next utterance. */
(() => {
  if(window.__onboarding) return;
  window.__onboarding = true;

  const STEP_KEY = "micmic.onboarding.step";
  const REDUCE = matchMedia("(prefers-reduced-motion: reduce)");
  const SPEECH = {he:"he-IL", en:"en-US", ar:"ar-SA", ru:"ru-RU"};
  const PERMS = [
    {k:"microphone",    label:"obMic",    why:"obMicWhy",    icon:"mic"},
    {k:"speech",        label:"obSpeech", why:"obSpeechWhy", icon:"wave"},
    {k:"accessibility", label:"obAx",     why:"obAxWhy",     icon:"ax"},
  ];
  const SVG = {
    mic:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3z"/><path d="M6 11a1 1 0 0 0-2 0 8 8 0 0 0 7 7.94V21H9a1 1 0 0 0 0 2h6a1 1 0 0 0 0-2h-2v-2.06A8 8 0 0 0 20 11a1 1 0 0 0-2 0 6 6 0 0 1-12 0z"/></svg>',
    wave:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4.5 10v4M8.25 7v10M12 3.5v17M15.75 7v10M19.5 10v4" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round"/></svg>',
    ax:'<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="4.4" r="2.2"/><path d="M4.1 8.4a1.1 1.1 0 0 1 1.3-.9l6.6 1.3 6.6-1.3a1.1 1.1 0 1 1 .4 2.2l-4.9 1v3.2l2 6.4a1.1 1.1 0 0 1-2.1.7L12 15.4l-2 5.6a1.1 1.1 0 0 1-2.1-.7l2-6.4v-3.2l-4.9-1a1.1 1.1 0 0 1-.9-1.3z"/></svg>',
    check:'<svg viewBox="0 0 24 24" aria-hidden="true"><path class="ck" d="M5 12.6l4.3 4.3L19 7.2"/></svg>',
    tri:'<svg viewBox="0 0 10 10" aria-hidden="true"><path d="M2.5 5 7 1.8v6.4z"/></svg>',
  };

  let root = null, step = 0, perms = null, succeeded = false, closing = false;
  let permTimer = null, turnTimer = null, demoTimers = [], advanceTimer = null, closeTimer = null;
  let orig = {};
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
    const perm = p => `
      <div class="ob-perm" data-k="${p.k}" data-s="checking">
        <span class="ob-ico">${SVG[p.icon]}</span>
        <span class="ob-pl"><b data-t="${p.label}"></b><small data-t="${p.why}"></small></span>
        <span class="ob-ps">
          <button type="button" class="ob-on" data-pane="${p.k}" data-t="obTurnOn"></button>
          <span class="ob-spin" aria-hidden="true"></span>
          <span class="ob-tick" aria-hidden="true">${SVG.check}</span>
        </span>
        <span class="ob-sr"></span>
      </div>`;
    return `
    <div class="ob-top">
      <div class="ob-prog" role="progressbar" aria-valuemin="1" aria-valuemax="3"><i></i><i></i><i></i></div>
    </div>
    <div class="ob-steps">
      <section class="ob-step" data-n="1">
        <div class="ob-hero">
          <div class="ob-demo" data-p="idle" aria-hidden="true">
            <div class="ob-bar">
              <span class="ob-morb"><span class="ob-mring"></span><span class="ob-marc"></span>
                <span class="ob-mcore">${SVG.mic}</span></span>
              <span class="ob-btext">
                <span class="ob-l1"><span class="ob-ph" data-t="ready"></span><span class="ob-words"></span></span>
                <span class="ob-l2"><span class="ob-work" data-t="working"></span>
                  <span class="ob-res">${SVG.check}<span data-t="obDemoDone"></span></span></span>
              </span>
            </div>
            <div class="ob-kbd">
              <div class="ob-row">
                <span class="ob-key ob-space"></span>
                <span class="ob-key ob-mod"><em>⌘</em><small>command</small></span>
                <span class="ob-key ob-hot"><em>⌥</em><small>option</small><span class="ob-ripple"></span></span>
                <span class="ob-arrows">
                  <span class="ob-key ob-half">${SVG.tri}</span>
                  <span class="ob-col"><span class="ob-key ob-half ob-up">${SVG.tri}</span>
                    <span class="ob-key ob-half ob-down">${SVG.tri}</span></span>
                  <span class="ob-key ob-half ob-right">${SVG.tri}</span>
                </span>
              </div>
            </div>
          </div>
        </div>
        <h2 class="ob-title" tabindex="-1" data-t="ob1Title"></h2>
        <p class="ob-sub" data-t="ob1Sub"></p>
        <p class="ob-hint" data-t="ob1Tap"></p>
      </section>

      <section class="ob-step" data-n="2">
        <div class="ob-hero">
          <div class="ob-perms" role="list">${PERMS.map(perm).join("")}</div>
        </div>
        <h2 class="ob-title" tabindex="-1" data-t="ob2Title"></h2>
        <p class="ob-sub" data-t="ob2Sub"></p>
      </section>

      <section class="ob-step" data-n="3">
        <div class="ob-hero">
          <button type="button" class="ob-orb" data-aria="obListen">
            <span class="ob-halo"></span><span class="ob-rip"></span><span class="ob-rip"></span>
            <span class="ob-shell"></span><span class="ob-oring"></span>
            <span class="ob-ocore">${SVG.mic}</span>
            <span class="ob-ocheck">${SVG.check}</span>
            <span class="ob-sparks">${sparks()}</span>
          </button>
        </div>
        <h2 class="ob-title" tabindex="-1" data-t="ob3Title"></h2>
        <p class="ob-sub" data-t="ob3Sub"></p>
        <p class="ob-quote"><span class="ob-qcheck">${SVG.check}</span><span class="ob-qtext" data-t="ob3Phrase"></span></p>
        <p class="ob-hint ob-nokey" data-t="obNoKey" hidden></p>
      </section>
    </div>
    <div class="ob-foot">
      <button type="button" class="ob-skip" data-t="obSkip"></button>
      <span class="ob-act">
        <button type="button" class="ob-next" data-go="next" data-t="obNext" hidden></button>
        <button type="button" class="ob-ghost" data-go="next" data-t="obLater" hidden></button>
        <span class="ob-pill" hidden><i></i><span data-t="obWaiting"></span></span>
        <button type="button" class="ob-next" data-go="done" data-t="obDone" hidden></button>
      </span>
    </div>
    <p class="ob-sr" aria-live="polite"></p>`;
  }

  // ------------------------------------------------------------------ copy
  function paint(){
    if(!root) return;
    for(const el of qa("[data-t]")) el.textContent = t(el.dataset.t);
    for(const el of qa("[data-aria]")) el.setAttribute("aria-label", t(el.dataset.aria));
    root.setAttribute("aria-label", t("obDialog"));
    const prog = q(".ob-prog");
    prog.setAttribute("aria-valuenow", String(step));
    prog.setAttribute("aria-valuetext", t("obStep").replace("{n}", step));
    paintPerms();
    // The words in the demo are the language's own sentence, one span a word.
    const words = q(".ob-words");
    words.innerHTML = "";
    t("obDemoSay").split(/\s+/).forEach((w, i) => {
      const b = document.createElement("b");
      b.textContent = w;
      words.append(b, document.createTextNode(" "));
    });
    if(step === 1) runDemo();
  }
  window.onboardingLanguage = paint;

  // ------------------------------------------------------------------ 1. the demo
  function runDemo(){
    demoTimers.forEach(clearTimeout); demoTimers = [];
    const demo = q(".ob-demo"), words = qa(".ob-words b");
    const at = (ms, fn) => demoTimers.push(setTimeout(fn, ms));
    if(REDUCE.matches){
      // One still frame that says it all: the key down, the words heard.
      demo.dataset.p = "press"; demo.dataset.w = "1";
      words.forEach(w => w.classList.add("in"));
      return;
    }
    demo.dataset.p = "idle"; delete demo.dataset.w;
    words.forEach(w => w.classList.remove("in"));
    let ms = 650;
    at(ms, () => { demo.dataset.p = "press"; });
    ms += 420;
    words.forEach((w, i) => at(ms + i * 360, () => { w.classList.add("in"); demo.dataset.w = "1"; }));
    ms += words.length * 360 + 520;
    at(ms, () => { demo.dataset.p = "work"; });
    ms += 1150;
    at(ms, () => { demo.dataset.p = "result"; });
    ms += 2300;
    at(ms, () => { demo.dataset.p = "rest"; });
    ms += 700;
    at(ms, () => { if(step === 1) runDemo(); });
  }

  // ------------------------------------------------------------------ 2. permissions
  function paintPerms(){
    if(!root) return;
    for(const p of PERMS){
      const row = q(`.ob-perm[data-k="${p.k}"]`);
      const s = perms ? (perms[p.k] || "unknown") : "checking";
      row.dataset.s = s;
      const why = row.querySelector("small");
      why.dataset.t = s === "not_asked" ? "obAsked" : s === "unknown" ? "obUnknown" : p.why;
      why.textContent = t(why.dataset.t);
      row.querySelector(".ob-sr").textContent =
        `${t(p.label)}: ${t(s === "granted" ? "obOn" : "obOff")}`;
    }
    const box = q(".ob-perms");
    if(perms && perms.ready) box.dataset.ready = ""; else delete box.dataset.ready;
    // Nothing left that she could fix from here: the way on is a real button, not
    // "Later". Only a full set of ticks moves on by itself.
    if(step === 2) footer();
    const nokey = q(".ob-nokey");
    nokey.hidden = !(perms && perms.accessibility !== "granted") || succeeded;
  }

  async function pollPerms(){
    try{
      const r = await fetch("/api/permissions", {cache:"no-store"});
      if(r.ok) perms = await r.json();
    }catch(_){}
    if(!root) return;
    const was = q(".ob-perms").hasAttribute("data-ready");
    paintPerms();
    if(step === 2 && perms && perms.ready && !advanceTimer){
      if(!was) announce(t("obOn"));
      advanceTimer = setTimeout(() => { advanceTimer = null; if(step === 2) go(3); }, 1150);
    }
  }

  function settled(){
    // Every row is either granted or something the page cannot know.
    return !!perms && PERMS.every(p => ["granted", "unknown"].includes(perms[p.k]));
  }

  // ------------------------------------------------------------------ 3. try it
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
    if(succeeded || step !== 3) return;
    try{
      const d = await (await fetch("/api/onboarding", {cache:"no-store"})).json();
      if(d && d.turn && d.turn.heard && step === 3 && !succeeded) success(d.turn.heard);
    }catch(_){}
  }

  function live(state){
    if(!root || step !== 3 || succeeded) return;
    const s = state === "thinking" ? "thinking" : "idle";
    q(".ob-orb").dataset.live = s;
    const pill = q(".ob-pill");
    pill.dataset.live = s;
    const label = pill.querySelector("span");
    label.dataset.t = s === "thinking" ? "think" : "obWaiting";
    label.textContent = t(label.dataset.t);
  }

  function success(heard){
    succeeded = true;
    clearInterval(turnTimer); turnTimer = null;
    const s3 = q('.ob-step[data-n="3"]');
    delete q(".ob-orb").dataset.live;
    s3.classList.add("ok");
    const title = s3.querySelector(".ob-title"), sub = s3.querySelector(".ob-sub");
    title.dataset.t = "obSetTitle"; sub.dataset.t = "obSetSub";
    const qt = s3.querySelector(".ob-qtext");
    delete qt.dataset.t; qt.textContent = heard;
    for(const el of [title, sub]){
      el.textContent = t(el.dataset.t);
      el.classList.remove("ob-swap"); void el.offsetWidth; el.classList.add("ob-swap");
    }
    q(".ob-nokey").hidden = true;
    announce(t("obSetTitle"));
    // Saved now, not on close: the window may be shut from under the celebration.
    post("/api/onboarding", {action:"done"}).catch(()=>{});
    footer();
    if(byKey) q('[data-go="done"]').focus({preventScroll:true});
    closeTimer = setTimeout(() => close(), REDUCE.matches ? 2600 : 3400);
  }

  // ------------------------------------------------------------------ flow
  function footer(){
    const show = (sel, on) => { const el = q(sel); if(el.hidden === on) el.hidden = !on; };
    show('[data-go="next"].ob-next', step === 1 || (step === 2 && settled()));
    show(".ob-ghost", step === 2 && !settled());
    show(".ob-pill", step === 3 && !succeeded);
    show('[data-go="done"]', step === 3 && succeeded);
    q(".ob-skip").style.visibility = succeeded ? "hidden" : "";
  }

  function primary(){ return root && qa(".ob-act button").find(b => !b.hidden); }

  function announce(text){
    const live = root && root.querySelector(":scope > .ob-sr");
    if(live){ live.textContent = ""; setTimeout(() => { live.textContent = text; }, 60); }
  }

  function go(n){
    if(!root || closing) return;
    step = Math.max(1, Math.min(3, n));
    try{ sessionStorage.setItem(STEP_KEY, String(step)); }catch(_){}
    for(const s of qa(".ob-step")){
      const k = Number(s.dataset.n);
      s.classList.toggle("on", k === step);
      s.classList.toggle("past", k < step);
      s.setAttribute("aria-hidden", k === step ? "false" : "true");
      s.inert = k !== step;
    }
    qa(".ob-prog i").forEach((i, k) => {
      i.classList.toggle("on", k + 1 === step);
      i.classList.toggle("done", k + 1 < step);
    });
    paint();
    clearInterval(permTimer); permTimer = null;
    clearInterval(turnTimer); turnTimer = null;
    if(step !== 1){ demoTimers.forEach(clearTimeout); demoTimers = []; }
    if(step >= 2){
      pollPerms();
      permTimer = setInterval(pollPerms, step === 2 ? 1000 : 2500);
    }
    if(step === 3){
      arm();
      turnTimer = setInterval(pollTurn, 700);
      live("idle");
    }
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
    [advanceTimer, closeTimer, ...demoTimers].forEach(clearTimeout);
    try{ sessionStorage.removeItem(STEP_KEY); }catch(_){}
    if(orig.state !== undefined) window.panelState = orig.state;
    if(orig.heard !== undefined) window.panelHeard = orig.heard;
    removeEventListener("keydown", onKeyCapture, true);
    document.removeEventListener("keydown", swallow);
    removeEventListener("pointerdown", onPointer, true);
    // The language pill slides home rather than jumping when the gear comes back.
    const lang = $("lang");
    lang.style.transition = "right .45s cubic-bezier(.22,1,.36,1)";
    root.classList.add("out");
    document.body.classList.remove("onboarding");
    const r = root;
    setTimeout(() => {
      r.remove();
      lang.style.transition = "";
      root = null;
      window.onboardingLanguage = null;
      try{ $("typebox").focus(); }catch(_){}
    }, 480);
  }

  // ------------------------------------------------------------------ keys
  function focusables(){
    const list = [$("langbtn")];
    if($("lang").dataset.open === "1") list.push(...$("langmenu").querySelectorAll("button"));
    list.push(...qa(".ob-step.on button, .ob-foot button").filter(b =>
      !b.hidden && b.offsetParent !== null && getComputedStyle(b).visibility !== "hidden"));
    return list;
  }

  function onKeyCapture(e){
    if(!root || closing) return;
    byKey = true;
    // Enter anywhere that is not a control is the screen's one action.
    if(e.key === "Enter" && !(e.target instanceof HTMLButtonElement)){
      const act = primary();
      if(act){ e.preventDefault(); e.stopPropagation(); act.click(); }
      return;
    }
    if(e.key === "Escape"){
      if($("lang").dataset.open === "1") return;     // the menu closes first
      e.preventDefault(); e.stopPropagation();
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
      if(b.dataset.go === "next") go(step + 1);
      else if(b.dataset.go === "done") finish("done");
      else if(b.classList.contains("ob-skip")) finish("skip");
      else if(b.dataset.pane) post("/api/permissions/open", {pane:b.dataset.pane}).catch(()=>{});
      else if(b.classList.contains("ob-orb") && !succeeded){
        // Without Accessibility the key cannot reach MicMic, so the circle is the
        // way in: the same "listen" the page's own orb sends to the listener.
        try{ window.webkit.messageHandlers.micmic.postMessage("listen"); }catch(_){}
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
      if(root && step === 3 && !succeeded && text){
        const qt = q(".ob-qtext"); delete qt.dataset.t; qt.textContent = text;
      }
    };

    let resume = 1;
    try{ resume = Number(sessionStorage.getItem(STEP_KEY)) || 1; }catch(_){}
    go(resume);
    requestAnimationFrame(() => requestAnimationFrame(() => root && root.classList.add("in")));
  }

  start();
})();
