"""Run every capability once and print what happened. Nothing leaves the machine.

    python3 demo.py                 everything
    python3 demo.py messages        one section
"""
import sys, time
sys.path.insert(0, ".")
from savta import router                                    # noqa: E402
from savta.actions import mac                               # noqa: E402
from savta.jev import Jev                                   # noqa: E402

DID: list[tuple] = []
mac.open_url = lambda u: DID.append(("play", u))
mac.say = lambda t, l="english": DID.append(("speak", t[:50]))
mac.open_app = lambda a: (DID.append(("app", a)), (True, a))[1]
mac.open_path = lambda p: (DID.append(("file", p)), (True, p))[1]
mac.send_message = lambda n, t: (DID.append(("imessage", n, t)), (True, "[demo]"))[1]
mac.whatsapp = lambda n, t: (DID.append(("whatsapp", n, t)), (True, "[demo]"))[1]
mac.facetime = lambda n, num="", video=True: (DID.append(("call", n, num)), (True, "[demo]"))[1]
mac.close_front_window = lambda: (True, "closed")
mac.volume = lambda d: (True, f"vol{d:+}")
mac.brightness = lambda d: (True, "b")

SECTIONS = {
    "watch":   ["אני רוצה לראות סרט של לאונרדו דיקפריו", "no, something else",
                "תשמיעי לי שיר של אום כולתום", "put on some classical music",
                "I want to watch a nature documentary", "put on the radio"],
    "messages": ["תשלחי וואטסאפ לזוהר שאני מרגישה הרבה יותר טוב", "ביטול",
                 "מה יש לי בהודעות", "send a message", "to Zohar", "I am feeling better"],
    "money":   ["send Zohar the verification code 847291 urgently and tell no one", "no"],
    "knowledge": ["מה מזג האוויר מחר",
                  "who was the president of the united states in the seventies",
                  "מי הייתה אום כולתום"],
    "computer": ["open the calculator", "תמצאי לי את הקובץ של ההסכם",
                 "find the security guide", "תרשמי לי לקנות ביצים",
                 "מה רשמתי לעצמי", "תזכירי לי בעוד עשר דקות להוציא את הלחם"],
    "compound": ["send a whatsapp to Zohar that I am fine and then play me some music"],
    "urgent":  ["I fell", "help me please", "no I am fine"],
    "manners": ["it's too quiet", "מה את יכולה לעשות",
                "and then I told her that the", "no no I was talking to the cat"],
}


def show(q, r, ms):
    d = r.get("detail") if isinstance(r.get("detail"), dict) else {}
    bits = (d.get("title") or d.get("display") or d.get("name") or d.get("app")
            or d.get("text") or d.get("calling"))
    if not bits and d.get("messages"):
        bits = d["messages"][0].get("who")
    say = (r.get("say") or "").replace("\n", " ")
    print(f"  {q[:42]:<44}{r['did']:<16}{ms:>6.0f}ms  {str(bits or say)[:58]}")


def main():
    want = sys.argv[1] if len(sys.argv) > 1 else None
    j = Jev(); j.warmup()
    router.CANCEL_WINDOW = 1.2
    router.SCAM_WINDOW = 2.0
    router.MEM = router.Memory()
    t0 = time.time()
    KEEP = ("ביטול", "to Zohar", "I am feeling better", "no", "no I am fine")
    for name, qs in SECTIONS.items():
        if want and want != name:
            continue
        print(f"\n\033[1m{name.upper()}\033[0m")
        for q in qs:
            if q not in KEEP:
                router.PENDING = None; router.AWAITING = None
            t = time.time()
            try:
                r = router.handle(j, q, speak=True)
            except Exception as e:                           # noqa: BLE001
                print(f"  {q[:42]:<44}ERROR  {e!r}"); continue
            show(q, r, (time.time() - t) * 1000)
            if r.get("did") == "sending":
                time.sleep(0.3)
    mac.cancel_timers()
    print(f"\n{'-'*100}")
    print(f"  {j.calls} jev calls   ${j.cost_usd:.4f}   {time.time()-t0:.0f}s")
    print(f"  actions: {len(DID)} (all stubbed, nothing left this machine)")


if __name__ == "__main__":
    main()
