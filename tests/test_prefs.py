#!/usr/bin/env python3
"""Standing preferences and "what do you know about me": set, apply, conflict,
recall, forget. English and Hebrew.

    cd <checkout> && uv run python tests/test_prefs.py

Offline and free: it borrows test_micmic.py's walls (every side effect on the Mac is a
recorder, state lives in a scratch copy) and scripts every model answer, the way
test_micmic's follow-up sections do, so it makes no Jev and no Gemini call. The
answers scripted for the preference questions are the ones Jev gave for these
sentences in the live calibration run of 2026-09-27 (n=2 each: pref_kind 1.00 on all
eight, pref_app / pref_confirm as scripted here, the rule span as scripted here).

Names are fictional. Nothing here reads or prints her real profile or habits: the
sections that need a name or habits write fictional ones into the scratch copy.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_micmic as tm              # noqa: E402  (walls go up on import)

router, brain, mac, prof = tm.router, tm.brain, tm.mac, tm.prof
from savta import prefs               # noqa: E402
from savta import memory as longterm  # noqa: E402

check, short = tm.check, tm.short


def ch(x: str, conf: float = 0.95) -> dict:
    return {"choice": x, "confidence": conf, "probabilities": {x: conf}}


def nl(x: float) -> dict:
    return {"noul": x}


class pref_turns(tm.scripted_turns):
    """scripted_turns, with an understand() that takes `standing` as the real one does.

    A spec's "prefs" answers are handed back only for questions that were actually
    folded into the request, so a sentence the pre-check missed gets no preference
    answers at all, exactly as it would live. ("pick", text) answers pref_forget with
    the row whose description contains text."""

    def __enter__(self):
        super().__enter__()
        self.standing: list = []
        self.rules_sent: list = []

        def understand(j, utt, contacts, recent="", playing="", likes=None,
                       spans=None, draft=None, standing=None, **_later):
            self.drafts.append(dict(draft) if draft else None)
            self.spans.append(sorted(spans or {}))
            self.standing.append(sorted(standing["questions"]) if standing else None)
            self.rules_sent.append(list(standing.get("rules") or []) if standing else None)
            spec = dict(self.understood[utt])
            answers = spec.pop("prefs", {})
            asked = (standing or {}).get("questions") or {}
            got = {}
            for k, v in answers.items():
                if k not in asked:
                    continue
                if isinstance(v, tuple) and v[0] == "pick":
                    opts = asked[k]["criteria"]
                    v = ch(next((o for o, t in opts.items() if t and v[1] in t),
                                prefs.FORGET_NONE))
                got[k] = v
            u = tm._u(**spec)
            u["standing"] = got
            return u
        router.understand = understand
        return self


def fresh():
    tm.reset_state()
    prefs.forget_all()
    longterm.forget_all()
    router._drop_undo()


def said(r) -> str:
    return r.get("say") or ""


# ================================================================== pre-check

NORMAL = ["what time is it", "מה השעה", "كم الساعة هلق", "what time is it in New York",
          "what's the weather like today", "מה מזג האוויר בתל אביב", "какая погода в Москве",
          "tell me a joke", "احكيلي نكتة", "расскажи анекдот", "open the calculator",
          "תפתחי את לוח השנה", "play some Umm Kulthum", "תשימי לי שיר של זוהר ארגוב",
          "شغليلي أغنية لفيروز", "send Miriam a message that I will be late",
          "תשלחי הודעה לזוהר שאני בסדר", "напиши Мириам что я приду позже",
          "what's on my screen", "תסכמי את מה שיש על המסך",
          "מי היה ראש הממשלה הראשון של ישראל", "make it louder", "undo",
          "תגבירי את הקול", "בטלי", "what is the capital of France", "and what about Italy",
          "make it quieter", "call Matan", "תתקשרי לדנה", "play some music",
          "send it on WhatsApp", "במקום זה תגידי שאני כבר בדרך", "no, to Dana",
          "another one", "stop"]
RULES = ["from now on use WhatsApp for Mom", "always send to Dana on WhatsApp",
         "don't use YouTube, use Dailymotion", "speak to me in Hebrew",
         "always ask before sending", "keep the volume low",
         "don't call anyone after 10pm", "what do you know about me", "forget that",
         "forget everything", "מעכשיו תשלחי לאמא בוואטסאפ", "תמיד תשאלי אותי לפני שאת שולחת",
         "תדברי איתי בעברית", "תשאירי את הווליום נמוך", "מה את יודעת עליי", "תשכחי את זה",
         "תשכחי הכל", "говори со мной по-русски", "забудь это", "من هلّق ابعتي لماما على واتساب"]


def t_prefs_cost_nothing_on_a_normal_request(_j):
    """The owner's constraint: speed and round trips. A normal sentence carries no
    preference question and no rule; the request is byte for byte what it was."""
    fresh()
    hits = [u for u in NORMAL if prefs.worth_asking(u)]
    check("no normal request (the perf bench's and the suite's) trips the pre-check",
          not hits, str(hits))
    misses = [u for u in RULES if not prefs.worth_asking(u)]
    check("every way of stating, recalling or forgetting one does, in four languages",
          not misses, str(misses))
    check("with nothing saved, a normal request asks nothing extra",
          all(prefs.questions(u) is None for u in NORMAL))

    class Capture:
        calls, cost_usd = 0, 0.0

        def ask(self, state, qs):
            self.seen = (json_dump(state), sorted(qs))
            return {k: ({"choice": next(iter(q["criteria"])), "confidence": 1.0,
                         "probabilities": {}} if q["type"] == "choice"
                        else {"score": 0.0} if q["type"] == "score" else {"noul": 0.0})
                    for k, q in qs.items()}
    a, b = Capture(), Capture()
    brain.understand(a, "play some music", ["Dana"], "nothing yet", likes={})
    brain.understand(b, "play some music", ["Dana"], "nothing yet", likes={}, standing=None)
    check("understand() with no preferences sends the very same request",
          a.seen == b.seen and not any(k.startswith("pref_") for k in a.seen[1])
          and "her_standing_rules" not in a.seen[0], short(a.seen[1]))

    prefs.add_rule("don't call anyone after 10pm")
    prefs.add_rule("אל תתקשרי לאף אחד אחרי עשר בלילה")
    check("a rule reaches a request it touches (EN)",
          "don't call anyone after 10pm" in prefs.relevant("call Dana"),
          str(prefs.relevant("call Dana")))
    check("a rule reaches a request it touches (HE)",
          "אל תתקשרי לאף אחד אחרי עשר בלילה" in prefs.relevant("תתקשרי לדנה"),
          str(prefs.relevant("תתקשרי לדנה")))
    untouched = [u for u in ("play some music", "מה השעה", "make it quieter",
                             "what's the weather like today") if prefs.relevant(u)]
    check("and stays out of every request it does not", not untouched, str(untouched))
    fresh()


def json_dump(x) -> str:
    import json
    return json.dumps(x, ensure_ascii=False, sort_keys=True, default=str)


# ================================================================== set and apply

@tm.with_gates_on
def t_prefs_message_app(_j):
    """"From now on use WhatsApp for Dana": stored typed, applied by code where the
    channel is chosen; her explicit words in a later sentence still win, and she hears
    that once."""
    fresh()
    set_en, set_he = "from now on use WhatsApp for Dana", "מעכשיו תשלחי את כל ההודעות בוואטסאפ"
    late, text_late = "send Dana a message that I will be late", "send Dana a text that I will be late"
    matan, nir_he = "send Matan a message that I am on my way", "תשלחי לניר שאני בדרך הביתה"
    no_one, answer = "send a message that the door is open", "Dana"
    msg = dict(intent="message", contact_named=0.95, has_message_content=0.9)
    understood = {
        set_en: dict(intent="open_app", contact="Dana", contact_named=0.97,
                     prefs={"pref_kind": ch("message_app", 1.0), "pref_app": ch("whatsapp")}),
        set_he: dict(intent="message", language="hebrew",
                     prefs={"pref_kind": ch("message_app", 1.0), "pref_app": ch("whatsapp")}),
        late: dict(msg, contact="Dana"),
        text_late: dict(msg, contact="Dana"),
        matan: dict(msg, contact="Matan"),
        nir_he: dict(msg, contact="Nir Cohen", language="hebrew"),
        no_one: dict(intent="message", has_message_content=0.9),
    }
    with pref_turns(understood) as s:
        s.j.same.update({set_en: 0.9, late: 0.9, text_late: 0.9, matan: 0.9, nir_he: 0.9,
                         answer: 0.9})
        s.j.spans.update({late: "I will be late", text_late: "I will be late",
                          matan: "I am on my way", nir_he: "אני בדרך הביתה"})
        r = router.handle(s.j, set_en, speak=False)
        check("en: set for one person, among her real contacts",
              r["did"] == "preference_set" and prefs.load()["message_app"] == {"Dana": "whatsapp"},
              f"did={r['did']} prefs={prefs.load()['message_app']}")
        check("en: and she hears it back", "Dana" in said(r) and "WhatsApp" in said(r), said(r))
        check("en: the questions rode in the one understanding request",
              s.standing[-1] and "pref_kind" in s.standing[-1] and "pref_rule" in s.spans[-1],
              f"{s.standing[-1]} {s.spans[-1]}")
        check("en: the Undo is offered", bool(r.get("undo")), short(r))

        n = s.j.calls
        r = router.handle(s.j, late, speak=False)
        check("applied: a message to Dana goes on WhatsApp without her saying so",
              r["did"] == "sending" and tm._d(r).get("channel") == "whatsapp",
              f"did={r['did']} detail={short(tm._d(r))}")
        check("applied at no extra cost: the message asked nothing about preferences",
              s.standing[-1] is None and s.j.calls - n == 2,
              f"standing={s.standing[-1]} calls={s.j.calls - n}")
        router._cancel_pending()

        r = router.handle(s.j, text_late, speak=False)
        check("conflict: 'a text' this time is a text, her words win",
              r["did"] == "sending" and tm._d(r).get("channel") == "imessage",
              f"did={r['did']} detail={short(tm._d(r))}")
        check("conflict: and she hears it once, briefly",
              said(r).startswith("This one goes as text messages, as you asked, not on WhatsApp."),
              said(r))
        router._cancel_pending()

        r = router.handle(s.j, matan, speak=False)
        check("a choice for Dana is not a choice for Matan",
              tm._d(r).get("channel") == "imessage", short(tm._d(r)))
        router._cancel_pending()

        # She names nobody and no app, then answers "Dana": Dana's choice applies.
        r = router.handle(s.j, no_one, speak=False)
        check("no person yet: asked who", r["did"] == "need_who", r["did"])
        s.j.answer[answer] = (0.95, 0.05)
        s.j.named[answer] = "Dana"
        r = router.handle(s.j, answer, speak=False)
        check("then asked what it should say", r["did"] == "need_what", r["did"])
        words = "the door is open downstairs"
        s.j.answer[words] = (0.95, 0.05)
        r = router.handle(s.j, words, speak=False)
        check("once she has said who, that person's choice applies",
              r["did"] == "sending" and tm._d(r).get("channel") == "whatsapp",
              f"did={r['did']} awaiting={short(router.AWAITING)} detail={short(tm._d(r))}")
        router._cancel_pending()
        router.AWAITING = None

        r = router.handle(s.j, set_he, speak=False)
        check("he: a choice for everyone",
              r["did"] == "preference_set" and prefs.load()["message_app"].get("*") == "whatsapp"
              and r["lang"] == "hebrew" and "וואטסאפ" in said(r), f"{r['did']} {said(r)}")
        r = router.handle(s.j, nir_he, speak=False)
        check("he: applied to anyone she has not said otherwise for",
              r["did"] == "sending" and tm._d(r).get("channel") == "whatsapp",
              f"did={r['did']} detail={short(tm._d(r))}")
        router._cancel_pending()
    fresh()


@tm.with_gates_on
def t_prefs_confirm_volume_site_language(_j):
    """The other typed knobs: ask before sending, a volume ceiling, the video site
    (stored honestly: only YouTube is searched), the speech language (the existing
    Settings value)."""
    fresh()
    always, relax = "always ask me before sending a message", "you don't need to ask me anymore"
    long_msg = "send Matan a message that I will be there at eight tonight"
    low_he, louder = "תשאירי את הווליום נמוך", "make it louder"
    site, lang_en = "don't use YouTube, use Dailymotion", "speak to me in Hebrew"
    understood = {
        always: dict(intent="message", prefs={"pref_kind": ch("confirm_send", 1.0),
                                              "pref_confirm": ch("always_ask")}),
        relax: dict(intent="chitchat", prefs={"pref_kind": ch("confirm_send", 0.9),
                                              "pref_confirm": ch("no_need")}),
        long_msg: dict(intent="message", contact="Matan", contact_named=0.95,
                       has_message_content=0.9),
        low_he: dict(intent="control", control_action="quieter", language="hebrew",
                     prefs={"pref_kind": ch("volume_limit", 0.97), "pref_volume": ch("low")}),
        louder: dict(intent="control", control_action="louder"),
        site: dict(intent="watch", prefs={"pref_kind": ch("video_site", 0.98),
                                          "pref_site": ch("dailymotion")}),
        lang_en: dict(intent="chitchat", prefs={"pref_kind": ch("speech_language", 1.0),
                                                "pref_language": ch("hebrew")}),
    }
    hint_before = router.load_settings().get("language_hint")
    real_get = mac.get_volume
    try:
        with pref_turns(understood) as s:
            s.j.same[long_msg] = 0.9
            s.j.spans[long_msg] = "I will be there at eight tonight"
            r = router.handle(s.j, always, speak=False)
            check("en: 'always ask before sending' is stored",
                  r["did"] == "preference_set" and prefs.always_confirm(), said(r))
            r = router.handle(s.j, long_msg, speak=False)
            check("applied: a long message said in full now waits for a yes",
                  r["did"] == "confirm_send" and tm._d(r).get("why") == "she_asked_to_be_asked",
                  f"did={r['did']} detail={short(tm._d(r))}")
            router.AWAITING = None
            r = router.handle(s.j, relax, speak=False)
            check("en: and she can take it back", not prefs.always_confirm()
                  and "still check very short" in said(r), said(r))
            r = router.handle(s.j, long_msg, speak=False)
            check("after which the same message counts down as before",
                  r["did"] == "sending", r["did"])
            router._cancel_pending()

            tm.VOLSET.clear(); tm.VOLUME.clear()
            r = router.handle(s.j, low_he, speak=False)
            check("he: 'keep the volume low' sets a ceiling, and turns it down now",
                  r["did"] == "preference_set" and prefs.volume_cap() == 35
                  and tm.VOLSET == [35] and r["lang"] == "hebrew",
                  f"did={r['did']} cap={prefs.volume_cap()} volset={tm.VOLSET} {said(r)}")
            mac.get_volume = lambda: 20
            tm.VOLSET.clear(); tm.VOLUME.clear()
            r = router.handle(s.j, louder, speak=False)
            check("applied: 'louder' stops at her ceiling",
                  r["did"] == "louder" and tm.VOLSET == [35] and not tm.VOLUME,
                  f"did={r['did']} volset={tm.VOLSET} volume={tm.VOLUME}")
            mac.get_volume = lambda: 35
            tm.VOLSET.clear(); tm.VOLUME.clear()
            r = router.handle(s.j, louder, speak=False)
            check("conflict: asked again at the ceiling, her request wins and she hears why",
                  r["did"] == "louder" and tm.VOLUME == [18]
                  and said(r).startswith("You asked me to keep the volume low"),
                  f"did={r['did']} volume={tm.VOLUME} {said(r)}")

            r = router.handle(s.j, site, speak=False)
            check("en: Dailymotion is remembered, and it says honestly it only searches YouTube",
                  r["did"] == "preference_set" and prefs.load()["video_site"] == "dailymotion"
                  and "only search YouTube" in said(r) and not tm.YT_QUERIES,
                  f"did={r['did']} {said(r)} queries={tm.YT_QUERIES}")

            r = router.handle(s.j, lang_en, speak=False)
            check("en: 'speak to me in Hebrew' sets the existing Settings language",
                  router.load_settings().get("language_hint") == "he-IL", str(router.load_settings()))
            check("and says so in Hebrew", r["lang"] == "hebrew" and "בעברית" in said(r), said(r))
            u = router.undo_last()
            check("undo puts the language back",
                  u["undone"] and router.load_settings().get("language_hint")
                  == (hint_before or "en-US"), f"{u} {router.load_settings()}")
    finally:
        mac.get_volume = real_get
        router.save_settings({"language_hint": hint_before or "en-US"})
    fresh()


@tm.with_gates_on
def t_prefs_free_rules(_j):
    """A rule in her own words: Jev picks the words, nothing writes them. Shown to Jev
    only on a request it touches; the request wins, and she hears the rule once."""
    fresh()
    rule_en, rule_he = ("from now on don't call anyone after 10pm",
                        "מעכשיו אל תתקשרי לאף אחד אחרי עשר בלילה")
    call_en, call_he, music = "call Dana", "תתקשרי לדנה", "play some music"
    call = dict(intent="call", contact="Dana", contact_named=0.95)
    understood = {
        rule_en: dict(intent="note", spans={"pref_rule": ("don t call anyone after 10pm", 0.9)},
                      prefs={"pref_kind": ch("other_rule", 1.0)}),
        rule_he: dict(intent="call", language="hebrew",
                      spans={"pref_rule": ("אל תתקשרי לאף אחד אחרי עשר בלילה", 0.9)},
                      prefs={"pref_kind": ch("other_rule", 1.0)}),
        call_en: dict(call, prefs={"pref_breaks_rule": nl(0.92)}),
        "call Dana now": dict(call, prefs={"pref_breaks_rule": nl(0.03)}),
        call_he: dict(call, language="hebrew", prefs={"pref_breaks_rule": nl(0.9)}),
        music: dict(intent="music", media_kind="song_or_music"),
    }
    with pref_turns(understood) as s:
        s.j.same.update({call_en: 0.9, "call Dana now": 0.9, call_he: 0.9})
        r = router.handle(s.j, rule_en, speak=False)
        check("en: stored in her own words, apostrophe and all",
              prefs.rules() == ["don't call anyone after 10pm"]
              and "don't call anyone after 10pm" in said(r), f"{prefs.rules()} {said(r)}")
        tm.CALLED.clear()
        r = router.handle(s.j, call_en, speak=False)
        check("en: the rule rode along only because the request touches it",
              s.rules_sent[-1] == ["don't call anyone after 10pm"]
              and s.standing[-1] == ["pref_breaks_rule"], f"{s.rules_sent[-1]} {s.standing[-1]}")
        # Owner decision 2026-10-02: a call against her rule is asked first, never placed.
        check("conflict: a call against her rule is asked first, not placed",
              r["did"] == "confirm_call" and not tm.CALLED and r.get("asked_back"),
              f"did={r['did']} called={tm.CALLED}")
        check("and she hears the rule read back, then 'anyway?'",
              "don't call anyone after 10pm" in said(r) and "anyway?" in said(r), said(r))
        s.j.answer["yes"] = (0.95, 0.05)
        s.j.agreed["yes"] = 0.95
        r = router.handle(s.j, "yes", speak=False)
        check("on her yes, the call is placed (an ordinary call, not an emergency)",
              r["did"] == "calling" and tm.CALLED and tm.CALLED[0][0] == "Dana",
              f"did={r['did']} called={tm.CALLED}")
        r = router.handle(s.j, "call Dana now", speak=False)
        check("no conflict, no remark", "You asked me" not in said(r), said(r))
        r = router.handle(s.j, music, speak=False)
        check("a request the rule does not touch carries nothing",
              s.standing[-1] is None and s.rules_sent[-1] is None, str(s.standing[-1]))

        r = router.handle(s.j, rule_he, speak=False)
        check("he: stored in her words, answered in Hebrew",
              "אל תתקשרי לאף אחד אחרי עשר בלילה" in prefs.rules()
              and r["lang"] == "hebrew" and said(r).startswith("רשמתי"), said(r))
        tm.CALLED.clear()
        r = router.handle(s.j, call_he, speak=False)
        check("he: asked first in Hebrew, the rule read back, nothing dialled",
              r["did"] == "confirm_call" and not tm.CALLED
              and "אל תתקשרי לאף אחד אחרי עשר בלילה" in said(r) and "בכל זאת" in said(r),
              f"{r['did']} {said(r)} {tm.CALLED}")
        s.j.answer["לא"] = (0.95, 0.05)
        s.j.agreed["לא"] = 0.05
        r = router.handle(s.j, "לא", speak=False)
        check("he: her no places no call", r["did"] == "declined_call" and not tm.CALLED,
              f"{r['did']} {tm.CALLED}")
    fresh()


@tm.with_gates_on
def t_prefs_rule_asks_before_send(_j):
    """Owner decision 2026-10-02 (bench hard-pref-003): a message against a saved rule
    ("never message Dana after 10pm") is read back with the rule and asked "send
    anyway?". It goes only on her yes, never on a countdown; a no sends nothing.
    Anything else against a rule (music, volume) still just says the rule once."""
    fresh()
    prefs.add_rule("never message Dana after 10pm")
    prefs.add_rule("אל תשלחי הודעות לדנה אחרי עשר בלילה")
    prefs.add_rule("never play music after 10pm")
    text_en, text_he = "text Dana are you up", "תשלחי לדנה את ערה"
    ok_en, music = "text Dana see you tomorrow morning", "play some music"
    bare_en = "send Dana a message"
    msg = dict(intent="message", contact="Dana", contact_named=0.95, has_message_content=0.9)
    understood = {
        text_en: dict(msg, prefs={"pref_breaks_rule": nl(0.93)}),
        text_he: dict(msg, language="hebrew", prefs={"pref_breaks_rule": nl(0.9)}),
        ok_en: dict(msg, prefs={"pref_breaks_rule": nl(0.03)}),
        bare_en: dict(msg, has_message_content=0.1, prefs={"pref_breaks_rule": nl(0.9)}),
        music: dict(intent="music", media_kind="song_or_music",
                    prefs={"pref_breaks_rule": nl(0.9)}),
    }
    with pref_turns(understood) as s:
        s.j.same.update({text_en: 0.9, text_he: 0.9, ok_en: 0.9, bare_en: 0.9})
        s.j.spans.update({text_en: "are you up its late", text_he: "את ערה עכשיו בלילה",
                          ok_en: "see you tomorrow morning"})
        tm.SENT.clear()
        r = router.handle(s.j, text_en, speak=False)
        aw = router.AWAITING or {}
        check("en: a message against her rule is asked, not counted down",
              r["did"] == "confirm_send" and router.PENDING is None and not tm.SENT
              and aw.get("need") == "confirm_send" and aw.get("why") == "rule",
              f"did={r['did']} pending={router.PENDING} awaiting={short(aw)}")
        check("en: the rule is read back, then 'send anyway?'",
              "never message Dana after 10pm" in said(r) and "anyway?" in said(r)
              and "I am doing it anyway" not in said(r), said(r))
        r = tm._answer(s, "yes")
        check("en: her yes sends it, on the ordinary read-back",
              r["did"] == "sending" and (router.PENDING or {}).get("to") == "Dana",
              f"did={r['did']} pending={short(router.PENDING)}")
        router._cancel_pending()

        r = router.handle(s.j, text_en, speak=False)
        r = tm._answer(s, "no", yes="no")
        check("en: her no sends nothing", r["did"] == "send_declined"
              and router.PENDING is None and not tm.SENT, f"{r['did']} {router.PENDING}")

        r = router.handle(s.j, ok_en, speak=False)
        check("en: a message the rule does not cover goes as before",
              r["did"] == "sending" and "rule" not in said(r), f"{r['did']} {said(r)}")
        router._cancel_pending()

        r = router.handle(s.j, text_he, speak=False)
        check("he: asked first in Hebrew, the rule read back, no countdown",
              r["did"] == "confirm_send" and router.PENDING is None and r["lang"] == "hebrew"
              and "אל תשלחי הודעות לדנה אחרי עשר בלילה" in said(r) and "בכל זאת" in said(r),
              f"{r['did']} {said(r)}")
        r = tm._answer(s, "כן")
        check("he: her yes sends it", r["did"] == "sending"
              and (router.PENDING or {}).get("to") == "Dana", f"{r['did']} {said(r)}")
        router._cancel_pending()

        # No words yet: asked what it should say, and her answer is then asked about.
        r = router.handle(s.j, bare_en, speak=False)
        check("en: no words yet, asked what to say, the rule read first",
              r["did"] == "need_what" and said(r).startswith("You have a rule:"),
              f"{r['did']} {said(r)}")
        s.j.answer["are you still awake"] = (0.95, 0.05)
        r = router.handle(s.j, "are you still awake", speak=False)
        check("en: her words are then asked about, never counted down",
              r["did"] == "confirm_send" and router.PENDING is None and "anyway?" in said(r)
              and (router.AWAITING or {}).get("why") == "rule", f"{r['did']} {said(r)}")
        router.AWAITING = None

        r = router.handle(s.j, music, speak=False)
        check("not a send or a call: nothing asked, the rule said once as before",
              not str(r["did"]).startswith("confirm_") and router.AWAITING is None
              and said(r).startswith("You asked me: never play music after 10pm."),
              f"{r['did']} {said(r)}")
    fresh()


# ================================================================== recall and forget

@tm.with_gates_on
def t_prefs_recall_and_forget(_j):
    """"What do you know about me?" read back from what is on disk, with templates and
    no model call; "forget that ..." rewrites the file without it, and says so."""
    fresh()
    p0 = prof.load()
    p = dict(p0, name="Tova")
    prof.save(p)
    try:
        prefs.set_app("whatsapp", "Dana")
        prefs.set_confirm(True)
        prefs.add_rule("don't call anyone after 10pm")
        longterm.note("people", "Dana")
        longterm.note("people", "Matan")
        longterm.note("music", "Umm Kulthum")
        know_en, know_he = "what do you know about me", "מה את יודעת עליי"
        f_one, f_that, f_all_he = ("forget that I prefer WhatsApp for Dana", "forget that",
                                   "תשכחי הכל")
        f_none = "forget that I like jazz"
        understood = {
            know_en: dict(intent="chitchat", prefs={"pref_kind": ch("recall", 1.0)}),
            know_he: dict(intent="chitchat", language="hebrew",
                          prefs={"pref_kind": ch("recall", 1.0)}),
            f_one: dict(intent="chitchat", prefs={"pref_kind": ch("forget_one", 0.95),
                                                  "pref_forget": ("pick", "Dana")}),
            f_that: dict(intent="chitchat", prefs={"pref_kind": ch("forget_one", 0.9),
                                                   "pref_forget": ch(prefs.FORGET_LAST)}),
            f_none: dict(intent="chitchat", prefs={"pref_kind": ch("forget_one", 0.9),
                                                   "pref_forget": ch(prefs.FORGET_NONE)}),
            f_all_he: dict(intent="chitchat", language="hebrew",
                           prefs={"pref_kind": ch("forget_all", 0.97)}),
        }
        with pref_turns(understood) as s:
            n, llm0 = s.j.calls, s.llm.calls
            r = router.handle(s.j, know_en, speak=False)
            text = said(r)
            check("en: a short spoken list of what is on disk",
                  r["did"] == "recalled" and all(w in text for w in (
                      "Your name is Tova", "Messages to Dana go on WhatsApp",
                      "I ask you before sending any message",
                      "don't call anyone after 10pm", "Dana and Matan", "Umm Kulthum",
                      "forget any of it")), text)
            check("en: no model wrote it, and it cost no round trip beyond understanding",
                  s.llm.calls == llm0 and s.j.calls == n, f"llm={s.llm.calls - llm0} "
                  f"jev={s.j.calls - n}")
            r = router.handle(s.j, know_he, speak=False)
            check("he: the same, in Hebrew",
                  r["lang"] == "hebrew" and "קוראים לך Tova" in said(r)
                  and "בוואטסאפ" in said(r), said(r))

            r = router.handle(s.j, f_one, speak=False)
            check("en: 'forget that I prefer WhatsApp for Dana' removes that one",
                  r["did"] == "forgot" and prefs.load()["message_app"] == {}
                  and prefs.always_confirm(), f"{r['did']} {prefs.load()}")
            check("and says what it forgot",
                  said(r) == "Forgotten: Messages to Dana go on WhatsApp.", said(r))
            check("the forget question offered her real rows",
                  "pref_forget" in (s.standing[-1] or []), str(s.standing[-1]))

            prefs.set_volume_cap("low")            # the last thing she said
            r = router.handle(s.j, f_that, speak=False)
            check("en: a bare 'forget that' forgets the last thing she set",
                  r["did"] == "forgot" and prefs.volume_cap() == 0
                  and prefs.always_confirm(), f"{r['did']} {said(r)}")

            r = router.handle(s.j, f_none, speak=False)
            check("something she never told it: nothing is removed, and it asks which",
                  r["did"] == "forget_which" and prefs.always_confirm(), f"{r['did']} {said(r)}")

            r = router.handle(s.j, f_all_he, speak=False)
            check("he: 'forget everything' clears preferences and habits",
                  r["did"] == "forgot_all" and not prefs.items()
                  and not longterm.summary() and r["lang"] == "hebrew"
                  and said(r).startswith("שכחתי"), f"{r['did']} {said(r)}")
            check("her name is not part of it (that is setup, not a preference)",
                  prof.load().get("name") == "Tova")
            u = router.undo_last("hebrew")
            check("he: and Undo puts it all back",
                  u["undone"] and prefs.always_confirm() and prefs.rules()
                  and "Dana" in (longterm.summary().get("people") or []), f"{u} {prefs.items()}")

            prefs.forget_all()
            r = router.handle(s.j, f_that, speak=False)
            check("with nothing saved, it says there is nothing to forget",
                  r["did"] == "nothing_to_forget", r["did"])
    finally:
        prof.save(p0)
    fresh()


@tm.with_gates_on
def t_prefs_unsure_is_an_ordinary_request(_j):
    """A sentence that only happens to say "always": pref_kind unsure or "none" means
    the request is handled exactly as before, and nothing is stored."""
    fresh()
    song = "send Dana a message that I will always love her"
    understood = {song: dict(intent="message", contact="Dana", contact_named=0.95,
                             has_message_content=0.9,
                             prefs={"pref_kind": ch("message_app", 0.41)})}
    with pref_turns(understood) as s:
        s.j.same[song] = 0.9
        s.j.spans[song] = "I will always love her"
        r = router.handle(s.j, song, speak=False)
        check("an unsure reading is not a preference: the message is sent as asked",
              r["did"] == "sending" and not prefs.items(), f"{r['did']} {prefs.items()}")
        router._cancel_pending()
    fresh()


def t_prefs_music_app(_j):
    """"Always play music on Spotify": stored typed for the music path to read with
    prefs.music_app(); read back and forgotten like the rest."""
    fresh()
    en, he = "always play music on Spotify", "מעכשיו תמיד תשימי מוזיקה באפל מיוזיק"
    understood = {
        en: dict(intent="music", prefs={"pref_kind": ch("music_app", 0.95),
                                        "pref_music": ch("spotify")}),
        he: dict(intent="music", language="hebrew",
                 prefs={"pref_kind": ch("music_app", 0.95), "pref_music": ch("apple_music")}),
        "what do you know about me": dict(intent="chitchat",
                                          prefs={"pref_kind": ch("recall", 1.0)}),
        "forget that": dict(intent="chitchat", prefs={"pref_kind": ch("forget_one", 0.9),
                                                      "pref_forget": ch(prefs.FORGET_LAST)}),
    }
    check("nothing chosen reads as empty, so the caller keeps its default",
          prefs.music_app() == "")
    with pref_turns(understood) as s:
        r = router.handle(s.j, en, speak=False)
        check("en: stored, and read back through prefs.music_app()",
              r["did"] == "preference_set" and prefs.music_app() == "spotify"
              and "Spotify" in said(r) and not tm.YT_QUERIES, f"{r['did']} {said(r)}")
        r = router.handle(s.j, he, speak=False)
        check("he: the same", prefs.music_app() == "apple_music" and r["lang"] == "hebrew"
              and "אפל מיוזיק" in said(r), said(r))
        r = router.handle(s.j, "what do you know about me", speak=False)
        check("read back", "Music plays on Apple Music." in said(r), said(r))
        r = router.handle(s.j, "forget that", speak=False)
        check("and forgotten", r["did"] == "forgot" and prefs.music_app() == "", said(r))
    fresh()


def t_prefs_lines(_j):
    """Every line in four languages, no em dashes, and no deletion anywhere."""
    langs = ("english", "hebrew", "arabic", "russian")
    keys = set(prefs.SAY["english"])
    gaps = [(lg, sorted(keys - set(prefs.SAY[lg]))) for lg in langs if keys - set(prefs.SAY[lg])]
    check("every preference line exists in all four languages", not gaps, str(gaps))
    dashes = [v for lg in langs for v in prefs.SAY[lg].values() if "—" in v or "–" in v]
    check("no em or en dashes in anything she hears", not dashes, str(dashes))
    tm.t_no_delete_path(None)


TESTS = [
    ("P1. prefs cost nothing on a normal request", t_prefs_cost_nothing_on_a_normal_request),
    ("P2. prefs: which app a message goes by", t_prefs_message_app),
    ("P3. prefs: confirm, volume, video site, language", t_prefs_confirm_volume_site_language),
    ("P4. prefs: rules in her own words", t_prefs_free_rules),
    ("P4b. prefs: a send against a rule waits for her yes", t_prefs_rule_asks_before_send),
    ("P5. prefs: what do you know about me, forget", t_prefs_recall_and_forget),
    ("P6. prefs: an unsure reading changes nothing", t_prefs_unsure_is_an_ordinary_request),
    ("P7. prefs: which app music plays in", t_prefs_music_app),
    ("P8. prefs: lines and deletion", t_prefs_lines),
]


def main() -> int:
    for title, fn in TESTS:
        print(title)
        try:
            fn(None)
        except Exception:  # noqa: BLE001
            tm.FAILED.append((title, "raised: " + traceback.format_exc(limit=6)))
            print("  FAIL  raised\n          " + traceback.format_exc(limit=6).replace(
                "\n", "\n          "))
        fresh()
        print()
    print("-" * 78)
    for name, detail in tm.FAILED:
        print(f"  - {name}\n      {detail}")
    print(f"{tm.PASSED} passed, {len(tm.FAILED)} failed; "
          f"{len(tm.OSA_TOTAL)} osascript calls escaped the stub (must be 0)")
    return 1 if (tm.FAILED or tm.OSA_TOTAL) else 0


if __name__ == "__main__":
    code = main()
    tm._restore_profile()
    sys.exit(code)
