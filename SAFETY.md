# Safety

Jarvis can hear you, type for you, read your messages, and send them. That's a lot of trust, so this page sets out, plainly, every rule that protects you, where it lives in the code, and how it's tested.

**The short version:**
- Jarvis **never deletes your things**.
- He **never sends without your spoken yes**.
- He **never writes your messages or dictation down**.
- He **only looks at your screen when you ask**.
- He **can't be ordered around by text from other people or the web**.
- He **runs entirely on your Mac**.

---

## 1. Before Jarvis runs: what gets installed, and why it's trusted

- **Pinned, fingerprinted downloads.**
  - Every Python package is pinned to an exact version with its SHA-256 checksum ([`requirements.lock`](requirements.lock)), and installed with `--require-hashes`. A package that doesn't match is refused.
  - Every model file has a fixed address (an exact version, never “latest”), a size and a SHA-256 ([`setup/models.json`](setup/models.json)). It's downloaded to a temporary file, checked, and only then moved into place. A mismatch is deleted.
- **Ollama is verified before it's installed.** If you let setup install it, the download must be signed by Ollama's own Apple developer identity (team `3MU9H2V9Y9`) and accepted by macOS (Gatekeeper). Otherwise it isn't installed.
- **uv** (the tool that installs Jarvis's Python) is pinned to one release and checked against its published SHA-256, both when the app is built and when setup downloads it.
- **Models can't run hidden code.**
  - The neural networks are ONNX, CTranslate2 and safetensors files: weights, not programs.
  - Kokoro's voice file is plain numbers, opened with code loading off.
  - openWakeWord's optional “custom verifier”, the one code-capable format, is never used.
- **Nothing is installed system-wide.** Everything goes into `~/Library/Application Support/Jarvis for Mac`. Ollama is the only exception, and it's a normal app in Applications that you agree to.
- **No API keys, tokens or accounts** exist anywhere in Jarvis.

## 2. While Jarvis runs

### Listening
1. **Audio is never saved.** Sound lives in memory only while it's being understood, and is never written to disk or sent anywhere.
2. **Two-step wake-up.** The wake-word model fires, then a second model must actually hear “Jarvis”. This stops sound-alikes such as “Hey Travis”.
3. **Your voice only, once trained.** After “learn my voice”, other people's voices are ignored: their “Hey Jarvis”, background talk, and talking over him. They're logged as *“other voice ignored”* with a similarity number, never audio or words. Very short answers like “yes” are too short to judge, so they're accepted, but only inside a conversation you started.
4. **He never hears himself.** Echo cancellation removes his voice from the microphone, and audio captured while he speaks is discarded.

### Acting
5. **The AI can only use listed skills.**
   - [`jarvis_core/skills.py`](jarvis_core/skills.py) defines everything Jarvis can do.
   - There is **no skill** that deletes files, empties the Trash, runs shell commands, types passwords, pays for anything, changes security settings, or adds to your Contacts. However the AI is asked, it can't do those things.
   - A test fails if a skill with a name like that is ever added.
6. **Three levels, enforced in code, not by the AI:**

   | Level | Skills |
   |---|---|
   | Does it and tells you | open apps, folders and websites; web search; music; volume; timers; reminders; calendar events; notes; finding files; clipboard; dark mode; Wi-Fi; screenshots; lock screen; battery; memory; sums and dates |
   | **Asks first; needs a clear spoken yes** | quitting an app; sleep, restart or shut down; running one of your Shortcuts; going offline |
   | **Reads back exactly, then needs your yes** | every WhatsApp message and iMessage |
   | Only when *your* words ask | looking at or reading the screen |
   | **Never** | delete, Trash, shell commands, passwords, payments, security settings, writing to Contacts |

7. **What counts as “yes”.**
   - Only a clear yes counts: “yes”, “yeah”, “send it”, “go ahead”.
   - Any “no” wins, so “yes, actually no” is a no.
   - Silence or mumbling is a no.
8. **Undo and stop.** “Stop” or “quiet” cuts him off mid-sentence. “Undo that” reverses what can be reversed: a timer, the volume, dark mode, Wi-Fi, music playback, or something he just remembered.
9. **Your words are never pasted into a script.** AppleScript receives them as separate arguments, so a phrase can't become a command. Tested.
10. **Settings from the AI are checked.** Anything a skill doesn't expect is dropped, and choices must come from an allowed list.
11. **The calculator only does arithmetic.** It parses the sum's structure and refuses anything else. Tests try to make it run code.
12. **Found files that are programs are never run.** Scripts, apps, installers and executables are only shown in Finder.
13. **He doesn't invent things.** He's told to state only what a skill returned or what you asked him to remember, and tests check it.

### Messages
14. **Every message needs your yes.**
    - Jarvis reads back the exact recipient and the exact words.
    - “Change it to …” rewrites the message and asks again, up to three times.
    - No setting skips this.
15. **Spoken numbers are read back digit by digit** before the yes. If two people match a name, he asks which one, and gives up if the answer isn't clear.
16. **He never invents the words.** If none of a message's words came from you, it's thrown away and he asks what to say.
17. **WhatsApp is checked before Enter.**
    - For one person, the helper opens WhatsApp's official `whatsapp://` link for that exact number.
    - It waits until the message box holds exactly the approved words, presses Enter, and confirms the box emptied.
    - For groups, it uses WhatsApp's search. It opens only an exact name match, checks the chat title, leaves anyone's draft alone, types, checks the words, and only then presses Enter.
    - Any failed check stops it with nothing sent.
18. **Sending limit:** 3 messages a minute, 20 an hour.
19. **The helpers report only “sent” or an error code.** They never print chat names or message text. A test checks this.

### Reading, the screen and the web
20. **Reading WhatsApp is read-only.**
    - Jarvis opens WhatsApp's own database on your Mac read-only, and a test checks that no writing command exists.
    - Nothing is marked as read, so nobody gets blue ticks.
    - Messages go only to the brain on your Mac and are never logged.
21. **Messages can't control him.**
    - A message addressed to “Jarvis” is replaced, before the brain sees it, with a note saying someone sent instructions that were ignored.
    - After reading messages, the clipboard, the screen or the web, any send or risky step the brain proposes is dropped unless *your own words* asked for it. It would still need your spoken yes.
    - Tests cover both.
22. **New messages are read aloud only if you ask,** can be limited to one chat, and “don't read messages out loud” turns it off.
23. **The screen, only when you ask.**
    - A screen skill runs only when your words are about the screen.
    - The picture is analysed on your Mac and deleted at once.
    - Text on screen is information, never instructions.
24. **Network guard.**
    - Jarvis may connect to your Mac itself, and to ordinary web addresses (ports 80 and 443) for the lookups you ask for. Every other kind of connection is refused.
    - Each outside address is written to the log.
    - Web lookups only read: no forms, sign-ins, posts, purchases or uploads. Nothing about you is sent except the words you asked about.
25. **No telemetry.** No analytics, crash reporting or “phone home”. Model libraries are told not to check for updates.

### Voice typing (`fn`)
26. **It types, it never sends.** It never presses Return, so dictating into a chat leaves the text for *you* to send.
27. **It never reads what's in a text box.** It only checks what kind of box has the cursor.
28. **It never types into a password field.**
29. **Your clipboard is put back** after pasting, and the pasted text is marked so clipboard managers skip it.
30. **It can't answer or obey what you dictate.** The editing model is told it's an editor. A check then compares its version with your words: an answer, an addition, or dropping what you said is refused, and your own words are typed instead.
31. **The keyboard helper only knows about `fn`.** It notices `fn` and whether *some* other key was pressed with it. It never records which keys.
32. **The last dictation stays in memory for five minutes** (“paste that again”), never on disk.

### Your data
33. **The log keeps who, not what.**
    - Message commands are logged as “a message to Alex”, never the words.
    - Dictation logs only the app, the word count and how it was delivered.
34. **Your voiceprint is numbers, not a recording.** 192 numbers that can't be turned back into your voice. “Forget my voice” deletes it.
35. **Memory is yours.**
    - Only what you told him to remember is kept.
    - “Forget …” removes an item.
    - Deleting `data/memory.json` wipes everything.
36. **One folder holds everything.** Delete `~/Library/Application Support/Jarvis for Mac` and it's all gone.

## 3. How this is tested

The test suites in [`tests/`](tests/) use invented contacts, an invented WhatsApp database and stand-ins for anything that would act. During tests, real Contacts, WhatsApp, Messages, the keyboard and the clipboard are **blocked**, so a test can't touch your Mac. They include:

| Suite | Checks |
|---|---|
| `test_safety.py` | No dangerous skill names, script-injection, the calculator, the network guard, untrusted text, logging rules, the read-only WhatsApp reader |
| `test_messaging.py` | Read-back, confirmation, “yes, actually no”, numbers digit by digit, rate limits, invented messages refused |
| `test_whatsapp.py` | Reading, groups, nicknames, messages that try to give orders |
| `test_dictation.py` | Never answers or invents, password fields, clipboard, no text in the log, the `fn` key logic |
| `test_pipeline.py`, `test_brain_pipeline.py`, `test_interrupt.py`, `test_voice.py`, `test_wake_verify.py` | Whole spoken conversations, the brain's choices, interrupting, voice recognition, wake word |

## 4. Known limits

- **Recognising only your voice is strong but not perfect.** The real protection for sending is the spoken yes.
- **WhatsApp and Music automation** depend on those apps' layouts, and a redesign can break them. Every check above makes a broken step *stop*, not send something wrong.
- **WhatsApp's terms** forbid automation and unofficial clients. Jarvis uses the official app, one message at a time, only after your yes, but it's still not an official WhatsApp feature. Use it at your own risk.
- **The app is signed on the builder's Mac (“ad hoc”), not notarised by Apple.** Build it yourself from this source if you prefer.

Found a way around any of this? Please report it privately; see [SECURITY.md](SECURITY.md).
