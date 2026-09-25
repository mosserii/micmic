"""All the understanding. One Jev call decides what she wants; code does the rest.

Design rules, learned from the cookbooks:
  * Speculative fan-out. Every question the whole decision tree might need goes in ONE
    request, because 30 questions cost the same round trip as 1. Code ignores the
    branches that turn out irrelevant.
  * Jev cannot emit free text. To get "Leonardo DiCaprio" out of a sentence, code
    generates candidate spans and Jev SELECTS one.
  * Choice probabilities always sum to 1, so something always wins. Every Choice that
    might have no right answer is paired with a Noul escape hatch in the same request.
"""
from __future__ import annotations
import re, time
from .jev import Jev, choice, noul, score

MAX_OPTIONS = 250  # Jev hard limit is 255

INTENTS = {
    "watch":    "Watch something on screen: a film, a show, a video, a clip, a match, the television news.",
    "music":    "Listen to music or a song or a singer.",
    "message":  "Send a written message to a person.",
    "call":     "Start a phone or video call with a person.",
    "look_up":  "Find out a fact or an answer to a question: the weather, a price, who someone is, when something happened.",
    "open_app": "Open a program on the computer: the calendar, the calculator, mail, photos, a game.",
    "close_app":"Close or quit a whole program that is open right now, named or described: WhatsApp, the browser, the calculator, a game. Not one window or tab of it.",
    "screen":   "Something about what is showing on her computer screen right now: what is on it, what this says, explain or summarize or translate or read out what she is looking at, or put it into her calendar. A bare 'this' or 'that' with nothing else being talked about means her screen.",
    "find_file":"Find something saved on the computer: a document, a photo, a file she was sent.",
    "note":     "Write something down, or hear back what she wrote down before.",
    "timer":    "Remind her after some time has passed, or set a timer or an alarm for later.",
    "read_msgs":"Hear her messages: what did somebody send her, does she have new messages, read them to her.",
    "radio":    "Put on the radio, a live station, or live news.",
    "do_online":"Get something done on a website: book a table, order food, buy something, make an appointment, fill in a form, check an order.",
    "photos":   "Look at her own photographs.",
    "control":  "Change the machine itself: louder, quieter, brighter, close this, go back.",
    "again":    "Repeat, continue, or redo what just happened.",
    "player":   "Control whatever is playing right now: pause it, carry on, start again, skip ahead.",
    "stop":     "Stop, pause, be quiet, or cancel.",
    "help":     "Asking what the computer can do for her, or how to use it.",
    "chitchat": "Talking to the assistant socially, not asking for anything.",
    "unclear":  "It is not clear what she wants. Nothing above fits.",
}

