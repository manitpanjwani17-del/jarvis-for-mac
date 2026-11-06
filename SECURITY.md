# Security policy

Jarvis has microphone access, can type, can read WhatsApp on your Mac and can send messages. Security reports are taken seriously.

## Reporting a problem

**Please don't open a public issue for security problems.** Instead:

1. Go to this repository's **Security** tab → **Report a vulnerability** (GitHub's private reporting).
2. Describe what you found, how to reproduce it, and what it lets an attacker do.
3. **Don't include real personal data** (messages, contacts, phone numbers); made-up examples are perfect.

You'll get a reply as soon as possible. Once a fix is released you'll be credited, unless you'd rather not be.

## What counts

Especially welcome:

- Any way to make Jarvis **send a message without the spoken yes**, or to someone other than the one read back.
- Any way for **text from other people or the web** (a message, a web page, the clipboard, the screen) to make Jarvis act.
- Any way the **`fn` helper** could record or leak keystrokes, or type somewhere it shouldn't (a password field, or pressing Return).
- **Message text, dictation or audio** being written to disk or the log, or sent off the Mac.
- Downloads that could be **swapped or tampered with** despite the checksums.
- Anything that lets Jarvis **delete** something.

See [SAFETY.md](SAFETY.md) for the rules these would break.

## Supported versions

Only the latest release gets fixes. Please update before reporting.
