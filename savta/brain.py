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

# Languages a message can be asked to go out in. The translation itself is the
# language model's job; Jev only says which one she asked for.
WRITE_IN = {"not_applicable": "She did not ask for the message to be in a particular language.",
            "english": None, "hebrew": None, "arabic": None, "russian": None,
            "french": None, "spanish": None, "german": None, "italian": None,
            "portuguese": None}

# Asked only while a message is being prepared (see understand's `draft`). The owner
# said "send it on WhatsApp" right after a message, and was asked who to send it to
# and what it should say, as if nothing had happened.
DRAFT_QUESTIONS = {
    "amends_message": {"type": "noul",
        "instructions": "She is changing or repeating the message in message_being_prepared: sending it to a different person, with different words, in another language or by another app, or sending it after all, rather than asking for a new, separate message",
        "criteria": {"true": "Send it on WhatsApp. Send her on WhatsApp. No, to Dana. Send it to Matan instead. No wait, send it in French. Say I am running late instead. Send it again. תשלחי את זה בוואטסאפ. לא, לדנה. תכתבי את זה באנגלית. במקום זה תגידי שאני מאחרת. Отправь это в WhatsApp.",
                     "false": "A new message with its own person and words: send a message to Gal that dinner is at eight, tell Nir happy birthday, תשלחי הודעה לדנה שאני בדרך. Only stopping it: no, stop, don't send it, לא, עצרי. Anything that is not about sending a message: play music, what is the weather, call Dana."}},
    "message_app": {"type": "choice",
        "instructions": "Did she name the app the message should go by?",
        "criteria": {"whatsapp": "She said WhatsApp, or ווטסאפ.",
                     "imessage": "She asked for a text message, an SMS or iMessage by name.",
                     "unchanged": "She did not name an app."}},
}

# Asked only while MicMic is guiding her through something on screen, one step at a
# time (savta/actions/guide.py). "next" or "why" on its own means nothing without it.
# "none" first: a replay that never saw this question defaults to the first option.
GUIDE_QUESTIONS = {
    "guide_command": {"type": "choice",
        "instructions": "MicMic is walking her through a task on her screen one step at a time (guide_in_progress). Is what she says now about that guidance?",
        "criteria": {
            "none": "No: a new, separate request or question, or anything that is not about the steps.",
            "next": "She did the step or wants the next one: done, next, I did it, okay what now, got it. עשיתי, הבא, מה עכשיו. تمّ، التالي. готово, дальше.",
            "why": "She asks what this step is or why she should do it: what's this, why, what does that do. למה, מה זה. ليش، شو هاد. зачем, что это.",
            "back": "She wants the step before this one again: go back, the previous step. חזרה, תחזרי אחורה. ارجع. назад.",
            "repeat": "She wants this step said again: say that again, repeat, what did you say. תחזרי על זה. عيدي. повтори.",
            "stop": "She wants the guiding to end: stop, enough, stop guiding, I will do it myself. די, עצרי, תפסיקי. وقّفي، خلص. хватит, стоп."}},
}

# Asked only when her words may describe a song instead of naming it and a song is
# plausible (actions/music.describes_a_song). The owner's session (1.1.0): "please
# change to our latest world cup song" searched "shakira" and played another of her
# songs. A song she describes is identified first (router._identify_song).
SONG_DESC_QUESTIONS = {
    "describes_song": {"type": "noul",
        "instructions": "She wants one particular song but describes it instead of saying its title: by when it came out (the newest, the latest, the first), by what it was made for or is known from (a film, a series, the World Cup, Eurovision, weddings), or by what it is about or how it goes",
        "criteria": {"true": "Play Shakira's newest song. Put on the song from Titanic. Change to our latest World Cup song. The one they play at weddings. The theme from Friends. השיר החדש של עומר אדם. أغنية كأس العالم الأخيرة. песню из Титаника.",
                     "false": "She says the song's title (play Waka Waka, put on 1969, the song Hallelujah), names only a singer or a band (play Bad Bunny, put on Shakira), asks for a kind of music (something happy, older songs, a man singing), asks for another one, something else or the next one, or is not asking for a song at all."}},
}

