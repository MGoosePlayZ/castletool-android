"""App shell: navigation, top bar, dialogs, file picking, background work, the
signed-in profile (cached for offline use), Castle's deck list, opening/saving
cards, deleting local decks, and the in-app card player."""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import flet as ft

try:
    import flet_webview as fwv
except ImportError:  # desktop-only dev without the package
    fwv = None

import theme
from blueprint_ops import OpError
from castle_api import CastleAPI, CastleAPIError, OfflineError
from deck_list import list_recent_decks
from deck_store import clone_deck, list_local_decks, push_card, resolve_deck_ref
from serve import LocalServe
from session import CardSession, deck_has_unsaved
from theme import BG, MAJOR, MINOR1, SEP, castle_icon, minor, text, title
from widgets import icon_button, pill_button

WEBVIEW_PLATFORMS = (ft.PagePlatform.ANDROID, ft.PagePlatform.IOS, ft.PagePlatform.MACOS)
GLOBAL_LIMIT = 100
GLOBAL_MAX_AGE = 120.0   # seconds before the deck list is refreshed again


@dataclass
class Screen:
    build: Callable = lambda app: ft.Container()
    title: str = ""
    bar: Optional[Callable] = None            # full custom top bar
    left: Optional[Callable] = None           # replaces the back button
    actions: Callable = lambda app: []
    on_leave: Optional[Callable] = None
    on_back: Optional[Callable] = None        # custom back handling (may be async)
    title_font: bool = True