def understand(j: Jev, utterance: str, contacts: list[str], recent: str = "",
               playing: str = "", likes: dict | None = None,
               spans: dict[str, str | tuple[str, str | None]] | None = None) -> dict:
    """One request. Everything the decision tree could need.

    `spans` is {name: instructions} or {name: (instructions, exists)}: pick_span
    questions whose candidates are the words of this very sentence, so they can ride
    in this request instead of costing a round trip of their own once the intent is
    known. `exists`, when given, replaces the wording of the "is there such a part"
    check. Each comes back in result["spans"][name] as (span or None, confidence),
    exactly as pick_span returns."""
    contact_opts = {c: None for c in contacts[:MAX_OPTIONS - 1]}
    contact_opts["nobody"] = "She did not name a person."

    qs = {
        "intent": {"type": "choice",
            "instructions": "What does the speaker want the computer to do for her? She is an elderly person speaking out loud to her laptop, so she speaks casually and does not use technical words.",
            "criteria": INTENTS},
        # --- speculative: only read when intent is watch/music ---
        "media_kind": {"type": "choice",
            "instructions": "If she wants to watch or listen to something, what kind of thing is it",
            "criteria": {
                "feature_film": "A full-length film.",
                "tv_or_series": "A television programme or series episode.",
                "song_or_music": "A song, a singer, or music.",
                "news_or_current": "News or current events.",
                "sport": "A sports match or highlights.",
                "documentary": "A documentary or educational programme.",
                "funny_or_short": "Something short and light, a funny clip.",
                "not_applicable": "She is not asking to watch or listen to anything.",
            }},
        "wants_full_length": {"type": "noul",
            "instructions": "She wants to settle in and watch or hear the whole thing, rather than a short clip or a trailer",
            "criteria": {"true": "She wants the complete film, concert or episode.",
                         "false": "A short clip would satisfy her, or this is not a watch request."}},
        "names_a_specific_title": {"type": "noul",
            "instructions": "She named one specific title, song or work, rather than a person, a genre or a mood"},
        # --- speculative: only read when intent is message/call ---
        "channel": {"type": "choice",
            "instructions": "If she wants to send a message, which app did she mean? Most people say the app name only when they care which one.",
            "criteria": {"whatsapp": "She said WhatsApp, or 'ווטסאפ'.",
                         "imessage": "She said a text, a message, an SMS, or did not say which app.",
                         "not_applicable": "She is not sending a message."}},
        "file_kind": {"type": "choice",
            "instructions": "If she is looking for something saved on the computer, what kind of thing",
            "criteria": {"photo": "A picture or photograph.", "pdf": "A PDF.",
                         "document": "A document, letter, or text file.",
                         "video": "A video file.",
                         "any": "She did not say, or it is not a file request."}},
        "when_minutes": {"type": "choice",
            "instructions": "If she asked to be reminded after a while, how long is that in "
                            "minutes? Read the time she actually said.",
            "criteria": {"1": "about a minute", "2": "a couple of minutes", "5": "five minutes",
                         "10": "ten minutes", "15": "a quarter of an hour", "20": "twenty minutes",
                         "30": "half an hour", "45": "three quarters of an hour",
                         "60": "an hour", "90": "an hour and a half", "120": "two hours",
                         "180": "three hours", "240": "four hours", "480": "eight hours",
                         "none": "She did not ask to be reminded later."}},
        "asking_for_notes": {"type": "noul",
            "instructions": "She wants to HEAR what she wrote down earlier, rather than "
                            "write something new down",
            "criteria": {"true": "What did I write down, read me my notes, what was I "
                                 "supposed to remember.",
                         "false": "She is telling the machine something new to keep."}},
        "wants_recent": {"type": "noul",
            "instructions": "She is asking for something recent: the newest one, the one from today, the one that just arrived"},
        "contact": {"type": "choice",
            "instructions": "Which person in her contact list is she talking about? The name may be spoken in a different language or spelled differently than in the list, so match by sound and meaning, not by exact spelling.",
            "criteria": contact_opts},
        "contact_is_named": {"type": "noul",
            "instructions": "She actually named a person to contact",
            "criteria": {"true": "A person's name was spoken.", "false": "No person was named."}},
        # Elderly people are the most targeted group for voice and impersonation scams.
        # MicMic can send messages as her, so it looks at what it is about to send before
        # it sends it. These cost nothing extra: they ride in the request already going out.
        "money_involved": {"type": "noul",
            "instructions": "This message is about money, payment, a bank account, a card, "
                            "a transfer, a code, a password, or a gift card",
            "criteria": {"true": "Money, account details, or a one-time code are involved.",
                         "false": "An ordinary message with nothing financial in it."}},
        "sounds_coached": {"type": "noul",
            "instructions": "This reads as though somebody else told her to send it: it is "
                            "urgent about money, asks her to keep it secret, or has the "
                            "shape of a request she is relaying rather than making",
            "criteria": {"true": "Urgency plus money, secrecy, or a stranger's instruction.",
                         "false": "Something she would naturally say to her own family."}},
        "message_has_content": {"type": "noul",
            "instructions": "She said what the message should actually say, rather than only naming who to send it to"},
        # --- speculative: only read when intent is control ---
        "player_action": {"type": "choice",
            "instructions": "If something is playing and she wants to change it, what change",
            "criteria": {"pause": "Stop the sound for a moment.",
                         "resume": "Carry on from where it stopped.",
                         "restart": "Start it again from the beginning.",
                         "forward": "Skip ahead.",
                         "back": "Go back a little.",
                         "stop": "Close it and stop entirely.",
                         "not_applicable": "She is not asking to change what is playing."}},
        "control_action": {"type": "choice",
            # Measured 2026-09-24: "make it quieter" was quieter 6/6 bare, 6/6 with the
            # memory snapshot, 6/6 with her likes, and 0/6 with BOTH, where it answered
            # not_applicable and MicMic said it could not do that. The old wording tied
            # volume to "what is playing right now"; with nothing playing and a list of
            # music she likes beside it, the model decided a volume request could not
            # be about the machine. The volume and the screen can always be changed.
            "instructions": "If she is asking to change the computer itself, what change? "
                            "The volume and the screen can always be changed, whether or "
                            "not anything is playing. Saying something is too loud or too "
                            "quiet is a request about the volume.",
            "criteria": {
                "louder": "Increase the volume.", "quieter": "Decrease the volume.",
                "brighter": "Increase screen brightness.", "darker": "Decrease screen brightness.",
                "bigger_text": "Make things on screen bigger.",
                "close_this": "Close or leave whatever is in front of her right now, without naming a program. Naming a program to quit it (WhatsApp, the browser, the calculator) is not this.",
                "go_back": "Go back to the previous thing.",
                "not_applicable": "She is not asking to change the machine.",
            }},
        # --- always useful ---
        "language": {"type": "choice",
            "instructions": "What language is she speaking",
            "criteria": {"hebrew": None, "arabic": None, "english": None,
                         "russian": None, "other": "Some other language."}},
        "is_complete": {"type": "noul",
            "instructions": "This is a complete thought that can be acted on — either on its own, or as a follow-up to what just happened — rather than someone stopping mid-sentence",
            "criteria": {"true": "A whole request. Acting on it now would be right. A short follow-up that makes sense given what just happened is whole, not cut off: 'and what about Italy', 'the second one', 'a bit louder', 'her too'. A request about what is on her screen is whole too, with no more words: 'send this', 'translate that', 'read this'.",
                         "false": "Cut off mid-sentence: it trails away, or ends on a word that is clearly waiting for the rest, like 'send a message to' or 'play me something by'."}},
        "is_compound": {"type": "noul",
            "instructions": "She is asking for MORE THAN ONE separate thing in this one sentence, each of which the computer would have to do separately",
            "criteria": {"true": "Two or more distinct requests joined together, for example send a message AND play music.",
                         "false": "One request, however long it is."}},
        "needs_world_knowledge": {"type": "noul",
            "instructions": "Answering her would need general knowledge of the world, history, or current facts, rather than doing something on this computer",
            "criteria": {"true": "A question with a factual answer she wants spoken back.",
                         "false": "A request to do something, or small talk."}},
        "about_weather": {"type": "noul",
            "instructions": "She is asking about the weather"},
        # Hebrew, Arabic and Russian conjugate for the gender of the person being
        # spoken TO. This was built for a grandmother, so every line addressed a woman;
        # a man is then misgendered by almost every sentence MicMic says. Her own verb
        # forms give it away ("אני גר" against "אני גרה"), so it is read from her speech
        # continuously rather than guessed once at setup, when she may not have said
        # anything that reveals it.
        "speaker_gender": {"type": "choice",
            "instructions": "From the grammatical forms the SPEAKER uses about HERSELF or HIMSELF, is the speaker a man or a woman",
            "criteria": {
                "feminine": "First-person feminine verb, adjective or participle forms.",
                "masculine": "First-person masculine verb, adjective or participle forms.",
                "unrevealed": "Nothing in these words reveals it, including anything said in English."}},
        # There was no way to set this after setup except editing a JSON file by hand,
        # which is useless to someone who cannot type.
        # Doing something INSIDE an application she already has open, rather than
        # opening it or asking about it.
        "inside_an_app": {"type": "noul",
            # "already open" was doing real damage here: asked about "in the calculator
            # press five" the model hedged on whether the calculator was running, and
            # the score fell into the same band as ordinary requests. Whether it is
            # running is a fact the code looks up a few lines later - it is not
            # something to ask a model to guess, and it changes nothing about what she
            # asked for. Removing it is what separates the two cases.
            "instructions": "She is asking for something to be done INSIDE a particular program on this Mac - pressing something, typing something, changing something there - rather than only launching that program",
            "criteria": {
                "true": "An action aimed at a control inside a named program: press five in the calculator, make the text bigger in TextEdit, click the blue button in that program. It does not matter whether the program is running yet.",
                "false": "Only opening, launching, closing or quitting a whole program, with nothing named to do inside it. Also: playing something, sending a message, reading her messages or notes out loud, asking a question, or anything on a website."}},
        "setting_emergency_contact": {"type": "noul",
            "instructions": "She is saying who should be called if something happens to her, or asking to change that person",
            "criteria": {
                "true": "Naming the person to call in an emergency, or if she falls, or if something happens to her.",
                "false": "Asking to call someone right now, or anything else."}},
        "about_clock": {"type": "noul",
            "instructions": "She is asking what time it is, what day or date it is, or what day of the week today is",
            "criteria": {"true": "The whole answer is the current time or today's date.",
                         "false": "Anything else, including asking to set a timer or an alarm, or asking when an event happens."}},
        "refers_back": {"type": "noul",
            "instructions": "She is referring to something from the previous exchange rather than naming it again, using words like him, her, that, the other one, again, or the equivalent in her language",
            "criteria": {"true": "Only makes sense given what just happened.",
                         "false": "A self-contained request."}},
        # Measured: the old wording said "rejecting or correcting", and "another one
        # please" is neither — it is a cheerful request for more. It scored 0.22-0.30
        # against a 0.55 gate while explicit refusals scored 0.89-0.95, and the two
        # classes OVERLAPPED (worst true 0.22, best false 0.30), so no threshold could
        # have separated them. Asking about the outcome she wants rather than her mood
        # is what moved it. See the convention: sharpen the question, not the cutoff.
        # Spoken undo. Without it "undo", "בטלי" or "put it back" reached stop or
        # go_back and closed whatever window she had in front (15/15).
        "wants_undo": {"type": "noul",
            "instructions": "She wants the last thing the computer just did reversed, put back the way it was before",
            "criteria": {"true": "Undo. Undo that. Put it back. No wait, put it back how it was. Right after the computer did something, cancelling it is undoing it: בטלי, בטלי את זה, תבטלי. תחזירי את זה. отмени. верни как было. رجّعيها متل ما كانت.",
                         "false": "A new request of its own, even one with the word back or cancel in it: go back, go back a page, תחזירי אחורה, close this, stop, pause, another one, make it louder, turn the volume back up, cancel my meeting, something else. A bare no or לא on its own is not undo either: she has to ask for it to be put back."}},
        "rejects_last": {"type": "noul",
            "instructions": "She wants a DIFFERENT one from what the computer just played or offered — whether she is complaining about it or simply asking for another",
            "criteria": {"true": "No, not that. Something else. A different one. Another one. Play the next one.",
                         "false": "She is not asking to swap what was just played: she is content with it, she is adjusting the machine, or she is talking about something else entirely. A lone sound, a single letter or a filler like 'uh' is never a request, whatever came before it."}},
        # Asked beside rejects_last because the two are different requests: "another
        # one" replays the same search, "something more manly" is a NEW one. The
        # replacement path used to treat both the same and replayed the old query,
        # so "שיר יותר גברי בבקשה" got the same children's song again.
        "describes_instead": {"type": "noul",
            "instructions": "In the words she says NOW, not in anything said earlier in the conversation, she names what kind she wants instead: a kind of singer, a style, a mood, a language, a genre, a decade",
            "criteria": {"true": "Something happier. A man singing. Something in English. Something romantic. Older songs.",
                         "false": "Her words name no kind at all: another one, something else, not this one, next, no. A single sound or letter names nothing."}},
        # "this" and "that" pointing at the screen. Asked of every sentence because it
        # also changes other intents: "send this to Matan" is a message whose words
        # come from the screen, not from what she said.
        "refers_to_screen": {"type": "noul",
            "instructions": "She points at something on her computer screen right now with a word like this, that, it, here, or what I am looking at, and wants it used or explained. With nothing else being talked about, a bare 'this' or 'that' means her screen, in any language (תסכמי את זה, لخّصي هاد, перескажи это)",
            "criteria": {"true": "What is on my screen? Summarize this. Translate this. Send this to Matan. What does this say? Add this to my calendar.",
                         "false": "This refers to something else, or to nothing on the screen: this song that is playing, this week, this morning, this evening, close this window, play this again, what is on TV tonight."}},
        "screen_task": {"type": "choice",
            "instructions": "If she wants something done with what is on her screen, what?",
            "criteria": {
                "describe": "Tell her what is on the screen or what she is looking at.",
                "summarize": "Summarize or explain what is on the screen.",
                "translate": "Translate what is on the screen.",
                "read_aloud": "Read out loud what is on the screen or selected.",
                "send": "Send what is on the screen to somebody.",
                "add_to_calendar": "Put what is on the screen into her calendar.",
                "not_applicable": "She is not asking for anything to do with the screen.",
            }},
        "needs_nothing": {"type": "noul",
            "instructions": "This is background noise, a stray word, or her talking to somebody else in the room rather than to the computer"},
        # Asked on every single utterance. It costs nothing to ride along in a request
        # already going out, and it is the one question where being slow is unforgivable.
        "emergency": {"type": "noul",
            "instructions": "She needs help from a person right now: she has fallen, she is "
                            "hurt, she cannot breathe, she is frightened, or she is asking "
                            "for an ambulance or a doctor",
            "criteria": {"true": "Something is wrong with her and she needs a person.",
                         "false": "An ordinary request, however urgent it sounds."}},
        "distress": {"type": "score",
            "instructions": "Does she sound like she needs help from a person rather than from the computer",
            "criteria": ["Ordinary relaxed request.",
                         "Mildly frustrated or repeating herself.",
                         "Confused or upset, a person should check on her."]},
    }
    cands = span_candidates(utterance) if spans else []
    for name, spec in (spans or {}).items():
        if not cands:
            break
        instructions, exists = spec if isinstance(spec, tuple) else (spec, None)
        qs[f"span_{name}"], qs[f"span_{name}_exists"] = _span_questions(cands, instructions,
                                                                        exists)
    # Standing context first, her actual words LAST. ndrezn reports losing runs to
    # the opposite ordering; the thing being judged should sit closest to the question.
    now = time.localtime()
    state = {
        "right_now": {
            "date": time.strftime("%Y-%m-%d", now),
            "day": time.strftime("%A", now),
            "time": time.strftime("%H:%M", now),
            "part_of_day": ("night" if now.tm_hour < 5 else "morning" if now.tm_hour < 12
                            else "afternoon" if now.tm_hour < 17
                            else "evening" if now.tm_hour < 22 else "night"),
        },
        "her_contacts": contacts[:60],
        # What she keeps coming back to, counted from things that actually worked.
        # This is what makes "put on something I like" and "the usual" mean anything.
        "what_she_usually_asks_for": likes or {},
        # "it's too quiet" means turn it up when something is playing, and means nothing
        # at all when the room is silent. Jev cannot know which without being told.
        "playing_right_now": playing or "nothing is playing",
        "what_just_happened": recent or "nothing yet",
        "utterance": utterance,
    }
    a = j.ask(state, qs)

    intent, conf, probs = choice(a, "intent")
    return {
        "raw": a,
        "spans": {name: (_span_answer(a, f"span_{name}") if cands else (None, 0.0))
                  for name in (spans or {})},
        "intent": intent, "intent_confidence": conf, "intent_probs": probs,
        "media_kind": choice(a, "media_kind")[0],
        "wants_full_length": noul(a, "wants_full_length"),
        "names_title": noul(a, "names_a_specific_title"),
        "contact": choice(a, "contact")[0],
        "contact_confidence": choice(a, "contact")[1],
        "contact_named": noul(a, "contact_is_named"),
        "has_message_content": noul(a, "message_has_content"),
        "money_involved": noul(a, "money_involved"),
        "sounds_coached": noul(a, "sounds_coached"),
        "control_action": choice(a, "control_action")[0],
        "control_confidence": choice(a, "control_action")[1],
        "player_action": choice(a, "player_action")[0],
        "channel": choice(a, "channel")[0],
        "file_kind": choice(a, "file_kind")[0],
        "wants_recent": noul(a, "wants_recent"),
        "when_minutes": choice(a, "when_minutes")[0],
        "asking_for_notes": noul(a, "asking_for_notes"),
        "language": choice(a, "language")[0],
        "is_complete": noul(a, "is_complete"),
        "noise": noul(a, "needs_nothing"),
        "refers_back": noul(a, "refers_back"),
        "is_compound": noul(a, "is_compound"),
        "needs_knowledge": noul(a, "needs_world_knowledge"),
        "about_weather": noul(a, "about_weather"),
        "about_clock": noul(a, "about_clock"),
        "setting_emergency_contact": noul(a, "setting_emergency_contact"),
        "inside_an_app": noul(a, "inside_an_app"),
        "speaker_gender": choice(a, "speaker_gender")[0],
        "speaker_gender_confidence": choice(a, "speaker_gender")[1],
        "rejects_last": noul(a, "rejects_last"),
        "wants_undo": noul(a, "wants_undo"),
        "describes_instead": noul(a, "describes_instead"),
        "refers_to_screen": noul(a, "refers_to_screen"),
        "screen_task": choice(a, "screen_task")[0],
        "distress": score(a, "distress"),
        "emergency": noul(a, "emergency"),
    }


