#!/usr/bin/env python3
"""Clipper web interface. Run:  python app.py  -> opens http://127.0.0.1:7860 in the browser."""

import os
import shutil
import sys
from pathlib import Path

import gradio as gr

import clipper
import product
import settings

# Whisper needs language codes ("en"), people type names ("English")
LANG_CODES = {"english": "en", "russian": "ru", "ukrainian": "uk", "polish": "pl", "spanish": "es",
              "german": "de", "french": "fr", "italian": "it", "portuguese": "pt", "turkish": "tr",
              "arabic": "ar", "hindi": "hi", "japanese": "ja", "korean": "ko", "chinese": "zh"}

# where finished clips go: next to app.py when run from source, Documents\Clipper in the .exe
if getattr(sys, "frozen", False):
    OUT_DIR = Path.home() / "Documents" / "Clipper"
else:
    OUT_DIR = Path("output")


# ───────────────────────── Create clips ─────────────────────────

def run(link, upload, n, fmt, style, min_len, max_len, hook_lang, whisper_model, language,
        upper, no_track, moments_json):
    ok, msg = settings.is_licensed()
    if not ok:
        raise gr.Error(f"{msg} Open the Settings tab and enter your license key.")
    cfg = settings.load()
    settings.apply_to_env(cfg)

    # empty fields can arrive as None from the browser -> normalise everything
    link, hook_lang, language, moments_json = (x or "" for x in (link, hook_lang, language, moments_json))
    source = (upload if upload else link.strip())
    if not source:
        raise gr.Error("Paste a link or upload a video.")

    args = ["-n", str(int(n or 8)), "--format", fmt or "vertical", "--style", style or "classic",
            "--min-len", str(int(min_len or 20)), "--max-len", str(int(max_len or 60)),
            "--whisper-model", whisper_model or "small",
            "--ai", cfg["ai"], "--model", cfg["claude_model"] or clipper.DEFAULT_MODEL,
            "--ollama-model", cfg["ollama_model"] or clipper.DEFAULT_OLLAMA_MODEL,
            "--out", str(OUT_DIR)]
    if hook_lang.strip():
        args += ["--hook-lang", hook_lang.strip()]
    lang = LANG_CODES.get(language.strip().lower(), language.strip().lower())
    if lang:
        args += ["--language", lang]
    if upper:
        args.append("--upper")
    if no_track:
        args.append("--no-track")
    opt = clipper.build_parser().parse_args(args)
    opt.interactive = False

    try:
        clipsdir, report = clipper.process(source, opt, moments_text=moments_json.strip() or None)
    except clipper.ClipperError as ex:
        if str(ex) == "NEED_MOMENTS":
            prompt = clipper.prompt_for(source, opt).read_text(encoding="utf-8")
            msg = ("### No AI connected\n"
                   "Go to **Settings** and either download the free AI model (Ollama) "
                   "or paste your Claude API key.\n\n"
                   "Or do it manually: copy the prompt below into Claude / ChatGPT, paste the JSON answer "
                   "into **\"Moments JSON\"** and click the button again.")
            return [], msg, None, gr.update(value=prompt, visible=True)
        raise gr.Error(str(ex))
    except Exception as ex:
        raise gr.Error(f"Something went wrong: {ex}")

    md = [f"## Done: {len(report)} clips\nSaved to `{clipsdir}`\n"]
    for r in report:
        md.append(f"### {r['file']}  —  {r['score']:g}/10\n"
                  f"**Hook:** {r['hook']}  \n**Time:** {clipper.fmt_time(r['start'])}–{clipper.fmt_time(r['end'])} "
                  f"({r['length']}s)  \n**Why:** {r['reason']}\n\n**Post for X:**\n```\n{r['post']}\n```\n")
    zip_path = shutil.make_archive(str(clipsdir.parent / "clips"), "zip", clipsdir)
    gallery = [(r["path"], f"{r['score']:g}/10 · {r['hook']}") for r in report]
    return gallery, "\n".join(md), zip_path, gr.update(visible=False)


