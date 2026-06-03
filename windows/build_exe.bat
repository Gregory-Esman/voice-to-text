@echo off
REM Build the Voice-To-Text Windows .exe locally. Run from the repo root:
REM    windows\build_exe.bat
setlocal
echo === Installing build deps ===
python -m pip install --upgrade pip || goto :err
python -m pip install -r windows\requirements-windows.txt || goto :err
python -m pip install pyinstaller || goto :err

echo === Building (PyInstaller) ===
pyinstaller --noconfirm windows\VoiceToText.spec || goto :err

echo === Packaging zip ===
if exist release rmdir /s /q release
mkdir release
copy /y dist\VoiceToText.exe release\ >nul
copy /y windows\config.example.toml release\ >nul
copy /y windows\README.md release\README.txt >nul
powershell -NoProfile -Command "Compress-Archive -Force -Path release\* -DestinationPath Voice-To-Text-Windows.zip" || goto :err

echo.
echo Done: dist\VoiceToText.exe  and  Voice-To-Text-Windows.zip
goto :eof

:err
echo BUILD FAILED (see errors above).
exit /b 1