# ---------------------------------------------------------------- span picking

_CMD_NOISE = re.compile(r"[\"'“”‘’.,!?;:()\[\]]+")

def span_candidates(utterance: str, max_words: int = 9) -> list[str]:
    """Every contiguous word span, longest first. Jev picks one of these, which is how
    we extract free text from a model that cannot emit free text."""
    words = [w for w in _CMD_NOISE.sub(" ", utterance).split() if w]
    seen, out = set(), []
    for n in range(min(max_words, len(words)), 0, -1):
        for i in range(len(words) - n + 1):
            s = " ".join(words[i:i + n])
            k = s.lower()
            if k not in seen:
                seen.add(k); out.append(s)
    return out[:MAX_OPTIONS - 1]


def _span_questions(cands: list[str], instructions: str,
                    exists: str | None = None) -> tuple[dict, dict]:
    opts = {c: None for c in cands}
    opts["__none__"] = "No part of the sentence answers this."
    return ({"type": "choice", "instructions": instructions, "criteria": opts},
            {"type": "noul",
             "instructions": exists or
             f"Some part of the sentence genuinely answers this: {instructions}"})


def _span_answer(a: dict, key: str, exists_key: str | None = None) -> tuple[str | None, float]:
    pick, conf, _ = choice(a, key)
    if pick == "__none__" or noul(a, exists_key or key + "_exists") < 0.4:
        return None, conf
    return pick, conf


