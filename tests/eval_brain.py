"""Does the brain pick the right skill, and how fast? (Skills are NOT run; this only checks choices.)"""
import sys, time, json; sys.path.insert(0, '.')
from jarvis_core.brain import Brain, BrainConfig, sentences

CASES = [
 ("Open Spotify", "open_app", {"name": "spotify"}),
 ("Can you pull up WhatsApp for me", "open_app", {"name": "whatsapp"}),
 ("Close Safari", "quit_app", {"name": "safari"}),
 ("Turn the volume down a bit", "volume", {"action": "down"}),
 ("Set the volume to 30", "volume", {"level": 30}),
 ("Mute", "volume", {"action": "mute"}),
 ("Skip this song", "media", {"action": "next"}),
 ("What song is this?", "media", {"action": "now_playing"}),
 ("Play some Arijit Singh", "play_music", {}),
 ("Put on lo-fi beats", "play_music", {}),
 ("Search YouTube for how to make cold coffee", "web_search", {"where": "youtube"}),
 ("Google the population of Japan", "web_search|look_up|None", {}),
 ("Open YouTube", "open_website", {}),
 ("Go to github.com", "open_website", {}),
 ("Set a timer for 10 minutes for the pasta", "set_timer", {}),
 ("How long is left on my timer?", "timers", {"action": "list"}),
 ("What's 18 percent of 2450?", "calculate", {}),
 ("What is 345 times 12?", "calculate", {}),
 ("How much battery do I have?", "system_status", {"what": "battery"}),
 ("How much space is left on my Mac?", "system_status", {"what": "storage"}),
 ("Am I connected to the internet?", "system_status", {"what": "internet"}),
 ("Switch to dark mode", "dark_mode", {"mode": "on"}),
 ("Lock the screen", "lock_screen", {}),
 ("Restart the computer", "power", {"action": "restart"}),
 ("Put the Mac to sleep", "power", {"action": "sleep"}),
 ("What's on my clipboard?", "read_clipboard", {}),
 ("Find my resume", "find_files", {}),
 ("Take a note: buy milk and eggs tomorrow", "create_note", {}),
 ("Remind me to call mum at 6 pm", "add_reminder", {}),
 ("Remind me in 20 minutes to check the oven", "add_reminder", {}),
 ("What are my reminders?", "list_reminders", {}),
 ("What's on my calendar today?", "calendar", {"day": "today"}),
 ("Do I have anything tomorrow?", "calendar", {"day": "tomorrow"}),
 ("Remember that my sister's birthday is on March 3rd", "remember", {}),
 ("What do you remember about me?", "recall|None", {}),
 ("Put lunch with Rohan in my calendar tomorrow at 1", "add_event", {}),
 ("Schedule a call with the bank on Friday at 11", "add_event", {}),
 ("Am I free on Friday?", "calendar", {}),
 ("Good morning", "briefing", {}),
 ("Open my downloads folder", "open_folder", {}),
 ("Take a screenshot", "screenshot", {}),
 ("Turn off the Wi-Fi", "wifi", {"state": "off"}),
 ("Run my focus mode shortcut", "run_shortcut", {}),
 ("Who won the cricket match yesterday?", "look_up|news", {}),
 ("What's the price of gold today?", "look_up", {}),
 ("Quit Xcode", "quit_app", {"name": "xcode"}),
 ("Is my Mac connected to the internet?", "system_status", {"what": "internet"}),
 ("How many days until Christmas?", "date_math", {}),
 ("Write me a two line poem about the sea", None, {}),
 ("What's the capital of Australia?", None, {}),
 ("Translate good night into Hindi", None, {}),
 ("Tell me a joke", None, {}),
 ("Who wrote Pride and Prejudice?", None, {}),
 ("How are you today, Jarvis?", None, {}),
 ("Explain black holes in simple words", None, {}),
 ("What's the weather like?", "weather", {}),
 ("Any new messages on WhatsApp?", "whatsapp_unread", {}),
 ("Check for updates in the Maple Tuition group", "read_messages", {}),
 ("What did Mummy say?", "read_messages", {}),
 ("Tell Mummy I'll be home by 8", "send_message", {}),
 ("Message the Maple Tuition group saying I'll be late", "send_message", {}),
 ("Remember Rohan's number is 98200 12345", "remember_person", {}),
 ("Read new messages out loud when they arrive", "message_alerts", {"state": "on"}),
 ("What's on my screen?", "look_at_screen", {}),
 ("Read the text on my screen", "read_screen_text", {}),
 ("What's in the news today?", "news", {}),
]

def run(model):
    b = Brain(BrainConfig(model=model))
    b.ensure_running()
    list(b.chat("hello"))  # load the model
    ok = 0; firsts = []; totals = []; fails = []
    for text, tool, args in CASES:
        b.history = []
        t = time.time(); first = None; out = ""
        for s in sentences(b.chat(text)):
            first = first or time.time() - t; out += s + " "
        total = time.time() - t
        calls = b.tool_calls
        got = calls[0]["name"] if calls else None
        good = got == tool or bool(tool and "|" in tool and (got or "None") in tool.split("|"))
        if good and calls and args:
            a = {k: str(v).lower() for k, v in calls[0]["arguments"].items()}
            good = all(str(v).lower() in a.get(k, "") for k, v in args.items())
        ok += good
        firsts.append(first or total); totals.append(total)
        if not good:
            fails.append(f"    {text!r}: wanted {tool} {args}, got {calls or out[:80]!r}")
    print(f"{model}: {ok}/{len(CASES)} right; first words/decision in {sorted(firsts)[len(firsts)//2]:.2f}s median, "
          f"{max(firsts):.2f}s worst; full reply {sorted(totals)[len(totals)//2]:.2f}s median")
    print("\n".join(fails))
    return b

if __name__ == "__main__":
    for m in sys.argv[1:] or ["qwen3.5:9b", "qwen3.5:4b"]:
        run(m)