# ───────────────────────── Settings ─────────────────────────

def license_status_md():
    if not settings.licensing_enabled():
        return "✅ **Personal edition** — no license needed."
    ok, msg = settings.is_licensed()
    if ok:
        return f"✅ **{msg}**"
    buy = f" Buy a license: {product.BUY_URL}" if product.BUY_URL else ""
    return f"❌ **{msg}**{buy}"


def ai_status_md():
    cfg = settings.load()
    has_key = bool(cfg["claude_key"] or os.getenv("ANTHROPIC_API_KEY"))
    if cfg["ai"] == "claude" or (cfg["ai"] == "auto" and has_key):
        return ("✅ Using **Claude** with your API key." if has_key
                else "⚠️ Claude selected, but no API key yet — paste it below and click Save.")
    models = clipper.ollama_models()
    if models is None:
        return ("⚠️ **Ollama is not running.** Install it from https://ollama.com (free), "
                "start it, then click **Refresh status**.")
    want = cfg["ollama_model"] or clipper.DEFAULT_OLLAMA_MODEL
    if any(m == want or m.split(":")[0] == want for m in models):
        return f"✅ Free local AI ready: **{want}**"
    return f"⚠️ Ollama is running, but the model **{want}** is not downloaded yet — click **Download AI model**."


def do_activate(key):
    if not (key or "").strip():
        return "❌ Paste your license key first."
    ok, msg = settings.activate(key)
    return ("✅ " if ok else "❌ ") + msg


def do_save(ai, claude_key, claude_model, ollama_model):
    cfg = settings.load()
    cfg["ai"] = "claude" if ai.startswith("Claude") else "ollama"
    cfg["claude_key"] = (claude_key or "").strip()
    cfg["claude_model"] = (claude_model or "").strip()
    cfg["ollama_model"] = (ollama_model or "").strip()
    settings.save(cfg)
    return ai_status_md()


def do_pull(ollama_model):
    model = (ollama_model or "").strip() or clipper.DEFAULT_OLLAMA_MODEL
    if clipper.ollama_models() is None:
        return "⚠️ Ollama is not running. Install/start it from https://ollama.com first."
    try:
        clipper.ollama_pull(model)
    except Exception as ex:
        return f"❌ {ex}"
    cfg = settings.load()
    cfg["ollama_model"], cfg["ai"] = model, "ollama"
    settings.save(cfg)
    return ai_status_md()


AI_CHOICES = ["Free — Ollama (runs on this computer)", "Claude — my own API key (best quality)"]


