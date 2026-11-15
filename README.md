# GameShot

Steam-style game screenshot tool for Windows.

## Build on Windows

Install Python 3.9+ and run:

```powershell
python -m pip install -r requirements.txt
python build.py
```

The executable is written to `dist/GameShot.exe`. It requests Administrator privileges at launch.

## Use

- Press F12 (configurable) while in a game to take a screenshot.
- Screenshots are saved under `Pictures/GameScreenshots/<Game name>/`.
- Game detection and settings are saved automatically.

See [README.txt](README.txt) for the original usage notes. GitHub Actions builds a Windows x64 executable on every push to `main` and publishes it as a downloadable workflow artifact.
