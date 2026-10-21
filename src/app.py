"""App shell: navigation, top bar, dialogs (with a task progress overlay), file picking,
background work with cancel, the signed-in profile (cached for offline use), Castle's
deck list, opening/saving cards, deleting local decks, developer settings and logs."""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import flet as ft

import tasks
import theme
from blueprint_ops import OpError
from castle_api import CastleAPI, CastleAPIError, DeckDeleted, OfflineError
from deck_list import list_recent_decks
from deck_store import clone_deck, push_card, resolve_deck_ref
from icons import IconStore
from session import CardSession, deck_has_unsaved
from theme import BG, DANGER, MAJOR, MINOR1, SEP, castle_icon, minor, text, title
from widgets import icon_button, pill_button

GLOBAL_LIMIT = 100
GLOBAL_MAX_AGE = 120.0   # seconds before the deck list is refreshed again
LOG_KEEP = 3000
LOG_LEVELS = [(0, "Errors only"), (1, "Normal"), (2, "Verbose")]
LEVEL_NAMES = {0: "ERROR", 1: "INFO", 2: "VERBOSE"}
DEFAULT_SETTINGS = {"dev_mode": False, "log_level": 1, "onboarded": False}
_CANCELLED = object()
_DELETED = object()          # a deck Castle no longer lets us see


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
    sig: Optional[Callable] = None            # bar signature: the bar is only rebuilt when it changes


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