# Asked only when her words may be pointing at the screen instead of naming a song
# (actions/music.mentions_screen_song). The owner's session: "play the song i see on
# my screen" searched the literal words "see screen" and played the wrong thing.
SCREEN_SONG_QUESTIONS = {
    "screen_song": {"type": "noul",
        "instructions": "She wants a particular song to play, but instead of naming it (no title, no artist of her own) she is pointing at her screen for it: this one, this song, that one, the song she sees on her screen, telling MicMic to look at the screen for the song she wants.",
        "criteria": {"true": "Play this one. Play this song. Play the song I see on my screen. No, look at my screen, you will see the song I want to play. תשימי את השיר הזה. תסתכל במסך, שם השיר שאני רוצה. شغل هاي الأغنية يلي عالشاشة. включи эту песню, она на экране.",
                     "false": "She names a song or an artist of her own (play Waka Waka, put on Bad Bunny), asks for a kind of music, rejects what is playing (no, not this one, something else) with nothing about a screen, or is not asking for a song at all."}},
}

# Asked only when her words may be aimed at MicMic itself (people.may_be_at_micmic:
# "useless", "stupid", "מטומטמת", "тупая"...). The bench (2026-10-01): "you're useless" scored
# distress over the gate and was answered "You sound upset. Shall I call Dana Cohen?".
# Being annoyed with the computer is not needing a person.
AT_MICMIC_QUESTIONS = {
    "upset_with_micmic": {"type": "noul",
        "instructions": "She is annoyed or frustrated with the computer assistant itself, or insulting it, rather than upset about something happening to her or to someone she knows",
        "criteria": {"true": "You're useless. You never understand me. Stupid machine. את מטומטמת. ты тупая. إنتي ما بتفهمي.",
                     "false": "She is hurt, frightened, lonely, being pressured or in danger, or someone she knows is: I fell, I'm scared, someone is at the door, a man says I must pay him. Can you help me, I don't feel well. Or an ordinary request."}},
}

# Asked only when her words may be asking for a detail of something open on her
# screen (actions/screen.may_ask_about_screen: "my flight", "the booking code", "how
# many eggs", "what time does my..."). The bench (2026-10-01): with a flight
# confirmation in front of her, "what time does my flight leave" was answered "I do
# not have access to your personal travel information" (refers_to_screen 0.07: no
# "this", no "screen"), and with a recipe saying 4 eggs, "how many eggs" got "2".
# Jev never sees the screen; it judges only whether the answer would be written in
# something she has open. Code then reads the screen and grounds the answer in it.
SCREEN_ASK_QUESTIONS = {
    "screen_question": {"type": "noul",
        "instructions": "She asks for a particular detail that would be written in something she has open on her computer right now: her own booking, flight, ticket, order, appointment, bill, an email, a recipe, a document or the page she is reading. Not a fact about the world in general",
        "criteria": {"true": "What time does my flight leave? What's my booking code? Which seat do I have? How many eggs do I need? How many eggs does it need? How much is the total? When is my appointment? What is the order number? מתי הטיסה שלי? מה קוד ההזמנה? כמה ביצים צריך? كم بيضة لازم؟ متى رحلتي؟ Во сколько мой рейс? Сколько яиц нужно?",
                     "false": "A fact about the world or a general how-to: how many eggs are in a dozen, how many people live in Paris, what time is it, when did the war end, how much is a bitcoin, how many minutes in an hour, what is the weather. Or a request to do something: play, send, call, open, set a timer, remind me."}},
}

