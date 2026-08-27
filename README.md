# Ninjito Translate

Real-time Russian → English translation for Dota 2. Reads the in-game chat
off the screen, listens to voice chat, and suggests better words while you
type — all in an overlay on top of the game.

Windows only.

## Download

Grab the installer from the
[latest release](https://github.com/Ninjito/Dota2_Translator/releases/latest).
Run it, launch Dota, and you're going.

It installs per-user (no admin prompt) and everything it needs is bundled,
including the OCR engine. Python is not required to run it.

> The first time you turn on voice translation the app downloads a ~500 MB
> speech model. That is a one-time wait, and it needs internet.

## What it does

**Chat translation.** Press `F7` and the Russian chat currently on screen is
captured, translated, and shown in the overlay. It can also watch the chat
and translate automatically as lines appear.

**Voice translation.** Listens to whatever Dota is playing through your
speakers, picks out Russian speech, and shows the English underneath marked
with a speaker icon. Toggle with `Ctrl+Shift+V`.

**Typing suggestions.** While you type in Dota's chat box, a popup offers
spelling fixes, word completions, and a grammar-corrected version of the
whole line. Word fixes and completions work offline; the sentence fix needs
internet.

## Controls

| Key | Action |
|---|---|
| `F7` | Translate the chat right now |
| `Ctrl+Shift+V` | Toggle voice translation |
| `Ctrl+Shift+M` | Open settings |
| `Ctrl+Shift+H` | Show the translation log |
| `Ctrl+Shift+L` | Lock the overlay in place |
| `Ctrl+Shift+T` / `Ctrl+Shift+Y` | Send to team / all chat |
| `Ctrl+Shift+Q` | Stop sending |

While the suggestion popup is open: `Up`/`Down` to choose, `Left`/`Right` to
insert, `Esc` to dismiss. With no popup showing, those keys behave exactly as
Dota expects.

On the overlay itself: left-drag to move, `Shift`+mouse wheel to change
transparency, `Esc` to quit.

All hotkeys are rebindable in Settings.

## First run

Click **Resize** and draw a box around Dota's chat area. The region is saved
relative to Dota's window, so it survives moving or resizing the game — but
if translations come out garbled, re-drawing the box is the first thing to
try.

## Running from source

Requires **Python 3.10+** and **Tesseract OCR** with the Russian language
data.

```bash
git clone https://github.com/Ninjito/Dota2_Translator.git
cd Dota2_Translator
pip install -r requirements.txt
```

Install Tesseract to `C:\Program Files\Tesseract-OCR` (the default), then
drop [`rus.traineddata`](https://github.com/tesseract-ocr/tessdata/raw/main/rus.traineddata)
into its `tessdata` folder.

```bash
python calibrate.py   # once, to pick the chat region
python main.py
```

## Building the installer

```bash
python build.py
```

Produces `dist/NinjitoTranslate/`. CI turns that into the released installer
with Inno Setup — see [.github/workflows/release.yml](.github/workflows/release.yml).

Releases are cut by pushing a tag:

```bash
git tag -a v0.1.1 -m "..." && git push origin v0.1.1
```

## Notes

- **Windows only.** It hooks Windows APIs for screen capture, WASAPI loopback
  audio, and global hotkeys.
- **Antivirus false positives** are common with PyInstaller builds. The
  installer is built in public CI from this source — the workflow run for
  each release shows exactly how the binary was produced.
- **If suggestions don't appear**, Settings → Suggest explains why. "Access
  denied" means Dota is running as administrator and this app isn't; run it
  as administrator too.
- Translation uses public web endpoints and the chat text leaves your machine
  to be translated. Voice transcription runs locally.

## Privacy

Captured chat and voice transcripts are written to `logs/history.jsonl` next
to the app, on your machine only. Nothing is uploaded anywhere by this
project. That file is git-ignored and should never be committed — it contains
other players' messages.
