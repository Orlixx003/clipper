@echo off
REM ============================================================
REM  Builds Clipper.exe for selling.  Double-click this file.
REM  Result: dist\Clipper-Windows.zip  (upload it to Gumroad)
REM ============================================================
cd /d "%~dp0"

echo.
echo [1/5] Creating a clean build environment...
if not exist build_venv python -m venv build_venv
call build_venv\Scripts\activate.bat || goto :error

echo.
echo [2/5] Installing libraries (a few minutes the first time)...
python -m pip install --upgrade pip >nul
pip install -r requirements.txt pyinstaller || goto :error

echo.
echo [3/5] Getting ffmpeg (bundled inside the app)...
if not exist ffmpeg\ffmpeg.exe (
    if not exist ffmpeg mkdir ffmpeg
    powershell -NoProfile -Command "Invoke-WebRequest https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip -OutFile ffmpeg\ff.zip" || goto :error
    powershell -NoProfile -Command "Expand-Archive ffmpeg\ff.zip -DestinationPath ffmpeg\tmp -Force" || goto :error
    powershell -NoProfile -Command "Get-ChildItem ffmpeg\tmp -Recurse -Include ffmpeg.exe,ffprobe.exe,LICENSE* | Copy-Item -Destination ffmpeg" || goto :error
    rmdir /s /q ffmpeg\tmp
    del ffmpeg\ff.zip
)

echo.
echo [4/5] Building Clipper.exe (5-15 minutes)...
pyinstaller --noconfirm --clean --name Clipper --console ^
  --add-binary "ffmpeg\ffmpeg.exe;ffmpeg" --add-binary "ffmpeg\ffprobe.exe;ffmpeg" ^
  --collect-all gradio --collect-all gradio_client --collect-all safehttpx --collect-all groovy ^
  --collect-all faster_whisper --collect-all ctranslate2 --collect-all onnxruntime ^
  --collect-data cv2 --collect-submodules yt_dlp --collect-all platformdirs --hidden-import pkg_resources ^
  --hidden-import clipper --hidden-import app --hidden-import settings --hidden-import product ^
  launcher.py || goto :error

echo.
echo [5/5] Packing dist\Clipper-Windows.zip ...
copy /y BUYER_GUIDE.txt dist\Clipper\ >nul
if exist ffmpeg\LICENSE* copy /y ffmpeg\LICENSE* dist\Clipper\ >nul
powershell -NoProfile -Command "Compress-Archive -Path dist\Clipper -DestinationPath dist\Clipper-Windows.zip -Force" || goto :error

echo.
echo ============================================================
echo  DONE!  Test it:  dist\Clipper\Clipper.exe
echo  Upload to Gumroad:  dist\Clipper-Windows.zip
echo ============================================================
pause
exit /b 0

:error
echo.
echo *** BUILD FAILED - send a screenshot of the text above ***
pause
exit /b 1
