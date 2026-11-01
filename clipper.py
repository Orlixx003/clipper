#!/usr/bin/env python3
"""
Clipper v2 — turns long videos (podcasts, streams, interviews) into short viral clips.

Pipeline:
  1. download    — yt-dlp (YouTube / X / Twitch ...) or a local file
  2. transcribe  — faster-whisper (local, free), word-level timestamps
  3. find moments — Claude API (hook, virality score, ready X post), or manual / JSON
  4. render      — ffmpeg: cut, face-tracking crop to 9:16 / 1:1, animated captions + hook

Usage:
  python clipper.py "https://youtube.com/watch?v=..." -n 8
  python clipper.py links.txt                     # many videos, one link per line
  python clipper.py podcast.mp4 --style bold --format square
  python clipper.py podcast.mp4 --manual          # no API key: paste prompt into any AI chat
  python app.py                                   # web interface in the browser
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

FORMATS = {
    "vertical": (1080, 1920),   # TikTok, Shorts, Reels
    "square": (1080, 1080),     # X feed
    "original": None,           # keep 16:9
}

# ASS colours are &HAABBGGRR
STYLES = {
    #            spoken word       upcoming word      outline            words/line caps         pop        size
    "classic": dict(hi="&H0000FFFF", base="&H00FFFFFF", out="&H00000000", words=3, upper=False, pop=True, size=1.0),
    "bold":    dict(hi="&H0000FF7F", base="&H00FFFFFF", out="&H00000000", words=2, upper=True,  pop=True, size=1.2),
    "minimal": dict(hi="&H00FFFFFF", base="&H00FFFFFF", out="&H00202020", words=5, upper=False, pop=False, size=0.8),
}

DEFAULT_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")


# ───────────────────────── helpers ─────────────────────────

class ClipperError(Exception):
    pass


def log(msg):
    print(f"[clipper] {msg}", flush=True)


_TRANSLIT = dict(zip(
    "абвгґдеєёжзиіїйклмнопрстуфхцчшщъыьэюя",
    ["a", "b", "v", "g", "g", "d", "e", "ye", "yo", "zh", "z", "i", "i", "yi", "y", "k", "l", "m", "n", "o",
     "p", "r", "s", "t", "u", "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya"]))


def slugify(text, maxlen=40):
    """ASCII-only file-safe name (ffmpeg on Windows breaks on non-Latin file names)."""
    text = "".join(_TRANSLIT.get(c, c) for c in text.lower())
    text = text.encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    text = re.sub(r"[\s_-]+", "_", text)
    return text[:maxlen].strip("_") or "clip"


def fmt_time(sec):
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def probe_size(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout
    st = json.loads(out)["streams"][0]
    return st["width"], st["height"]


def check_ffmpeg():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise ClipperError("ffmpeg not found. Windows: `winget install ffmpeg`, Mac: `brew install ffmpeg`, "
                           "then restart the terminal.")


# ───────────────────────── 1. download ─────────────────────────

def get_video(source, workdir):
    if Path(source).exists():
        log(f"Using local file: {source}")
        return Path(source).resolve()
    if not re.match(r"https?://", source):
        raise ClipperError(f"'{source}' is not a file or a link.")

    existing = [f for f in workdir.glob("source.*") if f.suffix != ".part"]
    if existing:
        log("Video already downloaded, reusing it.")
        return existing[0]

    import yt_dlp
    log("Downloading video...")
    opts = {
        "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(workdir / "source.%(ext)s"),
        "quiet": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([source])
    files = [f for f in workdir.glob("source.*") if f.suffix != ".part"]
    if not files:
        raise ClipperError("Download failed.")
    return files[0]


# ───────────────────────── 2. transcribe ─────────────────────────

def load_audio(video, sr=16000):
    """Decode the audio track to 16 kHz mono float32 numpy array using ffmpeg."""
    import numpy as np
    log("Extracting audio...")
    out = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(video),
         "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-acodec", "pcm_s16le", "-"],
        capture_output=True, check=True).stdout
    return np.frombuffer(out, np.int16).astype(np.float32) / 32768.0


def _run_whisper(model, audio, language):
    segments, info = model.transcribe(audio, language=language,
                                      word_timestamps=True, vad_filter=True)
    log(f"Language: {info.language}, duration: {fmt_time(info.duration)}")
    data = {"language": info.language, "duration": info.duration, "segments": []}
    for seg in segments:
        words = [{"start": round(w.start, 2), "end": round(w.end, 2), "word": w.word}
                 for w in (seg.words or [])]
        data["segments"].append({"start": round(seg.start, 2), "end": round(seg.end, 2),
                                 "text": seg.text.strip(), "words": words})
        print(f"\r  transcribed up to {fmt_time(seg.end)} / {fmt_time(info.duration)}", end="", flush=True)
    print()
    return data


def transcribe(video, workdir, model_size, language, device="auto"):
    cache = workdir / "transcript.json"
    if cache.exists():
        log("Transcript already exists, reusing it.")
        return json.loads(cache.read_text(encoding="utf-8"))

    from faster_whisper import WhisperModel
    log(f"Transcribing with Whisper '{model_size}' (first run downloads the model)...")
    # decode audio with ffmpeg ourselves (avoids PyAV version conflicts inside faster-whisper)
    audio = load_audio(video)

    data = None
    if device in ("auto", "cuda"):
        try:
            model = WhisperModel(model_size, device=device, compute_type="auto")
            data = _run_whisper(model, audio, language)
        except Exception as ex:
            # typical on Windows: NVIDIA GPU present but CUDA libraries (cublas/cudnn) missing
            if device == "cuda":
                raise ClipperError(f"GPU transcription failed: {ex}")
            log(f"GPU not available ({str(ex)[:80]}) -> using CPU (slower, but works).")
            data = None
    if data is None:
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        data = _run_whisper(model, audio, language)

    cache.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


# ───────────────────────── 3. find moments ─────────────────────────

PROMPT = """You are an expert short-form video editor who makes viral clips for X, TikTok and YouTube Shorts.