# Asked only when the microphone was open without her pressing the key (the wake word,
# the open mic, the window after a countdown): then the words may not be for MicMic at
# all. Bench v1 (2026-10-01): a TV line on an open mic started pop music, and "honey
# did you take the keys" got a chatty answer, because the old filter only looked at
# unclear or small-talk intents. A push-to-talk turn never asks this, so its request
# is exactly what it was. "assistant" first: a replay that never saw this question
# defaults to the first option, which keeps acting as before.
OPEN_MIC_QUESTIONS = {
    "addressed_to": {"type": "choice",
        "instructions": "The microphone was open without a button being pressed, so these words may not have been meant for MicMic, the voice assistant on this computer. Who were they said to, or where did they come from?",
        "criteria": {
            "assistant": "To MicMic: a request, an instruction or a question for the computer, however casual, or a word to it like thanks, stop or good night. It may be indirect (it is too cold in here for this music, I cannot read this, what did I note down) or have um, uh and pauses in it. Put on some jazz, what is the weather tomorrow, message Noa that I am on my way, call my sister, set a timer for the oven, louder, stop that, tell me something funny, thanks. תשימי שיר של אריק איינשטיין, מה השעה, תתקשרי לבת שלי. شغلي أغاني فيروز. позвони дочке.",
            "someone_in_room": "To another person, in the room or on a phone call: a question or remark to family or a friend, plans, a goodbye at the end of a call. Did you feed the dog, Mike dinner is ready, we have to leave by six, okay talk to you later bye, I will bring it on Sunday. האכלת את הכלב, אנחנו יוצאים בשש. أكلت؟ ты покормил кота?",
            "broadcast": "From a television, radio, video, podcast or song playing nearby: a presenter, a news anchor, an advert, lines from a show or film, song lyrics. Stay with us after the break, this is the nine o'clock news, the detective opened the door slowly, back to you in the studio. ואלה החדשות, נמשיך אחרי הפרסומות. هنا الأخبار. в эфире новости.",
            "no_one": "Not words for anyone: a cough, a sneeze, a laugh, um, a stray word or sound, someone thinking aloud to themselves."}},
}

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
               spans: dict[str, str | tuple[str, str | None]] | None = None,
               draft: dict | None = None, standing: dict | None = None,
               guide: dict | None = None, named: dict | None = None,
               follow: dict | None = None,
               song_desc: bool = False, screen_song: bool = False,
               at_micmic: bool = False,
               open_mic: bool = False,
               screen_ask: bool = False,
               when: dict | None = None) -> dict:
    """One request. Everything the decision tree could need.

    `standing` is savta.prefs.questions(): the preference questions when her words
    can state, recall or forget a preference, and her saved rules that this sentence
    plausibly touches. None on an ordinary request, which is then sent exactly as it
    would be without preferences at all.

    `named` is savta.actions.targets.question(): asked only when her words name an app,
    a website or a service ("on Netflix", "search it on Amazon", "in Spotify"), which
    of those, if any, is where she wants it done. The owner said "please put 1969 song
    on apple music" and got YouTube: nothing asked where. Without it nothing is asked
    and the answer is "none".

    `draft` is the message this conversation has just prepared, sent or stopped
    ({"to", "text", "channel"}). While there is one, two more questions ride along:
    is she changing that message, and did she name an app. Without one they are not
    asked at all, so every other request is judged exactly as it was.

    `spans` is {name: instructions} or {name: (instructions, exists)}: pick_span
    questions whose candidates are the words of this very sentence, so they can ride
    in this request instead of costing a round trip of their own once the intent is
    known. `exists`, when given, replaces the wording of the "is there such a part"
    check. Each comes back in result["spans"][name] as (span or None, confidence),
    exactly as pick_span returns.

    `guide` is the step-by-step guide running right now ({"goal", "current_step"}),
    or None. While there is one, guide_command rides along, like the draft questions.

    `follow` is savta.followup.question(): asked only when MicMic has just done
    something she can change (a reminder, a calendar event, what is playing, an answer
    about her screen) AND her words hold a modifier word (instead, move, pause,
    shorter, cancel...). One choice rides along, whose options code enumerated from
    what was done, and what was done goes into the state. Without it nothing is added.

    `song_desc`: her words may describe a song rather than name it
    (actions/music.describes_a_song), so describes_song rides along. Otherwise it is
    not asked and the request is exactly what it was.

    `at_micmic`: her words may be aimed at MicMic itself (people.may_be_at_micmic), so
    upset_with_micmic rides along: frustration with the machine is not distress.
    Otherwise not asked, and the request is exactly what it was.

    `screen_song`: her words may be pointing at the screen instead of naming a song
    (actions/music.mentions_screen_song), so screen_song rides along. Otherwise not
    asked, and the request is exactly what it was.

    `open_mic`: the microphone was open without her pressing the key (router: the
    activation was not "push"), so addressed_to rides along: were these words said to
    MicMic, to someone else, or by a television? Never asked on a push-to-talk turn,
    whose request is exactly what it was.

    `screen_ask`: her words may ask for a detail of something open on her screen
    (actions/screen.may_ask_about_screen), so screen_question rides along. Otherwise
    not asked, and the request is exactly what it was.

    `when` is savta.timewords.question(): asked only when her words hold a time word
    (a unit, a clock reading, "at 7", or a word for a timer, an alarm, a reminder or her
    calendar). Which kind of thing she wants set, and which of her words are the amount
    of time, the clock time, the day and what it is about; code computes the numbers.
    The old when_minutes below is still asked, so every other request is byte for byte
    what it was, but its closed list of minutes is no longer read for a number."""
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
        # "Send the message to Dana in French": the language she wants it written in,
        # which is not the language she is speaking.
        "write_in": {"type": "choice",
            "instructions": "If she asks for the message to be written or translated into a particular language, which one? The language she happens to be speaking in does not count.",
            "criteria": WRITE_IN},
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
        # --- speculative: only read when she asks about the weather ---
        # "מה מזג האוויר מחר בתל אביב" was answered with today's.
        "weather_day": {"type": "choice",
            "instructions": "If she is asking about the weather, which day is she asking "
                            "about? Today's date and weekday are in right_now.",
            "criteria": {"today": "Today or right now, or she did not say a day.",
                         "tomorrow": "Tomorrow, or the weekday that is tomorrow.",
                         "day_after": "The day after tomorrow, or the weekday that is "
                                      "two days from today.",
                         "later": "A day further away than the day after tomorrow: the "
                                  "weekend when that is further, next week, a date."}},
        # "What was the score of Maccabi Tel Aviv" was answered from the Wikipedia
        # article on Tel Aviv: "I do not know the score". A fresh report comes from a
        # live source (router._live_answer); this says which kind. The escape is first,
        # so a question nobody has recorded an answer for reads as not live.
        "live_kind": {"type": "choice",
            "instructions": "Is she asking for a fresh report of something that changes "
                            "from day to day, which only today's news or a live price can "
                            "answer, rather than a lasting fact? If so, which kind?",
            "criteria": {
                "not_live": "No. A lasting fact (who someone is, history, geography, how "
                            "something works, what a word means), the weather, the time, "
                            "something to watch or listen to, a message, small talk, or "
                            "anything else.",
                "sport_result": "How a team or a player did in a recent or current game: "
                                "who won, the score, the result.",
                "news": "The latest news, what is happening now, in general or about a "
                        "person, a place, a company or a subject.",
                "price": "What something is worth right now in the markets: bitcoin or "
                         "another cryptocurrency, or the exchange rate of a currency such "
                         "as the dollar or the euro."}},
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
        # Being shown HOW to do something on the screen, which is not the same as
        # asking MicMic to do it or asking what the screen says. walk_me_through starts
        # guide mode; how_to_here only puts a "Guide me" chip on the answer.
        # "none" first: a replay that never saw this question defaults to the first.
        "guidance": {"type": "choice",
            "instructions": "Is she asking to be shown HOW to do something in a program or on a website she has open on this computer, rather than asking the computer to do it, or asking about something else?",
            "criteria": {
                "none": "No. A request for the computer to do something itself (play, send, call, open, close, find, remind, book), a general question, a how-to about life away from the computer (how do I bake bread, how do I get to the station), small talk, or what the screen says (what is on my screen, summarize this, translate this, read this).",
                "walk_me_through": "She asks to be walked or guided through it step by step while she does it herself: guide me through adding the Maps API, walk me through this, guide me, show me step by step how to set this up, help me set up billing on this site step by step. תדריכי אותי, תעשי איתי את זה שלב אחרי שלב. دلّيني خطوة خطوة. проведи меня по шагам.",
                "how_to_here": "She asks how to do something in the program or on the website in front of her, without asking to be guided: how do I add an API key here, where do I turn on two factor on this page, how do I change my password in this app. איך מוסיפים פה משתמש. كيف بضيف مستخدم هون. как тут добавить пользователя."}},
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
    if draft:
        qs.update(DRAFT_QUESTIONS)
    if standing:
        qs.update(standing.get("questions") or {})
    if guide:
        qs.update(GUIDE_QUESTIONS)
    if named:
        qs.update(named)
    if follow:
        qs.update(follow["questions"])
    if song_desc:
        qs.update(SONG_DESC_QUESTIONS)
    if screen_song:
        qs.update(SCREEN_SONG_QUESTIONS)
    if at_micmic:
        qs.update(AT_MICMIC_QUESTIONS)
    if screen_ask:
        qs.update(SCREEN_ASK_QUESTIONS)
    if open_mic:
        qs.update(OPEN_MIC_QUESTIONS)
    if when:
        qs.update(when["questions"])
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
    }
    if draft:
        state["message_being_prepared"] = {
            "to": draft.get("to") or "not said yet",
            "says": draft.get("text") or "not said yet",
            "app": {"whatsapp": "WhatsApp", "telegram": "Telegram",
                    "signal": "Signal"}.get(draft.get("channel"), "text message")}
    if standing and standing.get("rules"):
        state["her_standing_rules"] = list(standing["rules"])
    if guide:
        state["guide_in_progress"] = {"goal": guide.get("goal") or "",
                                      "current_step": guide.get("current_step") or ""}
    if follow:
        state["last_thing_micmic_did"] = follow["state"]
    state["utterance"] = utterance
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
        "weather_day": choice(a, "weather_day")[0],
        "live_kind": choice(a, "live_kind")[0],
        "live_kind_confidence": choice(a, "live_kind")[1],
        "setting_emergency_contact": noul(a, "setting_emergency_contact"),
        "inside_an_app": noul(a, "inside_an_app"),
        "speaker_gender": choice(a, "speaker_gender")[0],
        "speaker_gender_confidence": choice(a, "speaker_gender")[1],
        "rejects_last": noul(a, "rejects_last"),
        "wants_undo": noul(a, "wants_undo"),
        "describes_instead": noul(a, "describes_instead"),
        "refers_to_screen": noul(a, "refers_to_screen"),
        "screen_task": choice(a, "screen_task")[0],
        "guidance": choice(a, "guidance")[0] if "guidance" in a else "none",
        "guidance_confidence": choice(a, "guidance")[1] if "guidance" in a else 0.0,
        "distress": score(a, "distress"),
        "emergency": noul(a, "emergency"),
        "write_in": choice(a, "write_in")[0],
        "amends_message": noul(a, "amends_message") if draft else 0.0,
        "message_app": choice(a, "message_app")[0] if draft else "unchanged",
        "named_app": choice(a, "named_app")[0] if named and "named_app" in a else "none",
        "named_app_confidence": (choice(a, "named_app")[1]
                                 if named and "named_app" in a else 0.0),
        "named_app_only_look": (noul(a, "named_app_only_look")
                                if named and "named_app_only_look" in a else 0.0),
        "standing": {k: v for k, v in a.items() if k.startswith("pref_")},
        "describes_song": (noul(a, "describes_song")
                           if song_desc and "describes_song" in a else 0.0),
        "screen_song": (noul(a, "screen_song")
                       if screen_song and "screen_song" in a else 0.0),
        "at_micmic": (noul(a, "upset_with_micmic")
                      if at_micmic and "upset_with_micmic" in a else 0.0),
        "screen_question": (noul(a, "screen_question")
                            if screen_ask and "screen_question" in a else 0.0),
        "guide_command": choice(a, "guide_command")[0] if guide and "guide_command" in a else "none",
        "guide_command_confidence": (choice(a, "guide_command")[1]
                                     if guide and "guide_command" in a else 0.0),
        **_addressed(a, open_mic),
    }