def storage_root() -> Path:
    """Persistent per-user data folder (decks, session, caches)."""
    env = os.getenv("FLET_APP_STORAGE_DATA")      # Android/iOS app-private storage
    if env:
        root = Path(env)
    elif sys.platform == "win32":
        root = Path(os.getenv("APPDATA") or Path.home() / "AppData" / "Roaming") / "Castletool"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support" / "Castletool"
    else:
        root = Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "castletool"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, value) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class App:
    def __init__(self, page: ft.Page):
        self.page = page
        self.api = CastleAPI()
        self.root = storage_root()
        self.decks_dir = self.root / "decks"
        self.decks_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = self.root / "cache"
        self.session_file = self.root / "session.json"
        self.profile_file = self.root / "profile.json"
        self.avatar_file = self.root / "profile-photo.img"
        self.global_file = self.root / "global_decks.json"

        self.user: Optional[dict] = None            # {userId, username} (cached while offline)
        self.avatar_bytes: Optional[bytes] = None
        self.online = True
        self.global_decks: list[dict] = []
        self.global_loaded_at = 0.0
        self.decks_tab = "RECENT"
        self.sessions: dict[tuple, CardSession] = {}
        self.stack: list[Screen] = []
        self.log_lines: list[str] = []
        self.log_targets: list[ft.Column] = []
        self.serve_server: Optional[LocalServe] = None
        self.login_cancel = False

        self.launcher = ft.UrlLauncher()
        self.picker = ft.FilePicker()
        self.use_webview = fwv is not None and page.platform in WEBVIEW_PLATFORMS

        self.topbar_host = ft.Container(border=ft.Border(bottom=ft.BorderSide(1, SEP)))
        self.body = ft.Container(expand=True)
        self.layout = ft.Column([self.topbar_host, self.body], spacing=0, expand=True)

    # ── startup ───────────────────────────────────────────────────────────
    async def start(self):
        from screens_library import home_screen
        self.page.add(ft.SafeArea(self.layout, expand=True))
        self._load_cached_session()
        self.push(home_screen(self))
        self.page.run_task(self._startup_refresh)
        missing = theme.missing_fonts() + theme.missing_icons()
        if missing:
            self.log("Missing files in assets/: " + ", ".join(missing))

    async def _startup_refresh(self):
        await self.refresh_profile()
        await self.refresh_global()

    def _load_cached_session(self):
        """A stored token means 'signed in', even with no connection."""
        sess = _read_json(self.session_file, {})
        token = sess.get("token")
        if token:
            self.api.token = token
            self.user = _read_json(self.profile_file) or {"userId": None, "username": ""}
            if self.avatar_file.exists():
                self.avatar_bytes = self.avatar_file.read_bytes()
        self.global_decks = _read_json(self.global_file, []) or []

    @property
    def signed_in(self) -> bool:
        return bool(self.api.token)

    def _save_session(self):
        _write_json(self.session_file, {"token": self.api.token})
        try:
            os.chmod(self.session_file, 0o600)
        except OSError:
            pass

    def _clear_session(self):
        self.api.token = None
        self.user = None
        self.avatar_bytes = None
        self.global_decks = []
        for f in (self.session_file, self.profile_file, self.avatar_file, self.global_file):
            f.unlink(missing_ok=True)

    # ── profile / connectivity ────────────────────────────────────────────
    async def refresh_profile(self):
        """Fetch username + picture. No connection keeps the cached ones (signed in,
        offline); a token Castle rejects means signed out."""
        if not self.api.token:
            return
        try:
            user = await asyncio.to_thread(self.api.me)
        except OfflineError:
            self.online = False
            self._repaint()
            return
        except Exception as ex:
            self.log(f"profile: {ex}")
            return
        self.online = True
        if user is None:
            self.log("Castle no longer accepts the saved sign-in.")
            self._clear_session()
            self._repaint()
            return
        self.user = {"userId": user.get("userId"), "username": user.get("username") or ""}
        _write_json(self.profile_file, self.user)
        self._repaint()
        photo = user.get("photo") or {}
        url = photo.get("avatarUrl") or photo.get("smallAvatarUrl") or photo.get("url")
        if url:
            try:
                data = await asyncio.to_thread(self.api.download_bytes, url)
                self.avatar_bytes = data
                self.avatar_file.write_bytes(data)
            except Exception as ex:
                self.log(f"profile picture: {ex}")
        self._repaint()

    async def ensure_online(self) -> bool:
        """For anything that needs Castle. Toasts why not."""
        if not self.api.token:
            self.toast("Sign in first.", error=True)
            return False
        if not self.online:
            await self.refresh_profile()
            if not self.online:
                self.toast("No Wi-Fi.", error=True)
                return False
        return True

    def _repaint(self):
        """Top bar (profile picture) may depend on the profile."""
        if self.stack:
            self.refresh_bar()

    def avatar(self, size: int = 36) -> ft.Control:
        """Profile picture: thin separator-gray ring, a thin black gap, then the picture."""
        inner = size - 8
        if self.avatar_bytes and self.signed_in:
            img: ft.Control = ft.Image(src=self.avatar_bytes, width=inner, height=inner, fit=ft.BoxFit.COVER)
        else:
            img = ft.Image(src="icons/default-profile.png", width=inner, height=inner, fit=ft.BoxFit.COVER,
                           error_content=ft.Container(castle_icon("people", inner * 0.6, MINOR1),
                                                      alignment=ft.Alignment.CENTER, width=inner, height=inner))
        return ft.Container(
            content=ft.Container(img, width=inner, height=inner, border_radius=inner / 2,
                                 clip_behavior=ft.ClipBehavior.ANTI_ALIAS),
            width=size, height=size, padding=3, alignment=ft.Alignment.CENTER, border_radius=size / 2,
            border=ft.Border.all(1, SEP))

    # ── Castle's deck list ────────────────────────────────────────────────
    async def refresh_global(self, force: bool = False):
        if not self.api.token or not (self.user or {}).get("userId"):
            return
        if not force and self.global_loaded_at and time.monotonic() - self.global_loaded_at < GLOBAL_MAX_AGE:
            return
        self.global_loaded_at = time.monotonic()     # also on failure: never hammer Castle / loop on rebuilds
        try:
            decks = await asyncio.to_thread(list_recent_decks, self.api, GLOBAL_LIMIT, self.user["userId"])
        except OfflineError:
            self.online = False
            return
        except Exception as ex:
            self.log(f"decks: {ex}")
            return
        self.online = True
        self.global_decks = decks
        _write_json(self.global_file, decks)
        self.refresh_body_if("Decks")

    # ── navigation ────────────────────────────────────────────────────────
    def push(self, screen: Screen):
        self.stack.append(screen)
        self._show()

    def pop(self):
        if len(self.stack) <= 1:
            return
        leaving = self.stack.pop()
        if leaving.on_leave:
            try:
                leaving.on_leave()
            except Exception as ex:
                self.log(f"[leave] {ex}")
        self._show()

    def go_back(self):
        """Back button: the screen may intercept it (e.g. 'Save changes?')."""
        s = self.stack[-1]
        if s.on_back:
            r = s.on_back()
            if inspect.isawaitable(r):
                self.spawn(r)
            return
        self.pop()

    def refresh(self):
        self._show()

    def refresh_body_if(self, screen_title: str):
        if self.stack and self.stack[-1].title == screen_title:
            self._show()

    def refresh_bar(self):
        self.topbar_host.content = self._bar(self.stack[-1])
        self.page.update()

    def _bar(self, s: Screen) -> ft.Control:
        if s.bar:
            return s.bar(self)
        if s.left:
            left = s.left(self)
        elif len(self.stack) > 1:
            left = icon_button(castle_icon("left_arrow", 22), lambda e: self.go_back())
        else:
            left = ft.Container()
        return ft.Container(ft.Row([
            ft.Container(left, width=56, alignment=ft.Alignment.CENTER_LEFT),
            ft.Container(title(s.title, 20, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS) if s.title_font
                         else text(s.title, 16, bold=True, max_lines=1), expand=True, alignment=ft.Alignment.CENTER),
            ft.Container(ft.Row(s.actions(self), spacing=0, tight=True), width=56, alignment=ft.Alignment.CENTER_RIGHT),
        ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=0),
            padding=ft.Padding(left=6, right=6, top=4, bottom=4))

    def _show(self):
        s = self.stack[-1]
        self.log_targets.clear()
        self.topbar_host.content = self._bar(s)
        self.body.content = s.build(self)
        self.page.update()

    # ── helpers ───────────────────────────────────────────────────────────
    def log(self, msg: str):
        for line in str(msg).splitlines() or [""]:
            self.log_lines.append(line)
            del self.log_lines[:-200]
            for col in self.log_targets:
                col.controls.append(minor(line, 11, selectable=True))
                del col.controls[:-200]
        try:
            self.page.update()
        except Exception:
            pass

    def log_view(self, height: Optional[int] = 140) -> ft.Column:
        col = ft.Column([minor(l, 11, selectable=True) for l in self.log_lines[-60:]], spacing=2,
                        scroll=ft.ScrollMode.AUTO, height=height, auto_scroll=True)
        self.log_targets.append(col)
        return col

    def spawn(self, awaitable):
        async def runner():
            await awaitable
        self.page.run_task(runner)

    async def run(self, fn, *args, report: bool = True):
        """Run blocking `fn` in a worker thread. Errors are logged and toasted;
        returns None on failure (so make `fn` return something truthy)."""
        try:
            return await asyncio.to_thread(fn, *args)
        except OfflineError:
            self.online = False
            self.log("error: no connection to Castle")
            if report:
                self.toast("No Wi-Fi.", error=True)
        except OpError as ex:
            self.log(f"error: {ex}")
            if report:
                self.toast(str(ex), error=True)
        except Exception as ex:
            self.log(f"ERROR: {ex}")
            if report:
                self.toast(str(ex), error=True)
        return None

    def toast(self, msg: str, error: bool = False):
        self.page.show_dialog(ft.SnackBar(content=text(msg, 13, MAJOR), bgcolor=BG, duration=3500,
                                          shape=theme.popup_shape(10)))

    def dialog(self, heading: str, content: ft.Control, actions: list):
        self.page.show_dialog(ft.AlertDialog(bgcolor=BG, shape=theme.popup_shape(), title=text(heading, 18, bold=True),
                                             content=content, actions=actions))

    def confirm(self, heading: str, body: str, ok_label: str, on_ok):
        async def _ok(e):
            self.page.pop_dialog()
            r = on_ok()
            if inspect.isawaitable(r):
                await r

        self.dialog(heading, text(body, 14, MINOR1) if body else ft.Container(height=0), [
            ft.TextButton(content=text("Cancel", 14, MINOR1), on_click=lambda e: self.page.pop_dialog()),
            ft.TextButton(content=text(ok_label, 14, MAJOR, bold=True), on_click=_ok)])

    async def pick(self, extensions: Optional[list[str]] = None, title_: str = "Choose a file") -> Optional[Path]:
        kwargs = dict(dialog_title=title_, allow_multiple=False)
        if extensions:
            kwargs["file_type"] = ft.FilePickerFileType.CUSTOM
            kwargs["allowed_extensions"] = [e.lstrip(".") for e in extensions]
        files = await self.picker.pick_files(**kwargs)
        if not files or not files[0].path:
            return None
        return Path(files[0].path)

    # ── sign in / out ─────────────────────────────────────────────────────
    async def _signed_in_as(self, user: dict):
        self._save_session()
        self.user = {"userId": user.get("userId"), "username": user.get("username") or ""}
        _write_json(self.profile_file, self.user)
        self.online = True
        self.global_loaded_at = 0.0
        await self.refresh_profile()
        await self.refresh_global(force=True)

    async def sign_in_password(self, username: str, password: str) -> bool:
        user = await self.run(lambda: self.api.login_password(username.strip(), password))
        if not user:
            self.api.token = None
            return False
        await self._signed_in_as(user)
        return True

    async def sign_in_browser(self) -> bool:
        self.login_cancel = False
        info = await self.run(self.api.start_login)
        if not info:
            return False
        await self.launcher.launch_url(info["url"])
        user = await self.run(lambda: self.api.wait_for_login(
            info["pollToken"], info["verifier"], cancelled=lambda: self.login_cancel))
        if not user:
            return False
        await self._signed_in_as(user)
        return True

    async def sign_out(self):
        self._clear_session()
        self.global_loaded_at = 0.0

    # ── decks: clone / open / delete ──────────────────────────────────────
    def clear_sessions(self, deck_id: str):
        for key in [k for k in self.sessions if k[0] == deck_id]:
            del self.sessions[key]

    def fresh_clone(self, deck_id: str) -> Path:
        """Clone into a scratch folder, then swap it in: a failed clone never destroys
        the copy already on the device."""
        scratch = self.decks_dir / ".incoming"
        shutil.rmtree(scratch, ignore_errors=True)
        try:
            clone_deck(self.api, deck_id, scratch, self.log)
            target = self.decks_dir / deck_id
            shutil.rmtree(target, ignore_errors=True)
            shutil.move(str(scratch / deck_id), str(target))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        self.clear_sessions(deck_id)
        return self.decks_dir / deck_id

    async def clone_ref(self, text_: str) -> Optional[Path]:
        """Clone from a deck id / link (incl. s.castle.xyz share links)."""
        if not await self.ensure_online():
            return None
        deck_id = await self.run(lambda: resolve_deck_ref(text_))
        if not deck_id:
            self.toast("That isn't a deck link.", error=True)
            return None
        path = await self.run(lambda: self.fresh_clone(deck_id))
        if path:
            self.global_loaded_at = 0.0
            self.refresh_body_if("Decks")
        return path

    async def open_deck(self, deck_id: str, local_dir: Optional[Path]):
        """Opening a deck re-clones it from Castle, unless that would lose unsaved
        edits or Castle can't be reached (then the copy on the device is used)."""
        from screens_library import deck_screen
        has_local = bool(local_dir and local_dir.exists())
        if has_local and deck_has_unsaved(local_dir):
            self.push(deck_screen(self, local_dir))
            return
        if self.api.token and (self.online or not has_local):
            if not self.online:
                await self.refresh_profile()
            if self.online:
                self.body.content = ft.Container(ft.ProgressRing(color=MAJOR, width=28, height=28, stroke_width=2),
                                                 alignment=ft.Alignment.CENTER, expand=True)
                self.page.update()
                path = await self.run(lambda: self.fresh_clone(deck_id))
                if path:
                    self.push(deck_screen(self, path))
                    return
                self._show()
                if not has_local:
                    return
        if has_local:
            self.push(deck_screen(self, local_dir))
        elif not self.api.token:
            self.toast("Sign in first.", error=True)
        else:
            self.toast("No Wi-Fi.", error=True)

    def ask_delete_deck(self, deck_dir: Path, name: str):
        unsaved = deck_has_unsaved(deck_dir)
        body = "Only the copy on this device is removed." + (" Unsaved changes will be lost." if unsaved else "")
        self.confirm(f"Delete \"{name}\"?", body, "Delete", lambda: self.delete_deck(deck_dir))

    async def delete_deck(self, deck_dir: Path):
        try:
            await asyncio.to_thread(shutil.rmtree, deck_dir)
        except OSError as ex:
            self.log(f"ERROR: {ex}")
            self.toast(str(ex), error=True)
            return
        self.clear_sessions(deck_dir.name)
        self.refresh_body_if("Decks")

    # ── editing sessions / saving ─────────────────────────────────────────
    def session_for(self, deck_dir: Path, card_id: str) -> CardSession:
        key = (deck_dir.name, card_id)
        if key not in self.sessions:
            self.sessions[key] = CardSession(deck_dir / "cards" / card_id)
        return self.sessions[key]

    async def save_card(self, ctx) -> bool:
        """Save this card to Castle (one card; the rest of the deck is untouched)."""
        if ctx.saving:
            return False
        if not await self.ensure_online():
            return False
        ctx.saving = True
        self.refresh_bar()
        ok = await self.run(lambda: push_card(self.api, ctx.deck_dir, ctx.card_id, self.log) or True)
        ctx.saving = False
        if ok:
            ctx.session.mark_saved()
        self.refresh_bar()
        return bool(ok)

    # ── player ────────────────────────────────────────────────────────────
    def stop_serve(self):
        srv, self.serve_server = self.serve_server, None
        if srv:
            try:
                srv.stop()
            except Exception as ex:
                self.log(f"[serve] {ex}")

    async def play_card(self, ctx):
        from cardbar import player_screen
        if not self.use_webview:
            self.toast("The player needs the Android app.", error=True)
            return
        if self.serve_server:
            self.stop_serve()

        def start():
            srv = LocalServe(ctx.deck_dir, username=(self.user or {}).get("username", ""),
                             cache_dir=self.cache_dir, on_log=self.log, card_id=ctx.card_id)
            srv.start()
            return srv

        srv = await self.run(start)
        if not srv:
            return
        self.serve_server = srv
        self.push(player_screen(self, ctx, srv.url))


# ── profile screen ─────────────────────────────────────────────────────────

def profile_screen(app: App) -> Screen:
    name = title("", 24)
    status = minor("", 13)
    host = ft.Column(spacing=10, horizontal_alignment=ft.CrossAxisAlignment.CENTER)
    user_f = ft.TextField(hint_text="Username or email", dense=True, border_color=SEP, width=280,
                          text_style=ft.TextStyle(font_family=theme.BODY, color=MAJOR))
    pass_f = ft.TextField(hint_text="Password", password=True, can_reveal_password=True, dense=True,
                          border_color=SEP, width=280, text_style=ft.TextStyle(font_family=theme.BODY, color=MAJOR))
    state = {"busy": False}

    def paint():
        if app.signed_in:
            name.value = (app.user or {}).get("username") or ""
            status.value = "" if app.online else "No Wi-Fi"
            host.controls = [pill_button("Sign out", on_sign_out)]
        else:
            name.value = ""
            status.value = "Not signed in"
            host.controls = [
                user_f, pass_f,
                pill_button("Sign in", on_sign_in, filled=True, busy=state["busy"], width=280),
                pill_button("Sign in with browser", on_sign_in_browser, disabled=state["busy"], width=280),
            ]
        app.page.update()

    async def on_sign_in(e):
        if not user_f.value or not pass_f.value:
            return
        state["busy"] = True
        paint()
        ok = await app.sign_in_password(user_f.value, pass_f.value)
        state["busy"] = False
        pass_f.value = ""
        if ok:
            app.refresh()
        else:
            paint()

    async def on_sign_in_browser(e):
        state["busy"] = True
        paint()
        ok = await app.sign_in_browser()
        state["busy"] = False
        if ok:
            app.refresh()
        else:
            paint()

    async def on_sign_out(e):
        await app.sign_out()
        app.refresh()

    async def on_retry(e):
        if app.signed_in and not app.online:
            await app.refresh_profile()
            app.refresh()

    def build(a: App) -> ft.Control:
        paint()
        return ft.Column([
            ft.Container(ft.Column([
                ft.GestureDetector(a.avatar(88), on_tap=on_retry), name, status, host,
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=10), padding=ft.Padding(top=24, bottom=16),
                alignment=ft.Alignment.CENTER),
            theme.separator(),
            ft.Container(ft.Column([minor("Log", 11), a.log_view(220)], spacing=4), padding=14),
        ], scroll=ft.ScrollMode.AUTO, spacing=0)

    return Screen(title="Profile", build=build)
