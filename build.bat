@echo off
rem ローカルで Setup.exe を作るスクリプト (Python 3.10+ と Inno Setup 6 が必要)
setlocal
python -m pip install -r requirements.txt pyinstaller || goto :err
pyinstaller --noconfirm --clean --noconsole --name LiveWallpaper live_wallpaper.py || goto :err

set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist %ISCC% set ISCC="%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist %ISCC% (
  echo Inno Setup 6 が見つかりません: https://jrsoftware.org/isdl.php
  goto :err
)
%ISCC% installer.iss || goto :err
echo.
echo 完成: installer_output\LiveWallpaper-Setup.exe
exit /b 0
:err
echo 失敗しました
exit /b 1