def build_ui():
    cfg = settings.load()
    with gr.Blocks(title=product.PRODUCT_NAME) as demo:
        gr.Markdown(f"# ✂️ {product.PRODUCT_NAME}\n"
                    "Turn long videos into viral clips with captions, hooks and ready-to-post X copy")
        with gr.Tabs():
            with gr.Tab("Create clips"):
                with gr.Row():
                    with gr.Column(scale=1):
                        link = gr.Textbox(label="Video link (YouTube, X, Twitch…)",
                                          placeholder="https://www.youtube.com/watch?v=...")
                        upload = gr.File(label="…or upload a video", file_types=["video"], type="filepath")
                        with gr.Row():
                            n = gr.Slider(1, 20, value=8, step=1, label="Number of clips")
                            fmt = gr.Radio(["vertical", "square", "original"], value="vertical", label="Format")
                        style = gr.Radio(["classic", "bold", "minimal"], value="classic", label="Caption style")
                        with gr.Accordion("Advanced", open=False):
                            with gr.Row():
                                min_len = gr.Number(20, label="Min length, sec")
                                max_len = gr.Number(60, label="Max length, sec")
                            hook_lang = gr.Textbox(label="Hook & post language",
                                                   placeholder="English (empty = same as video)")
                            language = gr.Textbox(label="Video language",
                                                  placeholder="English / Spanish / en (empty = auto-detect)")
                            whisper_model = gr.Dropdown(["tiny", "base", "small", "medium", "large-v3"],
                                                        value="small",
                                                        label="Transcription model (bigger = more accurate, slower)")
                            upper = gr.Checkbox(label="ALL CAPS captions")
                            no_track = gr.Checkbox(label="Disable face tracking")
                            moments_json = gr.Textbox(label="Moments JSON (optional, manual mode)", lines=3)
                        btn = gr.Button("Generate clips", variant="primary")
                    with gr.Column(scale=2):
                        prompt_box = gr.Textbox(label="Prompt for AI chat", lines=8, visible=False)
                        gallery = gr.Gallery(label="Clips", columns=4, height="auto")
                        zip_file = gr.File(label="Download all clips (.zip)")
                        report = gr.Markdown()
                btn.click(run, [link, upload, n, fmt, style, min_len, max_len, hook_lang, whisper_model,
                                language, upper, no_track, moments_json],
                          [gallery, report, zip_file, prompt_box])

            with gr.Tab("Settings"):
                if settings.licensing_enabled():
                    gr.Markdown("### License")
                    lic_status = gr.Markdown(license_status_md())
                    with gr.Row():
                        lic_key = gr.Textbox(label="License key (from your purchase email)",
                                             value=cfg["license_key"], scale=3)
                        lic_btn = gr.Button("Activate", scale=1)
                    lic_btn.click(do_activate, [lic_key], [lic_status])

                gr.Markdown("### AI that finds the best moments")
                ai_status = gr.Markdown(ai_status_md())
                ai = gr.Radio(AI_CHOICES, value=AI_CHOICES[1] if cfg["ai"] == "claude" else AI_CHOICES[0],
                              label="Choose AI")
                with gr.Accordion("Free local AI (Ollama)", open=cfg["ai"] != "claude"):
                    gr.Markdown("1. Install **Ollama** from https://ollama.com (free) and start it.\n"
                                "2. Click **Download AI model** below (one time, ~5 GB).")
                    ollama_model = gr.Textbox(label="Ollama model",
                                              value=cfg["ollama_model"] or clipper.DEFAULT_OLLAMA_MODEL)
                    pull_btn = gr.Button("Download AI model")
                with gr.Accordion("Claude (your own API key)", open=cfg["ai"] == "claude"):
                    gr.Markdown("Get a key at https://console.anthropic.com → API Keys. "
                                "You pay Anthropic directly — usually a few cents per long video.")
                    claude_key = gr.Textbox(label="Claude API key", value=cfg["claude_key"], type="password")
                    claude_model = gr.Textbox(label="Claude model",
                                              value=cfg["claude_model"] or clipper.DEFAULT_MODEL)
                with gr.Row():
                    save_btn = gr.Button("Save", variant="primary")
                    refresh_btn = gr.Button("Refresh status")
                save_btn.click(do_save, [ai, claude_key, claude_model, ollama_model], [ai_status])
                pull_btn.click(do_pull, [ollama_model], [ai_status])
                refresh_btn.click(lambda: ai_status_md(), None, [ai_status])

                gr.Markdown(f"---\n{product.PRODUCT_NAME} v{product.VERSION} · Support: {product.SUPPORT_CONTACT}  \n"
                            f"Clips are saved to: `{OUT_DIR.resolve()}`")
    return demo


demo = build_ui()


def main():
    settings.apply_to_env()
    print(f"[clipper] {product.PRODUCT_NAME} v{product.VERSION}")
    print(f"[clipper] License: {license_status_md()}")
    print(f"[clipper] AI: {ai_status_md()}")
    print("[clipper] Opening in your browser... keep this window open while you use Clipper.")
    demo.launch(inbrowser=True)


if __name__ == "__main__":
    main()