class ProgressView:
    """Greyed-out screen with a circular meter, the percentage below it and a status line."""

    def __init__(self, app: "App", task: tasks.Task):
        self.app, self.task = app, task
        self.ring = ft.ProgressRing(width=96, height=96, stroke_width=6, color=MAJOR, bgcolor=SEP, value=None)
        self.pct = text("", 20, MAJOR, bold=True, text_align=ft.TextAlign.CENTER)
        self.status = minor("", 13, text_align=ft.TextAlign.CENTER, max_lines=2,
                            overflow=ft.TextOverflow.ELLIPSIS)
        self.cancel_btn = pill_button("Cancel", self._cancel, width=120)
        self.dlg = ft.AlertDialog(
            modal=True, bgcolor="#00000000", elevation=0, shadow_color="#00000000", barrier_color="#B3000000",
            content_padding=0, title_padding=0, actions_padding=0, inset_padding=ft.Padding.all(24),
            content=ft.Container(ft.Column([
                self.ring, self.pct, ft.Container(self.status, width=240), self.cancel_btn,
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=14, tight=True),
                alignment=ft.Alignment.CENTER, width=280, padding=20))
        self.shown = False
        task._on_update = self.update

    def show(self) -> None:
        self.app.page.show_dialog(self.dlg)
        self.shown = True

    def close(self) -> None:
        if self.shown:
            self.shown = False
            self.app.close_dialog(self.dlg)

    def _cancel(self, e) -> None:
        self.task.cancel()
        self.status.value = "Cancelling…"
        self.cancel_btn.opacity = 0.4
        self.cancel_btn.on_click = None
        self.refresh()

    def update(self, task: tasks.Task) -> None:
        f = task.frac
        self.ring.value = f
        self.pct.value = f"{round(f * 100)}%" if f is not None else ""
        if not task.cancelled:
            self.status.value = task.message or task.label
        self.refresh()

    def refresh(self) -> None:
        try:
            self.app.page.update()
        except Exception:
            pass


class App:
    def __init__(self, page: ft.Page):
        self.page = page
        self.api = CastleAPI()
        self.root = storage_root()
        self.decks_dir = self.root / "decks"
        self.decks_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = self.root / "cache"
        self.cauldron_cache = self.cache_dir / "cauldron.json"      # {cardId: {url, cauldron}}
        self.session_file = self.root / "session.json"
        self.profile_file = self.root / "profile.json"
        self.avatar_file = self.root / "profile-photo.img"
        self.global_file = self.root / "global_decks.json"
        self.settings_file = self.root / "settings.json"
        self.settings: dict = {**DEFAULT_SETTINGS, **(_read_json(self.settings_file, {}) or {})}
        self.icons = IconStore(self.decks_dir)

        self.user: Optional[dict] = None            # {userId, username} (cached while offline)
        self.avatar_bytes: Optional[bytes] = None
        self.online = True
        self.global_decks: list[dict] = []
        self.global_loaded_at = 0.0
        self.decks_tab = "RECENT"
        self.sessions: dict[tuple, CardSession] = {}
        self.stack: list[Screen] = []
        self.log_entries: list[tuple[float, int, str]] = []
        self.log_targets: list[ft.Column] = []
        self.active_tasks: set[tasks.Task] = set()
        self.opening: set[str] = set()              # deck ids currently being opened
        self.login_cancel = False
        self.last_work_cancelled = False
        self._bar_sig = None

        self.launcher = ft.UrlLauncher()
        self.picker = ft.FilePicker()
        self.clipboard = ft.Clipboard()

        self.topbar_host = ft.Container(border=ft.Border(bottom=ft.BorderSide(1, SEP)))
        self.body = ft.Container(expand=True)
        self.layout = ft.Column([self.topbar_host, self.body], spacing=0, expand=True)

    # ── startup ───────────────────────────────────────────────────────────
    async def start(self):
        from screens_library import home_screen
        self.page.add(ft.SafeArea(self.layout, expand=True))
        self._setup_back()
        self._load_cached_session()
        self.log(f"Castletool {theme.APP_VERSION} on {getattr(self.page.platform, 'value', self.page.platform)}", 2)
        self.push(signin_screen(self) if self.needs_signin_gate else home_screen(self))
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
            if not self.settings.get("onboarded"):          # already signed in: skip the first-run screens
                self.set_setting("onboarded", True)
            self.user = _read_json(self.profile_file) or {"userId": None, "username": ""}
            if self.avatar_file.exists():
                self.avatar_bytes = self.avatar_file.read_bytes()
        self.global_decks = _read_json(self.global_file, []) or []

    @property
    def signed_in(self) -> bool:
        return bool(self.api.token)

    @property
    def needs_signin_gate(self) -> bool:
        """Until the first sign-in the app shows only the sign-in screen. After that, never again
        (signing out later gives the normal app with 'You are not signed in')."""
        return not self.signed_in and not self.settings.get("onboarded")

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

    # ── settings ──────────────────────────────────────────────────────────
    @property
    def dev_mode(self) -> bool:
        return bool(self.settings.get("dev_mode"))

    def set_setting(self, key: str, value) -> None:
        self.settings[key] = value
        _write_json(self.settings_file, self.settings)

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
            self.log(f"error: profile: {ex}")
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
                changed = data != self.avatar_bytes
                self.avatar_bytes = data
                self.avatar_file.write_bytes(data)
                if not changed:
                    return                       # same picture: nothing to repaint (no flicker)
            except Exception as ex:
                self.log(f"error: profile picture: {ex}")
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

    def avatar(self, size: int = 36, ring: bool = True) -> ft.Control:
        """Profile picture. With `ring`: a thin separator-gray outline, then a gap exactly as
        thin as the outline, then the picture. Without: just the picture."""
        pad = 1 if ring else 0
        inner = size - 2 * (pad + (1 if ring else 0))
        if self.avatar_bytes and self.signed_in:
            img: ft.Control = ft.Image(src=self.avatar_bytes, width=inner, height=inner, fit=ft.BoxFit.COVER,
                                       gapless_playback=True)
        else:
            img = ft.Image(src="icons/default-profile.png", width=inner, height=inner, fit=ft.BoxFit.COVER,
                           gapless_playback=True,
                           error_content=ft.Container(castle_icon("people", inner * 0.6, MINOR1),
                                                      alignment=ft.Alignment.CENTER, width=inner, height=inner))
        pic = ft.Container(img, width=inner, height=inner, border_radius=inner / 2,
                           clip_behavior=ft.ClipBehavior.ANTI_ALIAS)
        if not ring:
            return pic
        return ft.Container(content=pic, width=size, height=size, padding=pad, alignment=ft.Alignment.CENTER,
                            border_radius=size / 2, border=ft.Border.all(1, SEP))

    # ── Castle's deck list ────────────────────────────────────────────────
    async def refresh_global(self, force: bool = False, repaint: bool = True) -> bool:
        if not self.api.token or not (self.user or {}).get("userId"):
            return False
        if not force and self.global_loaded_at and time.monotonic() - self.global_loaded_at < GLOBAL_MAX_AGE:
            return False
        self.global_loaded_at = time.monotonic()     # also on failure: never hammer Castle / loop on rebuilds
        try:
            decks = await asyncio.to_thread(list_recent_decks, self.api, GLOBAL_LIMIT, self.user["userId"])
        except OfflineError:
            self.online = False
            return False
        except Exception as ex:
            self.log(f"error: decks: {ex}")
            return False
        self.online = True
        self.global_decks = decks
        _write_json(self.global_file, decks)
        self.log(f"Deck list refreshed: {len(decks)} decks", 2)
        if repaint:
            self.refresh_body_if("Decks")
        return True

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
                self.log(f"[leave] {ex}", 2)
        self._show()

    def replace_root(self, screen: Screen):
        """Swap the whole stack for one screen (the first-run sign-in / welcome / Decks)."""
        self.stack = [screen]
        self._show()

    def go_home(self):
        """Back to the first (Decks) screen, closing everything above it."""
        while len(self.stack) > 1:
            leaving = self.stack.pop()
            if leaving.on_leave:
                try:
                    leaving.on_leave()
                except Exception as ex:
                    self.log(f"[leave] {ex}", 2)
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

    def refresh_body(self):
        """Rebuild only the screen body (the top bar and its images stay untouched)."""
        self.body.content = self.stack[-1].build(self)
        self.page.update()

    def refresh_body_if(self, screen_title: str):
        if self.stack and self.stack[-1].title == screen_title:
            self._show()

    def refresh_bar(self):
        s = self.stack[-1]
        sig = (id(s), s.sig(self) if s.sig else None)
        if s.sig and sig == self._bar_sig:
            self.page.update()
            return
        self._bar_sig = sig
        self.topbar_host.content = self._bar(s)
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
        self._bar_sig = (id(s), s.sig(self) if s.sig else None)
        self.topbar_host.content = self._bar(s)
        self.body.content = s.build(self)
        self._sync_back()
        self.page.update()

    # ── Android back button ───────────────────────────────────────────────
    def _root_view(self):
        try:
            return self.page.views[0]
        except Exception:
            return None

    def _setup_back(self):
        view = self._root_view()
        if view is not None:
            view.on_confirm_pop = self._on_confirm_pop

    def _sync_back(self):
        """On the Decks screen the back button does what the system does (minimize). On every
        other screen (deck, card, blueprint, profile) it is intercepted and goes back a screen."""
        view = self._root_view()
        if view is not None:
            want = len(self.stack) <= 1
            if view.can_pop != want:
                view.can_pop = want

    async def _on_confirm_pop(self, e):
        view = self._root_view()
        if view is not None:
            await view.confirm_pop(False)          # never let the system close the app from here
        if len(self.stack) > 1:
            self.go_back()                         # screens may intercept it (e.g. "Save changes?")

    # ── logging ───────────────────────────────────────────────────────────
    def log(self, msg: str, level: int = 1):
        """level 0 = errors/warnings, 1 = normal, 2 = verbose. Everything is recorded;
        the Developer settings choose how much the log boxes show."""
        for line in str(msg).splitlines() or [""]:
            low = line.lower()
            lvl = 0 if low.startswith(("error", "warning", "failed")) else level
            self.log_entries.append((time.time(), lvl, line))
            del self.log_entries[:-LOG_KEEP]
            if self.dev_mode and lvl <= self.settings.get("log_level", 1):
                for col in self.log_targets:
                    col.controls.append(minor(line, 11, selectable=True))
                    del col.controls[:-300]
        if self.dev_mode and self.log_targets:
            try:
                self.page.update()
            except Exception:
                pass

    def visible_log_lines(self, limit: int = 120) -> list[str]:
        lvl = self.settings.get("log_level", 1)
        return [l for _t, v, l in self.log_entries if v <= lvl][-limit:]

    def log_view(self, height: Optional[int] = 140) -> ft.Control:
        """Scrolling log text box with a copy button at its top right. Hidden unless
        Developer mode is on."""
        if not self.dev_mode:
            return ft.Container(height=0)
        col = ft.Column([minor(l, 11, selectable=True) for l in self.visible_log_lines()], spacing=2,
                        scroll=ft.ScrollMode.AUTO, auto_scroll=True)
        self.log_targets.append(col)

        async def copy(e):
            await self.clipboard.set("\n".join(c.value for c in col.controls))
            self.toast("Logs copied.")

        return ft.Container(
            ft.Stack([ft.Container(col, left=0, top=0, right=34, bottom=0),
                      ft.Container(icon_button(castle_icon("copy", 18, MINOR1), copy, "Copy logs", size=32),
                                   right=0, top=0)], expand=True),
            height=height, padding=ft.Padding(left=10, top=4, right=4, bottom=8), border=ft.Border.all(1, SEP),
            border_radius=10)

    def log_text(self) -> str:
        """Every recorded line (all levels), timestamped."""
        head = [f"Castletool {theme.APP_VERSION}", f"platform: {getattr(self.page.platform, 'value', self.page.platform)}",
                f"saved: {datetime.now():%Y-%m-%d %H:%M:%S}", ""]
        body = [f"[{datetime.fromtimestamp(t):%Y-%m-%d %H:%M:%S}] [{LEVEL_NAMES.get(v, 'INFO')}] {l}"
                for t, v, l in self.log_entries]
        return "\n".join(head + body) + "\n"

    async def save_logs(self):
        data = self.log_text().encode("utf-8")
        name = f"castletool-logs-{datetime.now():%Y%m%d-%H%M%S}.txt"
        try:
            path = await self.picker.save_file(dialog_title="Save logs", file_name=name,
                                               file_type=ft.FilePickerFileType.CUSTOM, allowed_extensions=["txt"],
                                               src_bytes=data)
            if not path:
                return
            if self.page.platform not in (ft.PagePlatform.ANDROID, ft.PagePlatform.IOS):
                Path(path).write_bytes(data)           # desktop: the picker only returns the path
            self.toast("Logs saved.")
        except Exception as ex:
            fallback = self.root / "logs" / name        # picker unavailable: keep them in app storage
            try:
                fallback.parent.mkdir(parents=True, exist_ok=True)
                fallback.write_bytes(data)
                self.toast(f"Saved to {fallback}")
            except OSError:
                self.toast(f"Couldn't save logs: {ex}", error=True)

    # ── helpers ───────────────────────────────────────────────────────────
    def spawn(self, awaitable):
        async def runner():
            await awaitable
        self.page.run_task(runner)

    async def run(self, fn, *args, report: bool = True):
        """Run blocking `fn` in a worker thread. Errors are logged and toasted;
        returns None on failure or cancel (so make `fn` return something truthy)."""
        def guarded():
            try:
                return fn(*args)
            except tasks.Cancelled:
                return _CANCELLED

        try:
            r = await asyncio.to_thread(guarded)
            return None if r is _CANCELLED else r
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
            self.log(f"error: {ex}")
            if report:
                self.toast(str(ex), error=True)
        return None

    async def work(self, label: str, fn, *args, progress: bool = True, report: bool = True,
                   task: Optional[tasks.Task] = None, view: Optional["ProgressView"] = None):
        """Run `fn(task, *args)` on a worker thread with a cancellable Task. While it runs
        the screen is greyed out and a circular meter shows percentage and a status line.
        Returns fn's result, or None if it failed or was cancelled.
        `task` / `view`: reuse an overlay the caller already put on screen (see open_deck)."""
        task = task or tasks.Task(label, on_message=lambda m: self.log(f"[{label}] {m}", 2))
        if view is None and progress:
            view = ProgressView(self, task)
        self.active_tasks.add(task)
        t0 = time.monotonic()
        self.log(f"{label}: started", 2)
        if view:
            if not view.shown:
                view.show()
            view.update(task)
        try:
            result = await self.run(fn, task, *args, report=report)
        finally:
            self.active_tasks.discard(task)
            if view:
                view.close()
        took = time.monotonic() - t0
        self.last_work_cancelled = task.cancelled
        if task.cancelled:
            self.log(f"{label}: cancelled after {took:.1f}s")
            return None
        self.log(f"{label}: {'done' if result is not None else 'failed'} in {took:.1f}s", 2)
        return result

    def cancel_all_tasks(self):
        for t in list(self.active_tasks):
            t.cancel()

    def toast(self, msg: str, error: bool = False):
        self.page.show_dialog(ft.SnackBar(content=text(msg, 13, MAJOR), bgcolor=BG, duration=3500,
                                          shape=theme.popup_shape(10)))

    def close_dialog(self, dlg: Optional[ft.AlertDialog] = None):
        """Close one specific dialog (or the topmost when none is given)."""
        if dlg is None:
            self.page.pop_dialog()
            return
        try:
            dlg.open = False
            dlg.update()
        except Exception:
            self.page.pop_dialog()

    def dialog(self, heading: Optional[str], content: ft.Control, actions: Optional[list] = None,
               on_close: Optional[Callable] = None, closable: bool = True) -> ft.AlertDialog:
        """Modal with a ✕ at the top left and the title to its right; confirm buttons go in
        `actions`. `on_close` runs when the dialog goes away by any route (✕, tapping outside,
        or after a confirm), so it must be safe to call after success (cancel flags are)."""
        holder: dict = {}
        head: list[ft.Control] = []
        if closable:
            head.append(icon_button(castle_icon("close", 22), lambda e: self.close_dialog(holder["dlg"]), size=36))
        if heading:
            head.append(text(heading, 18, bold=True, expand=True, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS))
        dlg = ft.AlertDialog(
            bgcolor=BG, shape=theme.popup_shape(),
            title=ft.Row(head, spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER) if head else None,
            title_padding=ft.Padding(left=10, top=10, right=16, bottom=0), content=content,
            # Flutter's defaults leave ~24px under the content (and a gap for an empty action row)
            content_padding=ft.Padding(left=24, top=12, right=24, bottom=8 if actions else 16),
            actions=actions or [],
            actions_padding=ft.Padding(left=8, top=0, right=8, bottom=8) if actions else ft.Padding.all(0),
            on_dismiss=(lambda e: on_close()) if on_close else None)
        holder["dlg"] = dlg
        self.page.show_dialog(dlg)
        return dlg

    def confirm(self, heading: str, body: str, ok_label: str, on_ok):
        async def _ok(e):
            self.page.pop_dialog()
            r = on_ok()
            if inspect.isawaitable(r):
                await r

        self.dialog(heading, text(body, 14, MINOR1) if body else ft.Container(height=0), [
            ft.TextButton(content=text(ok_label, 14, MAJOR, bold=True), on_click=_ok)])

    def info(self, heading: str, message: str):
        self.dialog(heading, text(message, 14, MINOR1), [
            ft.TextButton(content=text("OK", 14, MAJOR, bold=True), on_click=lambda e: self.page.pop_dialog())],
            closable=False)

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
        await self.refresh_global(force=True, repaint=False)

    async def sign_in_password(self, username: str, password: str) -> bool:
        self.login_cancel = False
        user = await self.run(lambda: self.api.login_password(username.strip(), password))
        if self.login_cancel:                      # cancelled while waiting: drop the result
            self.api.token = None
            return False
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
        if not user or self.login_cancel:
            self.api.token = None
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

    def fresh_clone(self, deck_id: str, task: Optional[tasks.Task] = None) -> Path:
        """Clone into a scratch folder, then swap it in: a failed or cancelled clone never
        destroys the copy already on the device."""
        scratch = self.decks_dir / ".incoming" / deck_id          # one scratch dir per deck, never shared
        shutil.rmtree(scratch, ignore_errors=True)
        try:
            with tasks.use(task):
                clone_deck(self.api, deck_id, scratch, self.log)
                tasks.say("Finishing up", 0.98)
            target = self.decks_dir / deck_id
            shutil.rmtree(target, ignore_errors=True)
            shutil.move(str(scratch / deck_id), str(target))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
            try:
                scratch.parent.rmdir()                            # only succeeds when no other clone is running
            except OSError:
                pass
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
        path = await self.work("Cloning deck", lambda task: self.fresh_clone(deck_id, task))
        if path:
            self.global_loaded_at = 0.0
            self.refresh_body_if("Decks")
        return path

    async def open_deck(self, deck_id: str, local_dir: Optional[Path]):
        """Opening a deck re-clones it from Castle, unless that would lose unsaved
        edits or Castle can't be reached (then the copy on the device is used)."""
        from screens_library import deck_screen
        if deck_id in self.opening:                    # a second tap while the first is still working
            return
        has_local = bool(local_dir and local_dir.exists())
        if has_local and deck_has_unsaved(local_dir):
            self.push(deck_screen(self, local_dir))
            return
        self.opening.add(deck_id)
        # Cover the screen at once (placeholder text until real progress arrives) so a second
        # tap can't start a second clone while the network checks below are still running.
        task = tasks.Task("Opening deck", on_message=lambda m: self.log(f"[Opening deck] {m}", 2))
        view = ProgressView(self, task)
        view.show()
        task.update(None, "Loading…", force=True)
        try:
            if self.api.token and (self.online or not has_local):
                if not self.online:
                    await self.refresh_profile()
                if task.cancelled:
                    return
                if self.online:
                    def clone(t):
                        try:
                            return self.fresh_clone(deck_id, t)
                        except DeckDeleted:
                            return _DELETED
                    path = await self.work("Opening deck", clone, task=task, view=view)
                    if path is _DELETED:
                        view.close()
                        await self.deck_was_deleted(deck_id, local_dir)
                        return
                    if path:
                        view.close()
                        self.push(deck_screen(self, path))
                        return
                    if not has_local or self.last_work_cancelled:
                        return
            view.close()
            if has_local:
                self.push(deck_screen(self, local_dir))
            elif not self.api.token:
                self.toast("Sign in first.", error=True)
            else:
                self.toast("No Wi-Fi.", error=True)
        finally:
            view.close()
            self.opening.discard(deck_id)

    async def deck_was_deleted(self, deck_id: str, local_dir: Optional[Path]):
        """Castle won't show us a deck we know: it was deleted there. Drop the local copy (saving it
        would only fail) and the cached list entry, and say so."""
        self.log(f"Deck {deck_id} is gone from Castle; removing the local copy")
        self.global_decks = [d for d in self.global_decks if d.get("deckId") != deck_id]
        _write_json(self.global_file, self.global_decks)
        if local_dir and Path(local_dir).exists():
            await self.delete_deck(Path(local_dir))
        else:
            self.clear_sessions(deck_id)
            self.refresh_body_if("Decks")
        self.dialog("This deck was deleted", ft.Container(height=0), [
            ft.TextButton(content=text("OK", 14, MAJOR, bold=True), on_click=lambda e: self.page.pop_dialog())],
            closable=False)

    def ask_delete_deck(self, deck_dir: Path, name: str):
        unsaved = deck_has_unsaved(deck_dir)
        body = "Only the copy on this device is removed." + (" Unsaved changes will be lost." if unsaved else "")
        self.confirm(f"Delete \"{name}\"?", body, "Delete", lambda: self.delete_deck(deck_dir))

    async def delete_deck(self, deck_dir: Path):
        try:
            await asyncio.to_thread(shutil.rmtree, deck_dir)
            await asyncio.to_thread(self.icons.delete_deck, deck_dir)
        except OSError as ex:
            self.log(f"error: {ex}")
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

        def do(task):
            with tasks.use(task):
                push_card(self.api, ctx.deck_dir, ctx.card_id, self.log)
            return True

        ok = await self.work("Saving card", do)
        ctx.saving = False
        if ok:
            ctx.session.mark_saved()
        self.refresh_bar()
        return bool(ok)


# ── shared footer ───────────────────────────────────────────────────────────

def footer_links(a: App) -> ft.Control:
    """Colophon / Terms / Privacy / Github, then the unofficial-app disclaimer."""
    def link(label: str, url: str):
        async def go(e):
            await a.launcher.launch_url(url)
        return ft.Container(text(label, 13, MAJOR, medium=True), padding=ft.Padding.symmetric(vertical=8, horizontal=10),
                            ink=True, border_radius=8, on_click=go)
    return ft.Column([
        ft.Row([link(l, u) for l, u in theme.CASTLE_LINKS], alignment=ft.MainAxisAlignment.CENTER, wrap=True),
        minor(theme.DISCLAIMER, 11, text_align=ft.TextAlign.CENTER),
    ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=6)


# ── first-run screen: sign in ───────────────────────────────────────────────

def signin_screen(app: App) -> Screen:
    """Shown instead of Decks until the first sign-in: no top bar, nothing else reachable."""
    user_f = ft.TextField(hint_text="Username or email", dense=True, border_color=SEP, width=280,
                          text_style=ft.TextStyle(font_family=theme.BODY, color=MAJOR))
    pass_f = ft.TextField(hint_text="Password", password=True, can_reveal_password=True, dense=True,
                          border_color=SEP, width=280, text_style=ft.TextStyle(font_family=theme.BODY, color=MAJOR))
    state = {"busy": False}
    form = ft.Column(spacing=10, horizontal_alignment=ft.CrossAxisAlignment.CENTER)

    def paint():
        form.controls = [
            user_f, pass_f,
            pill_button("Sign in", on_sign_in, filled=True, busy=state["busy"], width=280),
            pill_button("Sign in with Castle", on_sign_in_browser, disabled=state["busy"], width=280),
        ] + ([pill_button("Cancel", on_cancel, width=280)] if state["busy"] else [])
        app.page.update()

    def on_cancel(e):
        app.login_cancel = True
        state["busy"] = False
        paint()

    async def finish(ok: bool):
        state["busy"] = False
        pass_f.value = ""
        if ok:
            app.set_setting("onboarded", True)          # these first-run screens never come back
            from screens_library import home_screen
            app.replace_root(home_screen(app))
        else:
            paint()

    async def on_sign_in(e):
        if not user_f.value or not pass_f.value:
            return
        state["busy"] = True
        paint()
        await finish(await app.sign_in_password(user_f.value, pass_f.value))

    async def on_sign_in_browser(e):
        state["busy"] = True
        paint()
        await finish(await app.sign_in_browser())

    def build(a: App) -> ft.Control:
        paint()
        # Scroll lives on the inner column: expand inside a scrolling parent gets unbounded height and
        # collapses to the top. Inner expand + center keeps the form centered; scroll covers the keyboard.
        return ft.Column([
            ft.Column([title("Welcome to Castletool!", 28, text_align=ft.TextAlign.CENTER), form],
                      spacing=24, horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                      alignment=ft.MainAxisAlignment.CENTER, scroll=ft.ScrollMode.AUTO, expand=True),
            ft.Container(footer_links(a), padding=ft.Padding(left=16, right=16, top=8, bottom=16)),
        ], spacing=0, horizontal_alignment=ft.CrossAxisAlignment.CENTER, expand=True)

    return Screen(build=build, title="Sign in", bar=lambda a: ft.Container(height=0))


# ── profile screen ─────────────────────────────────────────────────────────

def profile_screen(app: App) -> Screen:
    name = title("", 22, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
    status = minor("", 13)
    host = ft.Column(spacing=10, horizontal_alignment=ft.CrossAxisAlignment.START)
    form = ft.Column(spacing=10, horizontal_alignment=ft.CrossAxisAlignment.CENTER)
    user_f = ft.TextField(hint_text="Username or email", dense=True, border_color=SEP, width=280,
                          text_style=ft.TextStyle(font_family=theme.BODY, color=MAJOR))
    pass_f = ft.TextField(hint_text="Password", password=True, can_reveal_password=True, dense=True,
                          border_color=SEP, width=280, text_style=ft.TextStyle(font_family=theme.BODY, color=MAJOR))
    state = {"busy": False}

    def paint():
        if app.signed_in:
            name.value = (app.user or {}).get("username") or ""
            status.value = "" if app.online else "No Wi-Fi"
            host.controls = [pill_button("Sign out", on_sign_out, width=120)]
            form.controls = []
        else:
            name.value = "Not signed in"
            status.value = ""
            host.controls = []
            form.controls = [
                user_f, pass_f,
                pill_button("Sign in", on_sign_in, filled=True, busy=state["busy"], width=280),
                pill_button("Sign in with Castle", on_sign_in_browser, disabled=state["busy"], width=280),
            ] + ([pill_button("Cancel", on_cancel_login, width=280)] if state["busy"] else [])
        app.page.update()

    def on_cancel_login(e):
        app.login_cancel = True
        state["busy"] = False
        paint()

    async def finish_login(ok: bool):
        state["busy"] = False
        pass_f.value = ""
        if ok:
            app.go_home()                       # signed in: straight back to the Decks tabs
        else:
            paint()

    async def on_sign_in(e):
        if not user_f.value or not pass_f.value:
            return
        state["busy"] = True
        paint()
        await finish_login(await app.sign_in_password(user_f.value, pass_f.value))

    async def on_sign_in_browser(e):
        state["busy"] = True
        paint()
        await finish_login(await app.sign_in_browser())

    async def on_sign_out(e):
        await app.sign_out()
        app.refresh()

    async def on_retry(e):
        if app.signed_in and not app.online:
            await app.refresh_profile()
            app.refresh()

    def dev_section(a: App) -> list[ft.Control]:
        def toggle(e):
            a.set_setting("dev_mode", bool(e.control.value))
            a.refresh_body()

        sw = ft.Switch(value=a.dev_mode, on_change=toggle, active_color=BG, active_track_color=MAJOR,
                       inactive_thumb_color=MINOR1, inactive_track_color=BG, track_outline_color=SEP)
        out: list[ft.Control] = [ft.Row([text("Developer mode", 14, expand=True), sw])]
        if not a.dev_mode:
            return out

        def level(e):
            a.set_setting("log_level", int(e.control.value))
            a.refresh_body()

        out.append(ft.Dropdown(
            value=str(a.settings.get("log_level", 1)), label="Log verbosity", dense=True, border_color=SEP,
            options=[ft.dropdown.Option(key=str(k), text=t) for k, t in LOG_LEVELS], on_select=level,
            text_style=ft.TextStyle(font_family=theme.BODY, color=MAJOR, size=14), width=260))

        async def save(e):
            await a.save_logs()

        def clear(e):
            a.log_entries.clear()
            a.refresh_body()

        out.append(ft.Row([pill_button("Save all logs", save), pill_button("Clear", clear)], spacing=8, wrap=True))
        out.append(minor("Log", 11))
        out.append(a.log_view(240))
        return out

    def build(a: App) -> ft.Control:
        paint()
        head = ft.Row([
            ft.GestureDetector(a.avatar(88, ring=False), on_tap=on_retry),
            ft.Column([name, status, host], spacing=6, expand=True, horizontal_alignment=ft.CrossAxisAlignment.START),
        ], spacing=16, vertical_alignment=ft.CrossAxisAlignment.START)
        return ft.Column([
            ft.Container(ft.Column([head, form], spacing=14, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                         padding=ft.Padding(left=16, right=16, top=20, bottom=16)),
            theme.separator(),
            ft.Container(ft.Column(dev_section(a), spacing=10), padding=14),
            theme.separator(),
            ft.Container(footer_links(a), padding=ft.Padding(left=16, right=16, top=12, bottom=28)),
        ], scroll=ft.ScrollMode.AUTO, spacing=0)

    return Screen(title="Profile", build=build)