Below is a transcript of a long video with timestamps in seconds: [start-end] text.

Find the {n} best moments to turn into standalone clips. Each clip must:
- be {min_len}-{max_len} seconds long
- start exactly where the interesting part starts (no slow intro, no "so, um") and end on a finished thought
- make sense without any context
- have strong viral potential: a bold or controversial opinion, a surprising fact, a funny moment,
  a strong emotion, a story with a payoff, or a concrete actionable tip
- not overlap with other clips

For each clip return:
- "hook": short on-screen title, max 8 words, makes people stop scrolling
- "score": virality score 1-10 (be honest and strict)
- "post": ready-to-publish X post to go with the clip: lowercase hook line, 1-3 short punchy lines,
  ends with a question to drive replies, no hashtags, no emojis, max 240 characters
- "reason": one sentence why it will perform

Write hook and post in {hook_lang}.

Return ONLY a JSON array, best clip first, no other text:
[{{"start": 123.4, "end": 170.2, "hook": "...", "score": 8, "post": "...", "reason": "..."}}]

TRANSCRIPT:
{transcript}"""


def transcript_text(data):
    return "\n".join(f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in data["segments"])


def build_prompt(data, opt):
    hook_lang = opt.hook_lang or "the same language as the transcript"
    return PROMPT.format(n=opt.n, min_len=opt.min_len, max_len=opt.max_len,
                         hook_lang=hook_lang, transcript=transcript_text(data))


def parse_moments(text):
    m = re.search(r"\[.*\]", text, flags=re.S)
    if not m:
        raise ClipperError("Could not find a JSON array with moments in the AI answer.")
    moments = json.loads(m.group(0))
    for mo in moments:
        mo["start"], mo["end"] = float(mo["start"]), float(mo["end"])
        mo.setdefault("hook", "")
        mo.setdefault("post", "")
        mo.setdefault("reason", "")
        try:
            mo["score"] = float(mo.get("score", 0))
        except (TypeError, ValueError):
            mo["score"] = 0
    return moments


def find_moments_claude(data, opt):
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    import anthropic
    log(f"Asking Claude ({opt.model}) for the best {opt.n} moments...")
    client = anthropic.Anthropic(api_key=key)
    resp = client.messages.create(model=opt.model, max_tokens=8000,
                                  messages=[{"role": "user", "content": build_prompt(data, opt)}])
    text = "".join(b.text for b in resp.content if b.type == "text")
    return parse_moments(text)


OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
DEFAULT_OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")


def _ollama(path, payload=None, timeout=900):
    import urllib.request
    req = urllib.request.Request(OLLAMA_URL + path,
                                 data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def ollama_models():
    """Names of installed Ollama models, or None if Ollama isn't running."""
    try:
        return [m["name"] for m in _ollama("/api/tags", timeout=3).get("models", [])]
    except Exception:
        return None


