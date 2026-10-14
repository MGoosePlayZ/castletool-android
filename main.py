"""Castletool Android entry point. Run: `flet run` (desktop) / `flet build apk`."""
import flet as ft

import theme
from app import App


async def main(page: ft.Page):
    page.title = "Castletool"
    page.padding = 0
    page.spacing = 0
    page.bgcolor = theme.BG
    page.theme_mode = ft.ThemeMode.DARK
    page.theme = theme.make_theme()
    page.dark_theme = theme.make_theme()
    page.fonts = theme.font_map()
    try:  # phone-shaped window when developing on desktop
        page.window.width, page.window.height = 420, 860
    except Exception:
        pass
    await App(page).start()


ft.run(main, assets_dir="assets")