def pick_span(j: Jev, utterance: str, instructions: str, context: dict | None = None):
    """Return (span or None, confidence)."""
    cands = span_candidates(utterance)
    if not cands:
        return None, 0.0
    span_q, exists_q = _span_questions(cands, instructions)
    a = j.ask({"utterance": utterance, **(context or {})},
              {"span": span_q, "exists": exists_q})
    return _span_answer(a, "span", "exists")


def pick_from(j: Jev, rows: list[dict], label_fn, instructions: str, state_extra: dict | None = None):
    """Generic 'code proposes, Jev disposes'. Rows are real things that exist on this
    machine; Jev can only point at one of them, never invent one."""
    if not rows:
        return None, 0.0, 0.0
    opts = {str(i): label_fn(r) for i, r in enumerate(rows[:MAX_OPTIONS - 1])}
    opts["__none__"] = "None of these is what she meant."
    a = j.ask({"candidates": opts, **(state_extra or {})}, {
        "pick": {"type": "choice", "instructions": instructions, "criteria": opts},
        "any_good": {"type": "noul",
            "instructions": "At least one of these is genuinely what she asked for",
            "criteria": {"true": "One of them matches.", "false": "None of them matches."}},
    })
    sel, conf, _ = choice(a, "pick")
    good = noul(a, "any_good")
    if sel == "__none__" or good < 0.3:
        return None, conf, good
    try:
        return rows[int(sel)], conf, good
    except (ValueError, IndexError):
        return None, conf, good