def ollama_pull(model):
    """Download a model into Ollama (same as `ollama pull <model>`). Can take a while (GBs)."""
    res = _ollama("/api/pull", {"model": model, "stream": False}, timeout=3 * 3600)
    if res.get("status") != "success":
        raise ClipperError(f"Ollama could not download '{model}': {res}")


def find_moments_ollama(data, opt):
    """Free, local AI. Small models handle short texts better, so the transcript is
    split into ~10-minute chunks, each chunk gives candidates, then the best are kept."""
    models = ollama_models()
    if models is None:
        return None
    model = opt.ollama_model
    if not any(m == model or m.split(":")[0] == model for m in models):
        raise ClipperError(f"Ollama model '{model}' is not installed. Run in the terminal:  ollama pull {model}")

    segs, chunks, cur = data["segments"], [], []
    for sg in segs:
        if cur and sg["end"] - cur[0]["start"] > 600:
            chunks.append(cur)
            cur = []
        cur.append(sg)
    if cur:
        chunks.append(cur)
    per_chunk = max(2, -(-opt.n // len(chunks)) + 1)

    log(f"Asking local AI ({model}) for moments: {len(chunks)} part(s) of the video...")
    candidates = []
    for i, ch in enumerate(chunks, 1):
        part = {"segments": ch}
        prompt = PROMPT.format(
            n=per_chunk, min_len=opt.min_len, max_len=opt.max_len,
            hook_lang=opt.hook_lang or "the same language as the transcript",
            transcript=transcript_text(part))
        prompt = prompt.replace(
            "Return ONLY a JSON array, best clip first, no other text:",
            'Return ONLY a JSON object with key "clips" holding the array, best clip first:')
        prompt = prompt.replace('[{"start": 123.4', '{"clips": [{"start": 123.4').replace(
            '"reason": "..."}]', '"reason": "..."}]}')
        try:
            resp = _ollama("/api/chat", {
                "model": model, "stream": False, "format": "json",
                "messages": [{"role": "user", "content": prompt}],
                "options": {"num_ctx": 8192, "temperature": 0.4}})
            found = parse_moments(resp["message"]["content"])
        except ClipperError:
            found = []
        except Exception as ex:
            raise ClipperError(f"Ollama error: {ex}")
        lo, hi = ch[0]["start"], ch[-1]["end"]
        for mo in found:
            # small models sometimes invent timestamps: keep them inside this chunk and the length limits
            if not (lo - 5 <= mo["start"] < hi):
                continue
            if mo["end"] <= mo["start"] or mo["end"] - mo["start"] > opt.max_len + 15:
                mo["end"] = min(mo["start"] + opt.max_len, hi)
            candidates.append(mo)
        log(f"  part {i}/{len(chunks)}: {len(found)} moment(s)")
    if not candidates:
        raise ClipperError("The local AI didn't return usable moments. Try again or use a bigger model.")
    return candidates


def find_moments_manual(data, opt, workdir):
    prompt_file = workdir / "prompt.txt"
    prompt_file.write_text(build_prompt(data, opt), encoding="utf-8")
    log("")
    log(f"Manual mode. Prompt saved to: {prompt_file.resolve()}")
    log("1) Open it, copy everything and paste into Claude / ChatGPT")
    log("2) Copy the JSON answer and paste it below.")
    log("   Then press Enter, Ctrl+Z, Enter (Windows) or Ctrl+D (Mac/Linux).")
    answer = sys.stdin.read()
    (workdir / "moments.json").write_text(answer, encoding="utf-8")
    return parse_moments(answer)


def all_words(data):
    return [w for s in data["segments"] for w in s["words"]]


def snap(moment, words, duration):
    """Align clip edges to word boundaries so no word is cut in half."""
    s, e = moment["start"], moment["end"]
    starts = [w for w in words if w["start"] >= s - 0.4]
    if starts:
        s = starts[0]["start"]
    ends = [w for w in words if w["end"] <= e + 0.4]
    if ends:
        e = ends[-1]["end"]
    return max(0.0, s - 0.1), min(duration, e + 0.3)


def drop_overlaps(moments):
    kept = []
    for mo in moments:
        if all(mo["end"] <= k["start"] or mo["start"] >= k["end"] for k in kept):
            kept.append(mo)
    return kept


# ───────────────────────── 4a. face tracking ─────────────────────────

_cascade = None


def _face_detector():
    global _cascade
    if _cascade is None:
        import cv2
        _cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    return _cascade


def track_faces(video, start, end, src_w, step=0.4):
    """Returns list of (t_relative, face_center_x or None) sampled every `step` seconds."""
    try:
        import cv2
    except ImportError:
        log("  opencv not installed -> center crop")
        return []
    cap = cv2.VideoCapture(str(video))
    det = _face_detector()
    samples, t = [], start
    scale = 640 / src_w if src_w > 640 else 1.0
    while t < end:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if not ok:
            break
        small = cv2.resize(frame, None, fx=scale, fy=scale) if scale != 1.0 else frame
        gray = cv2.equalizeHist(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
        faces = det.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6,
                                     minSize=(int(gray.shape[0] * 0.08),) * 2)
        if len(faces):
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])   # biggest face = closest speaker
            samples.append((t - start, (x + w / 2) / scale))
        else:
            samples.append((t - start, None))
        t += step
    cap.release()
    return samples