def _addressed(a: dict, open_mic: bool) -> dict:
    """The open-mic signals, typed: who the words were for, and the two numbers the
    router gates on. On a push turn (not asked) they read as plainly for MicMic."""
    if not open_mic or "addressed_to" not in a:
        return {"addressed_to": "assistant", "addressed_to_confidence": 1.0,
                "addressed_to_assistant": 1.0, "is_broadcast": 0.0}
    sel, conf, probs = choice(a, "addressed_to")
    probs = probs or {}
    return {"addressed_to": sel, "addressed_to_confidence": conf,
            "addressed_to_assistant": float(probs.get("assistant",
                                                      conf if sel == "assistant" else 0.0)),
            "is_broadcast": float(probs.get("broadcast",
                                            conf if sel == "broadcast" else 0.0))}


# ---------------------------------------------------------------- span picking

_CMD_NOISE = re.compile(r"[\"“”.,!?;:()\[\]]+")
# An apostrophe or ’ inside a word stays ("I'm", "Dana's", Hebrew geresh in "צ'יפס"):
# stripping it sent "I m running late". Only a quote mark at a word's edge is noise.
_EDGE_QUOTE = re.compile(r"(?<!\w)['‘’]+|['‘’]+(?!\w)")

def span_candidates(utterance: str, max_words: int = 9) -> list[str]:
    """Every contiguous word span, longest first. Jev picks one of these, which is how
    we extract free text from a model that cannot emit free text."""
    words = [w for w in _EDGE_QUOTE.sub(" ", _CMD_NOISE.sub(" ", utterance)).split() if w]
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
                specific: bool = True, trailer: bool = False):
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
        + ("She asked for the trailer. Choose an official trailer, preferably from the "
           "studio's or distributor's own channel or a well-known trailer channel. Not a "
           "parody, a fan-made or concept trailer, a reaction, a scene, or the whole film. "
           if trailer else
           "Strongly prefer a complete full-length work over a trailer, a short clip, a reaction "
           "video, a compilation of scenes, or a review. " if full_length else
           "A short clip is fine. ")
        + ("Never a parody, a spoof, a cartoon or meme version, or a reaction video, unless "
           "that is what she asked for. " if specific else "")
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


