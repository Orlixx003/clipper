# Clipper

Turn long videos (podcasts, streams, interviews) into short, ready-to-post viral clips for X, TikTok, YouTube Shorts and Reels.

## Features

- **Any source:** paste a link (YouTube, X, Twitch and more) or use a local video file
- **Accurate transcript** with word-level timestamps (Whisper, runs locally for free)
- **AI moment finder:** Claude picks the most viral moments and, for each clip, gives you
  - a **virality score** from 1 to 10
  - an on-screen **hook**
  - a **ready-to-post X caption**
- **Face tracking:** the vertical crop follows the speaker instead of cutting the center
- **Animated captions** with word-by-word highlighting, in 3 styles: `classic`, `bold`, `minimal`
- **Hook title** on screen for the first 3 seconds
- **No duplicates:** clips never overlap and are sorted best-first
- **Batch mode:** process a whole list of videos in one run
- **Web interface:** use it in your browser, no command line needed

---

## Installation (one time)

### 1. Python and ffmpeg
**Windows** (PowerShell):
```
winget install Python.Python.3.12
winget install ffmpeg
```
Then **close and reopen** the terminal.

**Mac:**
```
brew install python ffmpeg
```

### 2. Dependencies
In the Clipper folder:
```
python -m venv venv
venv\Scripts\activate          # Windows
source venv/bin/activate       # Mac
pip install -r requirements.txt
```

### 3. Claude API key (optional)
Copy `.env.example` to `.env` and paste your key from https://console.anthropic.com.

No key? It still works. Clipper gives you a prompt, you paste it into any AI chat (Claude, ChatGPT) and paste the answer back.

---

## Option 1: Web interface (easiest)
```
python app.py
```
Your browser opens automatically. Paste a link or drop a video file, pick your settings and click **Generate clips**.
You'll get all clips with scores, hooks, X captions and a one-click **Download all (.zip)** button.

## Option 2: Command line
```
python clipper.py "https://www.youtube.com/watch?v=XXXX"
python clipper.py podcast.mp4 -n 12 --style bold
python clipper.py links.txt                       # several videos in a row
```

| Option | What it does |
|---|---|
| `-n 10` | number of clips (default 8) |
| `--format square` | `vertical` (9:16), `square` (1:1, great for X), `original` (16:9) |
| `--style bold` | caption style: `classic`, `bold`, `minimal` |
| `--min-len 15 --max-len 45` | clip length range in seconds |
| `--min-score 7` | skip clips the AI scored below 7 |
| `--hook-lang English` | language for hooks and captions |
| `--language en` | spoken language of the video (auto-detected if empty) |
| `--whisper-model medium` | more accurate transcript (`tiny`, `base`, `small`, `medium`, `large-v3`) |
| `--upper` | ALL CAPS captions |
| `--font "Montserrat"` | caption font (must be installed on your system) |
| `--hook-seconds 0` | no on-screen hook |
| `--no-track` | disable face tracking (center crop) |
| `--no-captions` | no burned-in captions |
| `--manual` | no API key: paste the prompt into an AI chat yourself |
| `--moments file.json` | use your own timestamps instead of AI |
| `--crf 18` | video quality (lower = better, bigger file) |

---

## Output
```
output/<video name>/
  clips/            ← finished .mp4 clips (01 = best)
  clips.md          ← score, hook, timestamps, X caption and transcript for every clip
  clips.json        ← the same data for other tools
  transcript.json   ← saved transcript (never re-done)
```
Running the same video again skips download and transcription, so you can quickly re-render clips with different settings.

### Your own timestamps
```json
[
  {"start": 125.0, "end": 168.5, "hook": "Nobody talks about this", "post": "your caption"},
  {"start": 610.2, "end": 650.0, "hook": "The mistake everyone makes"}
]
```

---

## Tips
- The first run downloads the Whisper model (75 MB to 3 GB). `small` is a good balance.
- Without an NVIDIA GPU, a 1-hour video on `small` takes roughly 10–30 minutes to transcribe.
- Face tracking works best with one person in frame. With two people it follows the biggest (closest) face.
- Treat clips as strong first drafts. Polish your favorites in CapCut if needed.
- Only clip content you own or have permission to use.

---

## Settings tab (license + AI)
- **AI:** choose free local **Ollama** (install from ollama.com, then click *Download AI model*) or **Claude** with your own API key.
- Settings are saved to `%APPDATA%\Clipper\config.json` (Windows) or `~/.clipper/config.json`.

## Building the Windows app for sale
1. Put your Gumroad `product_id` into `product.py` (leave it empty for a personal copy without licensing).
2. Double-click `build.bat` → `dist\Clipper-Windows.zip`.
3. See `SELLER_GUIDE_UA.md` for the full selling checklist.
