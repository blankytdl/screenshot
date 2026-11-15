GameShot - Steam-style game screenshot tool (Windows)
=====================================================

BUILD THE APP
  1. Install Python 3.9+ from python.org (tick "Add Python to PATH").
  2. Double-click build.bat
  3. Your app: dist\GameShot.exe (it asks for Administrator on launch)

The repository contains GameShot.py. The build script generates icon.ico
and icon.png automatically when they are missing.

RUN WITHOUT BUILDING
  pip install -r requirements.txt
  python build.py

USE IT
  - Press F12 (changeable) inside any game to take a screenshot.
  - Screenshots go to Pictures\GameScreenshots\<Game name>\
  - Games are detected and remembered automatically; anything that is not a
    game is filed under "Desktop". See the Games tab to rename or add games.
  - Settings save automatically to %APPDATA%\GameShot\

TIPS
  - Black or missing screenshots? Switch the game to borderless-windowed.
  - Close the window = keep running in the tray (Quit from the tray menu).
