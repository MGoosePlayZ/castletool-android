# castletool-android

Decks → cards → blueprints. Upload media/fonts/MIDI/audio, preview, play, save cards to Castle.
No CLI, no ffmpeg binary, no browser.

## Project layout (this folder is the project root)

```
castletool-android/            <- run every uv/git command here (pyproject.toml is in this folder)
├─ pyproject.toml              <- dependencies + Flet build settings
├─ uv.lock
├─ README.md
├─ .gitignore
├─ .github/workflows/build-apk.yml
├─ src/                        <- the app (this is what gets packaged)
│  ├─ main.py  app.py  ...
│  ├─ player/                  <- Castle's WASM player (do not touch)
│  └─ assets/
│     ├─ fonts/                <- YOU ADD: Basteleur-Bold.otf, CastleIcon.ttf, DMSans-Regular.ttf,
│     │                           DMSans-Medium.ttf, DMSans-Bold.ttf   (+ 2 MaterialSymbols files, already here)
│     ├─ icons/                <- YOU ADD: key-info.png, default-profile.png
│     └─ icon.png              <- optional: 1024x1024 transparent PNG, the Android launcher icon
├─ tests/                      <- not packaged into the APK
└─ tools/make_icon_fonts.py
```

## Setup on Windows 11 (PowerShell)

1. Install tools once: `winget install --id=astral-sh.uv -e` and `winget install --id=Git.Git -e`. Reopen PowerShell.
2. Extract the zip. You get a `castletool-android` folder. Open PowerShell **inside it**: `cd path\to\castletool-android`.
3. Add your files (names are exact, case-sensitive on GitHub's Linux runner):
   - `src\assets\fonts\Basteleur-Bold.otf`
   - `src\assets\fonts\CastleIcon.ttf`
   - `src\assets\fonts\DMSans-Regular.ttf`, `DMSans-Medium.ttf`, `DMSans-Bold.ttf` (Google Fonts -> DM Sans -> Download all -> `static` folder; rename if they are called `DMSans_18pt-Regular.ttf` etc.)
   - `src\assets\icons\key-info.png`, `src\assets\icons\default-profile.png`
4. `uv sync` creates `.venv` (Python 3.14, downloaded automatically) and installs everything. Do **not** run `uv init`
   (the project already exists) and you don't need `uv venv` (sync makes the venv).
5. Check: `uv run python tests/run_all.py` (all PASS).
6. Run: `uv run flet run src/main.py`. Everything works on PC except the in-app card player (needs the Android build).

## Build the APK on GitHub

The fonts are not free to redistribute, so use a **private** repository.

```
git init -b main
git add .
git commit -m "castletool android"
git remote add origin https://github.com/<you>/<repo>.git      # empty private repo made on github.com
git push -u origin main
```

GitHub -> Actions -> "Build APK" runs on every push (or press "Run workflow"). When it is green, open the run and
download the artifact `flet-apk.zip`. Unzip it and install the `.apk` (copy to the phone, or `adb install file.apk`).
The first build takes ~15 min, later ones are faster (cached).

If the app misbehaves on the phone: `adb logcat -s flet.python`