# ---------------------------------------------------------------- a stammered start
# MicMic Bench v1 msg-019: "אממ תשלחי לדנה ש... שאני בפקק" was read as half a sentence
# ("I only caught part of that", 3/3: is_complete 0.22). A filler at the start, a word
# cut off and said again ("ש... שאני", "la- late", "תש תשלחי") and a first word said
# twice ("call call dana") are tidied here, in code, before understand() sees the
# sentence. A sentence with none of these comes back as it was, byte for byte.
_FILLERS = {"um", "umm", "ummm", "uh", "uhh", "uhm", "er", "erm", "hmm", "hm", "mm", "mmm",
            "אממ", "אמממ", "אממממ", "אמ", "אה", "אהה", "אההה", "הממ", "המ", "ממ",
            "اممم", "امم", "ام", "اه", "إمم", "эм", "эмм", "ээ", "эээ", "мм", "ммм", "э"}
_CUT = re.compile(r"^(.*?\w)(?:\.{2,}|…|-)$")
_EDGE = re.compile(r"^[\s,.…!?;:\-]+|[\s,.…!?;:\-]+$")
_SEMITIC = re.compile(r"^[֐-׿؀-ۿ]+$")
# Two-letter Hebrew and Arabic words a bare fragment must never be mistaken for.
_SHORT_WORDS = set("""של את על אם גם זה לא כן מה מי אז יש רק עם אל כל הם הן זו בו לו לה בה די אף
פה שם כי או אך זאת
في من عن مع لا ما يا هو هي او أو لو كل""".split())


