# Contributing

Thanks for wanting to help Jarvis for Mac.

## How this repository works

- **It's maintained by its owner.** Only the maintainer pushes to it; that keeps every change reviewed against [SAFETY.md](SAFETY.md).
- **You're welcome to fork it** and change your copy however you like, under the [MIT licence](LICENSE).
- **The best way to help** is a good issue: a clear bug report, or a well-explained idea. Use the [issue forms](../../issues/new/choose).
- **Pull requests** are read, but may not be merged. If you'd like to propose code, open an issue first to discuss it.

## Ground rules for any change

1. **Safety rules can't be weakened.** Nothing may send without the spoken yes, delete user data, log message text or dictation, or act on text from other people or the web.
2. **Stay local and free.** No cloud services, API keys, accounts, telemetry or paid dependencies.
3. **Pin and fingerprint** anything downloaded: new packages go in `requirements.in`, then regenerate `requirements.lock` with hashes; new models go in `setup/models.json` with their SHA-256.
4. **Test with stand-ins, never real data.** Use the invented contacts and WhatsApp database in `tests/`. Never commit real messages, numbers, names, recordings or logs.
5. **Write for humans:** plain words in messages Jarvis speaks and in the docs.

## Development setup

See **Build from source** in the [README](README.md#%EF%B8%8F-build-from-source).
