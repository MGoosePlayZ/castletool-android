# castletool-android

Decks → cards → blueprints. Upload media/fonts/MIDI/audio, preview, play, save cards to Castle.
No CLI, no ffmpeg binary, no browser.

    pip install -r requirements.txt      # tests also use scipy
    for t in test_*.py; do python $t; done
    flet run                             # desktop dev
    flet build apk

## Files you supply (nothing is downloaded at runtime)

`assets/fonts/`
| File | Source |
|---|---|
| `Basteleur-Bold.otf` | you |
| `CastleIcon.ttf` | you |
| `DMSans-Regular.ttf`, `DMSans-Medium.ttf`, `DMSans-Bold.ttf` | Google Fonts → DM Sans → Download all → `static/` folder (rename if they come as `DMSans_18pt-Regular.ttf`) |
| `MaterialSymbols-W200.ttf`, `MaterialSymbols-W200-Filled.ttf` | included (2 KB, weight 200; filled only for lock). To add icons: get `MaterialSymbolsOutlined[FILL,GRAD,opsz,wght].ttf` from github.com/google/material-design-icons (`variablefont/`), add the icon to `MD` in `theme.py`, run `python tools/make_icon_fonts.py "<that file>"` |

`assets/icons/`
| File | Use |
|---|---|
| `key-info.png` | the "explain this" icon, drawn at 25% opacity |
| `default-profile.png` | profile picture when signed out |

Missing files are listed in the profile screen's log.

## Where things live
Per-user folder (`%APPDATA%\Castletool` on Windows, app storage on Android): `decks/`, `session.json` (sign-in token),
`profile.json` + `profile-photo.img` (cached username/picture for offline), `global_decks.json`.