# Words said twice on purpose, never a stammer.
_PAIRS = {"bye", "yes", "yeah", "okay", "cough", "knock", "very", "really", "so", "now",
          "come", "go", "please", "wait", "ביי", "כן", "לא", "נו", "די", "רגע", "בואי", "מהר",
          "يلا", "لا", "اه", "да", "нет", "ну", "пока"}


def steady(utterance: str) -> str:
    """The sentence with a stammered start and cut-off words tidied (see above)."""
    text = utterance or ""
    toks = re.sub(r"(\.{2,}|…)(?=\w)", r"\1 ", text).split()
    out: list[str] = []
    changed = False
    i = 0
    # Fillers at the start: "um", "אממ", "uh," and the like.
    while i < len(toks) and _EDGE.sub("", toks[i]).lower() in _FILLERS:
        i += 1
        changed = True
    first = True
    while i < len(toks):
        tok, nxt = toks[i], (toks[i + 1] if i + 1 < len(toks) else "")
        core, nxt_core = _EDGE.sub("", tok).lower(), _EDGE.sub("", nxt).lower()
        cut = _CUT.match(tok)
        if nxt_core and cut:
            frag = cut.group(1).lower()
            # "ש... שאני", "la- late", "tea... teacher", or a repeated letter "שש... שלחי".
            if nxt_core.startswith(frag) or (len(set(frag)) == 1 and frag[0] == nxt_core[0]):
                i += 1
                changed = True
                continue
        if first and nxt_core:
            # A cut-off first word with no mark: "תש תשלחי" (Hebrew or Arabic, 2-3
            # letters, not a word of its own), or the first word said twice.
            bare = (_SEMITIC.match(core or "-") and 2 <= len(core) <= 3
                    and core not in _SHORT_WORDS and len(nxt_core) >= len(core) + 2
                    and nxt_core.startswith(core))
            # Said twice and then carried on: "call call dana". A pair on its own
            # ("cough cough", "bye bye", "no no") is what she said, and stays.
            twice = (core == nxt_core and len(core) >= 3 and tok == core
                     and i + 2 < len(toks) and core not in _PAIRS)
            if bare or twice:
                i += 1
                changed = True
                continue
        out.append(tok)
        first = False
        i += 1
    if not changed:
        return utterance
    # Nothing but fillers ("hmm", "um") is what she said, not an empty turn: as the
    # answer to "send it?" an empty utterance reached understand() with no words at
    # all (test_micmic 3c, after the merge of fix/media-live-speed).
    return " ".join(out).strip() or utterance
