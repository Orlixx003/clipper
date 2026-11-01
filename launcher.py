"""Entry point of the packaged app (Clipper.exe). Makes the bundled ffmpeg visible, then starts the UI."""
import os
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    os.environ["PATH"] = str(base / "ffmpeg") + os.pathsep + os.environ.get("PATH", "")
    # Whisper models are cached in the user's profile, not next to the .exe
    os.environ.setdefault("HF_HOME", str(Path.home() / ".cache" / "clipper-models"))

try:
    import app
    app.main()
except Exception as ex:          # keep the window open so the buyer can read / screenshot the error
    import traceback
    traceback.print_exc()
    print(f"\nClipper could not start: {ex}")
    input("Press Enter to close...")