def crop_segments(samples, src_w, crop_w):
    """Turn noisy face samples into a few stable camera positions: [(t_from, x_left), ...]."""
    if not samples or all(x is None for _, x in samples):
        return [(0.0, (src_w - crop_w) // 2)]
    # fill gaps with the last known position (next known one for the beginning)
    xs = [x for _, x in samples]
    last, filled = next(x for x in xs if x is not None), []
    for x in xs:
        last = x if x is not None else last
        filled.append(last)
    # median smoothing (window 5) kills single false detections
    sm = []
    for i in range(len(filled)):
        win = sorted(filled[max(0, i - 2): i + 3])
        sm.append(win[len(win) // 2])
    # split into "shots": move the camera only if the face moved noticeably
    tol = crop_w * 0.18
    segs, cur_t, cur = [], 0.0, [sm[0]]
    for (t, _), x in zip(samples, sm):
        mean = sum(cur) / len(cur)
        if abs(x - mean) > tol and t - cur_t >= 1.0:
            segs.append((cur_t, mean))
            cur_t, cur = t, [x]
        else:
            cur.append(x)
    segs.append((cur_t, sum(cur) / len(cur)))
    out = []
    for t, cx in segs[:40]:
        left = min(max(cx - crop_w / 2, 0), src_w - crop_w)
        out.append((round(t, 2), int(left)))
    return out


def crop_x_expr(segs):
    """ffmpeg expression: camera jumps to the new position at each shot change."""
    expr = str(segs[-1][1])
    for i in range(len(segs) - 2, -1, -1):
        expr = f"if(lt(t,{segs[i + 1][0]}),{segs[i][1]},{expr})"
    return expr


# ───────────────────────── 4b. captions ─────────────────────────

def ass_time(t):
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def ass_escape(text):
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ").strip()


def caption_chunks(words, max_words, max_dur=1.4):
    chunks, cur = [], []
    for w in words:
        if cur and (len(cur) >= max_words or w["end"] - cur[0]["start"] > max_dur
                    or re.search(r"[.!?,]$", cur[-1]["word"].strip())):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def build_ass(words, clip_start, clip_end, hook, width, height, opt):
    st = STYLES[opt.style]
    upper = opt.upper or st["upper"]
    vertical = height > width
    fs = int(height * (0.055 if vertical else 0.065) * st["size"])
    hs = int(height * (0.045 if vertical else 0.06))
    margin_v = int(height * (0.22 if vertical else 0.10))
    hook_mv = int(height * 0.08)
    outline = max(3, fs // 11)

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{opt.font},{fs},{st['hi']},{st['base']},{st['out']},&H80000000,-1,0,0,0,100,100,0,0,1,{outline},3,2,60,60,{margin_v},1
Style: Hook,{opt.font},{hs},&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,{max(8, hs // 4)},0,8,80,80,{hook_mv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    if hook and opt.hook_seconds > 0:
        dur = min(opt.hook_seconds, clip_end - clip_start)
        pop = r"{\fad(0,250)\fscx90\fscy90\t(0,120,\fscx100\fscy100)}"
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(dur)},Hook,,0,0,0,,{pop}{ass_escape(hook)}")

    if not opt.no_captions:
        clip_words = [w for w in words if w["start"] >= clip_start - 0.05 and w["end"] <= clip_end + 0.05]
        chunks = caption_chunks(clip_words, st["words"])
        for i, ch in enumerate(chunks):
            s = ch[0]["start"] - clip_start
            e = (chunks[i + 1][0]["start"] - clip_start) if i + 1 < len(chunks) else ch[-1]["end"] - clip_start
            e = min(e, ch[-1]["end"] - clip_start + 0.5)
            parts = []
            for j, w in enumerate(ch):
                nxt = ch[j + 1]["start"] if j + 1 < len(ch) else w["end"]
                k = max(1, int(round((nxt - w["start"]) * 100)))
                txt = ass_escape(w["word"])
                parts.append(f"{{\\k{k}}}{txt.upper() if upper else txt}")
            pop = r"{\fscx80\fscy80\t(0,90,\fscx100\fscy100)}" if st["pop"] else ""
            lines.append(f"Dialogue: 0,{ass_time(s)},{ass_time(e)},Cap,,0,0,0,,{pop}{' '.join(parts)}")
    return header + "\n".join(lines) + "\n"


# ───────────────────────── 4c. render ─────────────────────────

def video_filter(video, s, e, src_w, src_h, opt):
    target = FORMATS[opt.format]
    if target is None:
        if src_w > 1920:
            return "scale=1920:-2", (1920, int(src_h * 1920 / src_w) // 2 * 2), None
        return "null", (src_w, src_h), None
    tw, th = target
    if src_w / src_h > tw / th:
        # source is wider than target -> crop horizontally and follow the face
        crop_w = int(src_h * tw / th) // 2 * 2
        if opt.no_track:
            segs = [(0.0, (src_w - crop_w) // 2)]
        else:
            segs = crop_segments(track_faces(video, s, e, src_w), src_w, crop_w)
        x = crop_x_expr(segs)
        return f"crop={crop_w}:{src_h}:x='{x}':y=0,scale={tw}:{th},setsar=1", (tw, th), segs
    # source is taller -> center crop vertically
    crop_h = int(src_w * th / tw) // 2 * 2
    return f"crop={src_w}:{crop_h}:0:(ih-{crop_h})/2,scale={tw}:{th},setsar=1", (tw, th), None


def render_clip(video, idx, moment, words, outdir, src_size, opt):
    s, e = moment["start"], moment["end"]
    hook = moment.get("hook", "")
    name = f"clip_{idx:02d}_{slugify(hook) if hook else 'clip'}"
    vf, (w, h), segs = video_filter(video, s, e, *src_size, opt)
    if segs and len(segs) > 1:
        log(f"  face tracking: {len(segs)} camera positions")

    ass_name = f"subs_{idx:02d}.ass"   # plain ASCII name: safest for the ffmpeg subtitles filter
    (outdir / ass_name).write_text(build_ass(words, s, e, hook, w, h, opt), encoding="utf-8")
    vf = f"{vf},subtitles={ass_name}"

    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-ss", f"{s:.2f}", "-i", str(Path(video).resolve()), "-t", f"{e - s:.2f}",
           "-vf", vf,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", str(opt.crf), "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
           f"{name}.mp4"]
    # run inside outdir so the subtitles path has no drive letters / backslashes (Windows-safe)
    res = subprocess.run(cmd, cwd=outdir, capture_output=True, text=True, errors="replace")
    if res.returncode != 0:
        raise ClipperError(f"ffmpeg failed on clip {idx}: {res.stderr.strip()[-600:] or res.returncode}")
    if not opt.keep_ass:
        (outdir / ass_name).unlink(missing_ok=True)
    return outdir / f"{name}.mp4"


# ───────────────────────── orchestration ─────────────────────────

def write_report(workdir, report):
    (workdir / "clips.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md = ["# Clips\n"]
    for r in report:
        md.append(f"## {r['file']}\n"
                  f"- **Score:** {r['score']:g}/10\n"
                  f"- **Time:** {fmt_time(r['start'])}–{fmt_time(r['end'])} ({r['length']}s)\n"
                  f"- **Hook:** {r['hook']}\n"
                  f"- **Why:** {r['reason']}\n\n"
                  f"**Post for X:**\n```\n{r['post']}\n```\n\n"
                  f"> {r['text']}\n")
    (workdir / "clips.md").write_text("\n".join(md), encoding="utf-8")


def process(source, opt, moments_text=None):
    """Process one video. Returns (clips_dir, report)."""
    check_ffmpeg()
    base = Path(source).stem if Path(source).exists() else slugify(re.sub(r"https?://(www\.)?", "", source)[-40:])
    workdir = (Path(opt.out) / slugify(base)).resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    video = Path(get_video(source, workdir)).resolve()
    data = transcribe(video, workdir, opt.whisper_model, opt.language, opt.device)
    if not data["segments"]:
        raise ClipperError("No speech found in the video.")

    if moments_text:
        moments = parse_moments(moments_text)
    elif opt.moments:
        moments = parse_moments(Path(opt.moments).read_text(encoding="utf-8"))
    elif opt.manual and opt.interactive:
        moments = find_moments_manual(data, opt, workdir)
    else:
        moments = None
        if not opt.manual:
            if opt.ai in ("auto", "claude"):
                moments = find_moments_claude(data, opt)
                if moments is None and opt.ai == "claude":
                    raise ClipperError("No ANTHROPIC_API_KEY in .env")
            if moments is None and opt.ai in ("auto", "ollama"):
                moments = find_moments_ollama(data, opt)
                if moments is None and opt.ai == "ollama":
                    raise ClipperError("Ollama is not running. Install it from ollama.com and start it.")
        if moments is None:
            if opt.interactive:
                log("No Claude API key and no Ollama found -> switching to manual mode.")
                moments = find_moments_manual(data, opt, workdir)
            else:
                (workdir / "prompt.txt").write_text(build_prompt(data, opt), encoding="utf-8")
                raise ClipperError("NEED_MOMENTS")

    words = all_words(data)
    for mo in moments:
        mo["start"], mo["end"] = snap(mo, words, data["duration"])
    moments = [m for m in moments if m["end"] - m["start"] >= 3]
    moments.sort(key=lambda m: -m["score"])
    moments = drop_overlaps(moments)[: opt.n]
    if opt.min_score:
        moments = [m for m in moments if m["score"] >= opt.min_score or m["score"] == 0]

    src_size = probe_size(video)
    clipsdir = workdir / "clips"
    clipsdir.mkdir(exist_ok=True)

    report = []
    for i, mo in enumerate(moments, 1):
        s, e = mo["start"], mo["end"]
        log(f"Clip {i}/{len(moments)}  [{mo['score']:g}/10]  {fmt_time(s)}-{fmt_time(e)}  {mo['hook']}")
        path = render_clip(video, i, mo, words, clipsdir, src_size, opt)
        text = " ".join(w["word"].strip() for w in words if s <= w["start"] < e)
        report.append({"file": path.name, "path": str(path.resolve()), "start": round(s, 2),
                       "end": round(e, 2), "length": round(e - s, 1), "score": mo["score"],
                       "hook": mo["hook"], "post": mo["post"], "reason": mo["reason"], "text": text})

    write_report(workdir, report)
    log(f"Done! {len(report)} clips in: {clipsdir.resolve()}")
    return clipsdir, report


def prompt_for(source, opt):
    """For the web UI: path of prompt.txt saved by process() when no API key is set."""
    base = Path(source).stem if Path(source).exists() else slugify(re.sub(r"https?://(www\.)?", "", source)[-40:])
    return Path(opt.out) / slugify(base) / "prompt.txt"


def build_parser():
    p = argparse.ArgumentParser(description="Turn long videos into short viral clips.")
    p.add_argument("source", nargs="?", default="",
                   help="link, local video file, or .txt with one link/file per line")
    p.add_argument("-n", type=int, default=8, help="how many clips (default 8)")
    p.add_argument("--format", choices=FORMATS, default="vertical", help="vertical | square | original")
    p.add_argument("--style", choices=STYLES, default="classic", help="caption style: classic | bold | minimal")
    p.add_argument("--min-len", type=int, default=20, help="min clip length, sec")
    p.add_argument("--max-len", type=int, default=60, help="max clip length, sec")
    p.add_argument("--min-score", type=float, default=0, help="skip clips with AI score below this")
    p.add_argument("--whisper-model", default="small",
                   help="tiny | base | small | medium | large-v3 (bigger = better, slower)")
    p.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto",
                   help="where Whisper runs: auto (GPU if it works, else CPU), cpu, cuda")
    p.add_argument("--language", default=None, help="speech language, e.g. en, uk, pl (auto if empty)")
    p.add_argument("--hook-lang", default=None, help="language for hooks and posts, e.g. English")
    p.add_argument("--hook-seconds", type=float, default=3.0, help="how long the hook stays on screen (0 = off)")
    p.add_argument("--ai", choices=["auto", "claude", "ollama"], default="auto",
                   help="who picks moments: auto (Claude if API key, else free local Ollama), claude, ollama")
    p.add_argument("--model", default=DEFAULT_MODEL, help="Claude model for picking moments")
    p.add_argument("--ollama-model", default=DEFAULT_OLLAMA_MODEL, help="local Ollama model (free)")
    p.add_argument("--manual", action="store_true", help="no API: paste the prompt into any AI chat yourself")
    p.add_argument("--moments", help="use a ready JSON file with moments")
    p.add_argument("--no-track", action="store_true", help="disable face tracking (center crop)")
    p.add_argument("--no-captions", action="store_true", help="don't burn captions")
    p.add_argument("--upper", action="store_true", help="CAPS captions")
    p.add_argument("--font", default="Arial", help="caption font (must be installed)")
    p.add_argument("--crf", type=int, default=20, help="video quality, lower = better (18-28)")
    p.add_argument("--keep-ass", action="store_true", help="keep .ass subtitle files")
    p.add_argument("--out", default="output", help="output folder")
    p.set_defaults(interactive=True)
    return p


def main():
    opt = build_parser().parse_args()
    if not opt.source:
        build_parser().print_help()
        return
    sources = [opt.source]
    if opt.source.lower().endswith(".txt") and Path(opt.source).exists():
        sources = [l.strip() for l in Path(opt.source).read_text(encoding="utf-8").splitlines()
                   if l.strip() and not l.strip().startswith("#")]
        log(f"Batch mode: {len(sources)} videos")
    failed = []
    for i, src in enumerate(sources, 1):
        if len(sources) > 1:
            log(f"===== Video {i}/{len(sources)}: {src}")
        try:
            process(src, opt)
        except (ClipperError, subprocess.CalledProcessError) as ex:
            log(f"ERROR: {ex}")
            failed.append(src)
    if failed:
        log(f"Failed: {len(failed)} -> " + ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
