# Security

## What this app does to your machine

Ninjito Translate needs some capabilities that legitimately look alarming.
They are listed here so you can judge it for yourself rather than trust a
description.

| It does this | Why |
|---|---|
| Installs a global keyboard hook | To read what you type into Dota's chat box so it can suggest corrections. Dota does not expose its chat text, so the keystrokes are reconstructed. |
| Captures the screen | To OCR the chat area. Only the region you select is read. |
| Records system audio output | To transcribe Russian voice chat. It listens to what your speakers play, via WASAPI loopback. |
| Sends text over the network | Chat lines go to a translation service. See below. |

## What leaves your machine

**Sent out:**

- Chat text you translate, to the translation service used by
  `deep-translator`.
- The current sentence, to the LanguageTool API, only while the
  whole-sentence grammar fix is enabled (Settings -> Suggest).

**Stays local:**

- Voice transcription. Speech recognition runs on your machine via
  faster-whisper; audio is never uploaded.
- OCR. Tesseract runs locally.
- Word fixes and completions. These use a local dictionary.

**Stored on disk**, next to the app, never uploaded by this project:

- `logs/history.jsonl` — every translated chat line and voice transcript,
  with timestamps. This includes other players' messages. Delete it if you
  do not want that record kept.

## The installer is not code signed

Windows SmartScreen will warn you, and antivirus products sometimes flag
PyInstaller executables. That warning is expected and this project cannot
remove it without a paid signing certificate.

If you would rather not trust a binary from a stranger, build it yourself —
`python build.py` — or read the workflow that produced it in
`.github/workflows/release.yml`. Every release is built in public CI from
the tagged source, and the run log for each release is visible under the
Actions tab.

## Reporting a vulnerability

Please report privately rather than in a public issue: use **Security ->
Report a vulnerability** on this repository, which opens a private advisory
only the maintainer can see.

Things worth reporting: anything that lets the keyboard hook, screen capture
or audio capture be abused, or that sends data somewhere not listed above.

For ordinary bugs, open a normal issue.

## Supported versions

The latest release only. This is a small project; fixes go into the next
version rather than being backported.