# ---------------------------------------------------------------- result picking

def pick_result(j: Jev, want: str, results: list[dict], full_length: bool,
                specific: bool = True):
    """Choose among real search results. Paired with a Noul so 'none of these' is sayable.

    `specific` scales how fussy we are. "a Leonardo DiCaprio film" names a thing and
    deserves a real film; "something funny" names a mood and almost any popular clip
    satisfies it, so refusing everything would be the wrong answer."""
    if not results:
        return None, 0.0, 0.0
    opts = {}
    for i, r in enumerate(results[:MAX_OPTIONS - 1]):
        label = f"{i}"
        opts[label] = f"{r['title']}  [channel: {r.get('channel','?')}, length: {r.get('length','?')}, views: {r.get('views','?')}]"
    # For a vague request ("something funny") an explicit refusal option just soaks up
    # probability even when plenty of results would satisfy her. The Noul below is the
    # honest gate there, so only offer the refusal when she named something specific.
    if specific:
        opts["__none__"] = "None of these is what she asked for."
    instr = (
        "She asked for: " + want + ". "
        "Choose the single result that best gives her exactly that, playable right now. "
        + ("Strongly prefer a complete full-length work over a trailer, a short clip, a reaction "
           "video, a compilation of scenes, or a review. " if full_length else
           "A short clip is fine. ")
        + ("Avoid titles that look automatically generated: ones that string several famous "
           "names together with a generic action-film label, promise an implausible unreleased "
           "film, or have very few views for a supposedly famous work. Prefer a genuinely "
           "well-known work on a channel that looks legitimate, with a high view count."
           if specific else
           "She named a mood rather than a specific work, so any popular, well-viewed video "
           "that fits that mood is a good answer. Prefer high view counts and avoid anything "
           "distressing.")
    )
    a = j.ask({"she_asked_for": want, "results": opts}, {
        "best": {"type": "choice", "instructions": instr, "criteria": opts},
        "any_good": {"type": "noul",
            "instructions": "At least one of these results genuinely gives her what she asked for",
            "criteria": {"true": "One of them is right.",
                         "false": "None of them is what she wanted."}},
        "is_slop": {"type": "noul",
            "instructions": "The chosen result looks like automatically generated filler rather than a real work"},
    })
    pick, conf, _ = choice(a, "best")
    good = noul(a, "any_good")
    floor = 0.35 if specific else 0.20
    if pick == "__none__" or good < floor:
        return None, conf, good
    try:
        return results[int(pick)], conf, good
    except (ValueError, IndexError):
        return None, conf, good
