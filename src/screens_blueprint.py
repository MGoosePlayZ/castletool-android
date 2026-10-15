"""Blueprint screen: header + Media / Music / Audio sections."""
from __future__ import annotations

import asyncio
import base64
import re
from pathlib import Path
from typing import Optional

import flet as ft

import blueprint_ops as ops
import castletool_core as ct
import drawing
import live_synth
import synth
import tasks
import theme
from app import Screen
from cardbar import CardContext, card_bar
from screens_library import card_bg
from theme import BG, MAJOR, MINOR1, SEP, castle_icon, minor, text
from widgets import ColorField, NumberBox, icon_button, info_note, pill_button

ARCHIVES = {"zip", "tar", "gz", "tgz", "bz2", "xz", "tbz2", "txz"}
MEDIA_EXTS = sorted({e.lstrip(".") for e in (ct.IMAGE_EXTS | ct.VIDEO_EXTS | ct.VECTOR_EXTS | ct.FONT_EXTS)} | ARCHIVES)
AUDIO_EXTS = sorted({e.lstrip(".") for e in ct.AUDIO_EXTS} | ARCHIVES)
FIELD_TEXT = ft.TextStyle(font_family=theme.BODY, color=MAJOR, size=14)
BLANK_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABpfZFQAAAAABJRU5ErkJggg==")


def blueprint_screen(app, ctx: CardContext, bp_path: Path) -> Screen:
    view = BlueprintView(app, ctx, bp_path)
    return Screen(title="", build=lambda a: view.build(), bar=lambda a: card_bar(a, ctx, "blueprint"),
                  on_leave=view.dispose, title_font=False, sig=lambda a: ctx.bar_sig())


def _clock(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60}:{s % 60:02d}"


class BlueprintView:
    def __init__(self, app, ctx: CardContext, bp_path: Path):
        self.app, self.ctx, self.bp_path = app, ctx, bp_path
        self.card_dir = ctx.card_dir
        self.bg = card_bg(self.card_dir)
        self.tab = "media"
        self.alive = True
        self.busy = False
        self.anim_token = 0
        self.engine: Optional[live_synth.LiveSynth] = None
        self._header: Optional[ft.Control] = None      # built once; reused across tab switches (no flicker)
        self._header_key = None
        self.icon_task: Optional[tasks.Task] = None
        self.reload()
        self.root = ft.Column([], spacing=0, scroll=ft.ScrollMode.AUTO, expand=True)

    # ── state ─────────────────────────────────────────────────────────────
    def reload(self) -> bool:
        if not self.bp_path.exists():
            return False
        self.bp = ops.load_blueprint(self.bp_path)
        self.name = ops.blueprint_title(self.bp, self.bp_path.stem)
        self.bg = card_bg(self.card_dir)
        st = self.bp_path.stat()
        key = (st.st_mtime_ns, st.st_size)
        if key != self._header_key:                    # blueprint changed on disk: header must be rebuilt
            self._header_key, self._header = key, None
        return True

    def dispose(self):
        self.alive = False
        self.anim_token += 1
        if self.icon_task:
            self.icon_task.cancel()
        self._close_engine()

    def build(self) -> ft.Control:
        if not self.reload():            # e.g. undo removed this blueprint
            self.app.page.run_task(self._leave)
            return ft.Container()
        self.render(update=False)
        return self.root

    async def _leave(self):
        self.app.pop()

    def render(self, update: bool = True):
        self.anim_token += 1                       # stops any running preview animation / song
        self._close_engine()
        self.app.log_targets.clear()
        panel = {"media": self.media_panel, "music": self.music_panel, "audio": self.audio_panel}[self.tab]()
        self.root.controls = [self.header(), self.tabs(), ft.Container(panel, padding=14)]
        if update:
            self.app.page.update()

    # ── header / tabs ─────────────────────────────────────────────────────
    def header(self) -> ft.Control:
        """Small 128px preview icon (from the local icon store) + the name, on the plain background."""
        if self._header is not None:
            return self._header
        d2 = drawing.get_drawing2(self.bp)
        holder = ft.Container(width=76, height=76, alignment=ft.Alignment.CENTER)
        if d2 and drawing.frame_count(d2):
            png = self.app.icons.png(self.card_dir, self.bp_path)
            if png:
                holder.content = ft.Image(src=png, width=72, height=72, fit=ft.BoxFit.CONTAIN, gapless_playback=True)
            else:
                self._render_icon_later(holder)
        else:
            holder.content = castle_icon("brick", 28, MINOR1)
        self._header = ft.Container(ft.Row([
            holder, text(self.name, 20, bold=True, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS, expand=True)],
            spacing=14, vertical_alignment=ft.CrossAxisAlignment.CENTER), padding=14)
        return self._header

    def _render_icon_later(self, holder: ft.Container):
        task = tasks.Task("icon")
        self.icon_task = task

        def make():
            try:
                with tasks.use(task):
                    return self.app.icons.render(self.card_dir, self.bp_path)
            except tasks.Cancelled:
                return None

        async def go():
            png = await asyncio.to_thread(make)
            if png and not task.cancelled:
                holder.content = ft.Image(src=png, width=72, height=72, fit=ft.BoxFit.CONTAIN, gapless_playback=True)
                self.app.page.update()
        self.app.page.run_task(go)

    def tabs(self) -> ft.Control:
        def tab(key: str, label: str, icon: str):
            active = self.tab == key

            def go(e, k=key):
                if self.busy:
                    return
                self.tab = k
                self.render()
            return ft.Container(
                ft.Column([castle_icon(icon, 24, MAJOR if active else MINOR1),
                           text(label, 12, MAJOR if active else MINOR1, bold=active)],
                          spacing=2, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                expand=True, padding=ft.Padding.symmetric(vertical=8), ink=True, on_click=go,
                border=ft.Border(bottom=ft.BorderSide(2 if active else 1, MAJOR if active else SEP)))
        return ft.Row([tab("media", "Media", "pencil"), tab("music", "Music", "music"), tab("audio", "Audio", "unmuted")],
                      spacing=0)

    def log_box(self) -> ft.Control:
        if not self.app.dev_mode:                      # logs are a Developer-mode feature
            return ft.Container(height=0)
        return ft.Container(ft.Column([minor("Activity", 11), self.app.log_view(110)], spacing=4),
                            margin=ft.Margin.only(top=14))

    async def after_edit(self, result, message: str):
        """Common tail of an edit: reload, toast, redraw."""
        self.busy = False
        if result:
            self.reload()
            self.app.toast(message)
        self.render()

    # ── MEDIA ─────────────────────────────────────────────────────────────
    def media_panel(self) -> ft.Control:
        out: list[ft.Control] = []
        d2 = drawing.get_drawing2(self.bp)
        if d2 and drawing.frame_count(d2):
            out.append(self.preview_block(d2))
        else:
            out.append(ft.Container(castle_icon("pencil", 30, MINOR1), height=140, alignment=ft.Alignment.CENTER))
        out.append(ft.Container(pill_button("Upload", self.on_pick_media, castle_icon("upload", 18, BG), filled=True),
                                margin=ft.Margin.only(top=14)))
        out.append(self.log_box())
        return ft.Column(out, spacing=6)

    def preview_block(self, d2: dict) -> ft.Control:
        info = drawing.drawing_info(d2)
        n = info["frames"]
        img = ft.Image(src=drawing.render_png(d2, 240, None), width=240, height=240, fit=ft.BoxFit.CONTAIN,
                       gapless_playback=True)
        controls: list[ft.Control] = [ft.Container(img, alignment=ft.Alignment.CENTER, height=250)]
        if n > 1:
            slider = ft.Slider(min=1, max=n, divisions=n - 1, value=1, expand=True, active_color=MAJOR,
                               inactive_color=SEP, label="{value}")
            label = minor(f"1 / {n}", 12, width=56, text_align=ft.TextAlign.RIGHT)
            state = {"playing": info["play_mode"] != "still", "frames": None, "i": 0}
            token = self.anim_token
            play_btn = icon_button(castle_icon("pause" if state["playing"] else "play", 22), None, size=36)

            def show(i: int):
                if state["frames"]:
                    img.src = state["frames"][i]
                    label.value = f"{i + 1} / {n}"
                    slider.value = i + 1
                    self.app.page.update()

            def on_slide(e):
                state["i"] = int(e.control.value) - 1
                show(state["i"])

            def toggle(e):
                state["playing"] = not state["playing"]
                play_btn.content = castle_icon("pause" if state["playing"] else "play", 22)
                self.app.page.update()

            slider.on_change = on_slide
            play_btn.on_click = toggle

            async def run():
                frames = await asyncio.to_thread(drawing.render_animation, d2, 240, None, 240,
                                                 lambda: token != self.anim_token)
                if token != self.anim_token or not frames:
                    return
                state["frames"] = frames
                delay = 1.0 / max(1.0, min(60.0, float(info["fps"] or 4)))
                while token == self.anim_token and self.alive:
                    if state["playing"]:
                        state["i"] = (state["i"] + 1) % len(frames)
                        show(state["i"])
                    await asyncio.sleep(delay)

            self.app.page.run_task(run)
            controls.append(ft.Row([play_btn, slider, label], vertical_alignment=ft.CrossAxisAlignment.CENTER))
        return ft.Column(controls, spacing=8)

    async def on_pick_media(self, e):
        path = await self.app.pick(MEDIA_EXTS, "Choose image, video, SVG, font or archive")
        if not path:
            return
        info = await self.app.run(lambda: ops.inspect_media(path, log=self.app.log))
        if not info:
            return
        if info["category"] == "audio":
            self.app.toast("That's audio. Use the Audio tab.", error=True)
            return
        if info["unsupported"]:
            self.app.toast(info["unsupported"], error=True)
            return
        ImportDialog(self, path, info).open()

    # ── MUSIC ─────────────────────────────────────────────────────────────
    def _close_engine(self):
        eng, self.engine = self.engine, None
        if eng is not None:
            try:
                eng.close()
            except Exception:
                pass

    def music_panel(self) -> ft.Control:
        out: list[ft.Control] = []
        music = ops.components(self.bp).get("Music")
        if music:
            bpm = ops.card_tempo(self.card_dir)
            st = synth.music_stats(music, bpm)
            out.append(minor(f"{st['tracks']} track{'s' if st['tracks'] != 1 else ''} · {st['notes']:,} notes · "
                             f"{bpm:g} BPM", 12))
            out.append(self.player_controls(music, bpm))
        else:
            out.append(ft.Container(castle_icon("music", 30, MINOR1), height=120, alignment=ft.Alignment.CENTER))
        if ct.HAS_MIDO:
            out.append(ft.Container(pill_button("Upload", self.on_pick_midi, castle_icon("upload", 18, BG), filled=True,
                                                busy=self.busy), margin=ft.Margin.only(top=14)))
        out.append(self.log_box())
        return ft.Column(out, spacing=8)

    def player_controls(self, music: dict, bpm: float) -> ft.Control:
        """Live playback: oscillators are mixed as the song plays and streamed to the speaker."""
        android = self.app.page.platform == ft.PagePlatform.ANDROID
        try:
            eng = live_synth.LiveSynth(music, bpm, lambda: live_synth.make_sink(android))
        except Exception as ex:
            return minor(str(ex), 12)
        if not eng.notes:
            return minor("No notes", 12)
        self.engine = eng
        eng.loop = False
        dur_ms = max(1, int(eng.duration * 1000))
        state = {"dragging": False, "icon": "play_music"}

        play_btn = icon_button(castle_icon("play_music", 26), None, size=44)
        slider = ft.Slider(min=0, max=dur_ms, value=0, expand=True, active_color=MAJOR, inactive_color=SEP)
        # fixed-width time slots: DM Sans digits aren't tabular, so the slider must not jiggle
        elapsed = ft.Container(text("0:00", 12, MINOR1, text_align=ft.TextAlign.RIGHT), width=40,
                               alignment=ft.Alignment.CENTER_RIGHT)
        length = ft.Container(text(_clock(eng.duration), 12, MINOR1, text_align=ft.TextAlign.LEFT), width=40,
                              alignment=ft.Alignment.CENTER_LEFT)

        def paint_icon():
            icon = "stop" if eng.playing else "play_music"
            if icon != state["icon"]:
                state["icon"] = icon
                play_btn.content = castle_icon(icon, 26)

        def toggle(e):
            if eng.playing:
                eng.stop()                              # stop = back to the start
                slider.value = 0
                elapsed.content.value = "0:00"
            else:
                eng.play()
            paint_icon()
            self.app.page.update()

        def drag_start(e):
            state["dragging"] = True

        def drag_end(e):
            state["dragging"] = False
            eng.seek(float(e.control.value) / 1000)

        play_btn.on_click = toggle
        slider.on_change_start, slider.on_change_end = drag_start, drag_end
        token = self.anim_token

        async def tick():
            shown_error = False
            while token == self.anim_token and self.alive:
                if eng.error and not shown_error:
                    shown_error = True
                    self.app.toast(eng.error, error=True)
                if not state["dragging"]:
                    slider.value = min(eng.position * 1000, dur_ms)
                elapsed.content.value = _clock(eng.position)
                paint_icon()
                self.app.page.update()
                await asyncio.sleep(0.1)

        self.app.page.run_task(tick)
        row = ft.Row([play_btn, elapsed, slider, length], spacing=2, vertical_alignment=ft.CrossAxisAlignment.CENTER)
        if eng.warnings:
            return ft.Column([row, info_note("\n".join(eng.warnings))], spacing=6)
        return row

    async def on_pick_midi(self, e):
        path = await self.app.pick(["mid", "midi"], "Choose a MIDI file")
        if not path:
            return
        res = await self.ctx.edit_async("Importing MIDI", lambda task: ops.import_midi(
            self.bp_path, self.card_dir, path, log=self.app.log, task=task))
        await self.after_edit(res, "MIDI imported.")

    # ── AUDIO ─────────────────────────────────────────────────────────────
    def audio_panel(self) -> ft.Control:
        out: list[ft.Control] = []
        urls = ops.audio_urls(self.bp)
        if urls:
            for i, u in enumerate(urls, 1):
                ident = re.sub(r"\?.*$", "", u).rstrip("/").split("/")[-1]
                out.append(ft.Container(
                    ft.Row([minor(f"{i}", 12, width=20), text(ident, 12, selectable=True, expand=True)]),
                    padding=ft.Padding.symmetric(vertical=6, horizontal=10), border_radius=8,
                    border=ft.Border.all(1, SEP)))
        else:
            out.append(ft.Container(castle_icon("unmuted", 30, MINOR1), height=120, alignment=ft.Alignment.CENTER))
        out.append(ft.Container(pill_button("Upload", self.on_pick_audio, castle_icon("upload", 18, BG), filled=True,
                                            busy=self.busy), margin=ft.Margin.only(top=14)))
        out.append(info_note("Audio is split into 30 second segments"))
        out.append(self.log_box())
        return ft.Column(out, spacing=8)

    async def on_pick_audio(self, e):
        if not await self.app.ensure_online():
            return
        path = await self.app.pick(AUDIO_EXTS, "Choose audio or an archive of audio")
        if not path:
            return
        token = self.app.api.token
        res = await self.ctx.edit_async("Uploading audio", lambda task: ops.import_audio(
            self.bp_path, self.card_dir, path, log=self.app.log, token=token, task=task))
        await self.after_edit(res, "Audio added." if not res or len(res) == 1 else f"Added {len(res)} actors.")


# ── import modal with live preview ─────────────────────────────────────────

class ImportDialog:
    """Settings for a picked media file, with a preview that follows the settings."""

    def __init__(self, view: BlueprintView, path: Path, info: dict):
        self.view, self.app, self.path, self.info = view, view.app, path, info
        self.cat = info["category"]
        self.vals: dict = {"charsets": {"ASCII"}, "quantize_on": info["size"] > 500_000}
        self.busy = False
        self.token = 0
        self.frame = 0                       # which frame the preview shows (0-based)
        self.pv_task: Optional[tasks.Task] = None
        self.holder: dict = {}
        self.preview_img = ft.Image(width=200, height=200, fit=ft.BoxFit.CONTAIN, gapless_playback=True, src=BLANK_PNG)
        self.preview_box = ft.Container(self.preview_img, width=220, height=220,
                                        alignment=ft.Alignment.CENTER, opacity=0.4)   # plain: no card behind it
        self.frame_label = minor("", 12, width=64, text_align=ft.TextAlign.RIGHT)
        self.frame_slider = ft.Slider(min=1, max=2, divisions=1, value=1, expand=True, active_color=MAJOR,
                                      inactive_color=SEP, on_change=self._frame_drag, on_change_end=self._frame_set)
        self.frame_row = ft.Row([self.frame_slider, self.frame_label], visible=False,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER)
        self.options = ft.Column([], spacing=10)
        self.import_host = ft.Container()

    # preview frame picker -------------------------------------------------
    def frame_total(self) -> int:
        v = self.vals
        try:
            every = max(1, int(float(v.get("every", 1) or 1)))
        except ValueError:
            every = 1
        if self.cat in ("font", "audio"):
            return 1
        return ops.preview_frame_count(self.info, {"every": every, "layout": v.get("layout", "frames")})

    def sync_frame_row(self):
        n = self.frame_total()
        self.frame = min(self.frame, n - 1)
        self.frame_row.visible = n > 1
        if n > 1:
            self.frame_slider.max = n
            self.frame_slider.divisions = n - 1
            self.frame_slider.value = self.frame + 1
            self.frame_label.value = f"{self.frame + 1} / {n}"

    def _frame_drag(self, e):
        self.frame_label.value = f"{int(e.control.value)} / {self.frame_total()}"
        self.app.page.update()

    def _frame_set(self, e):
        self.frame = int(e.control.value) - 1
        self.schedule_preview()

    # widgets -------------------------------------------------------------
    def set(self, key: str, value):
        self.vals[key] = value
        if key == "every":
            self.sync_frame_row()
        self.schedule_preview()

    def num(self, key: str, label: str, default, **kw) -> ft.Control:
        return NumberBox(label, self.vals.get(key, default), lambda v, k=key: self.set(k, v), width=190, **kw).control

    def switch(self, key: str, label: str, default: bool, rebuild: bool = False) -> ft.Control:
        def ch(e, k=key):
            self.vals[k] = e.control.value
            if rebuild:
                self.rebuild()
            self.schedule_preview()
        return ft.Row([ft.Switch(value=bool(self.vals.get(key, default)), on_change=ch, active_color=BG,
                                 active_track_color=MAJOR, inactive_thumb_color=MINOR1, inactive_track_color=BG,
                                 track_outline_color=SEP),
                       text(label, 14, expand=True)], spacing=6)

    def choice(self, key: str, label: str, options: list, default: str) -> ft.Control:
        def ch(e, k=key):
            self.vals[k] = e.control.value
            self.rebuild()
            self.schedule_preview()
        return ft.Dropdown(value=self.vals.get(key, default), label=label, dense=True, border_color=SEP,
                           options=[ft.dropdown.Option(key=v, text=t) for v, t in options], on_select=ch,
                           text_style=FIELD_TEXT, width=260)

    def color(self, key: str, default: str, enabled: bool = True) -> ft.Control:
        return ColorField(self.app, self.vals.get(key, default), lambda v, k=key: self.set(k, v),
                          enabled=enabled).control

    # layout --------------------------------------------------------------
    def option_controls(self) -> list[ft.Control]:
        info, cat, c = self.info, self.cat, []
        if cat == "font":
            return self.font_options()
        multi = info["count"] > 1
        vector = cat == "vector"
        bitmap_like = not vector or self.vals.get("svg_style") == "bitmap"
        if vector:
            c.append(self.choice("svg_style", "Draw as", [("vector", "Vector"), ("bitmap", "Bitmap")], "vector"))
        c.append(self.num("scale" if bitmap_like else "svg_scale", "Scale", 1, step=0.1, minimum=0.01, decimals=2))
        if not bitmap_like:
            c.append(self.num("steps", "Curve steps", 16, step=1, minimum=2, maximum=64))
        if bitmap_like:
            c.append(self.switch("smooth", "Smooth", False))
        if cat in ("animation", "video"):
            c.append(self.num("every", "Every Nth frame", 1, step=1, minimum=1))
        if multi and cat in ("image", "vector"):
            c.append(self.choice("layout", "Several files", [("frames", "Frames of one actor"),
                                                             ("separate", "Separate actors")], "frames"))
            c.append(info_note("Every file becomes a frame of this blueprint." if
                               self.vals.get("layout", "frames") == "frames" else
                               "Every file becomes its own blueprint."))
            if self.vals.get("layout", "frames") == "frames":
                c.append(self.num("fps", "Frames per second", 4, step=1, minimum=1, maximum=60, decimals=1))
            if bitmap_like and cat == "image":
                c.append(self.switch("no_scale", "Keep original sizes", False))
        if vector:
            on = bool(self.vals.get("color_on", False))
            c.append(self.switch("color_on", "Override color", False, rebuild=True))
            c.append(self.color("color", "#ffffff", enabled=on))
        if bitmap_like:
            c.append(self.switch("quantize_on", "Reduce colors", self.vals.get("quantize_on", False), rebuild=True))
            if self.vals.get("quantize_on"):
                c.append(self.num("quantize_colors", "Colors", 256, step=8, minimum=1, maximum=256))
        if cat == "video" and info["has_audio"]:
            c.append(self.switch("want_audio", "Sound", True))
        return c

    def font_options(self) -> list[ft.Control]:
        c: list[ft.Control] = []
        chosen: set = self.vals["charsets"]

        def toggle(e, name: str):
            (chosen.add if e.control.value else chosen.discard)(name)
            self.schedule_preview()

        c.append(ft.Column([
            ft.Checkbox(label=name, value=name in chosen, on_change=lambda e, n=name: toggle(e, n), active_color=MAJOR,
                        check_color=BG, label_style=ft.TextStyle(font_family=theme.BODY, size=13))
            for name in ops.font_charset_names()], spacing=0))
        c.append(ft.TextField(value=self.vals.get("custom", ""), label="Characters", dense=True, border_color=SEP,
                              text_style=FIELD_TEXT, width=260, on_change=lambda e: self.set("custom", e.control.value)))
        c.append(self.choice("font_style", "Draw as", [("vector", "Vector"), ("bitmap", "Bitmap")], "vector"))
        if self.vals.get("font_style") == "bitmap":
            c.append(self.num("px", "Pixels per em", 64, step=8, minimum=4, maximum=512))
            c.append(self.switch("smooth", "Smooth", False))
        else:
            c.append(self.num("font_scale", "Scale", 1, step=0.1, minimum=0.01, decimals=2))
            c.append(self.num("steps", "Curve steps", 16, step=1, minimum=2, maximum=64))
        c.append(self.color("color", "#ffffff"))
        return c

    def rebuild(self):
        self.options.controls = self.option_controls()
        self.sync_frame_row()
        self.app.page.update()

    # options → ops arguments ---------------------------------------------
    def collect(self) -> tuple[str, dict]:
        v, cat = self.vals, self.cat

        def num(key, default, cast=float):
            raw = str(v.get(key, default)).strip()
            try:
                return cast(raw)
            except ValueError:
                raise ops.OpError("Enter a number.")

        if cat == "font":
            return "font", {
                "charsets": sorted(v.get("charsets") or []), "custom": v.get("custom", ""),
                "bitmap": v.get("font_style") == "bitmap", "scale": num("font_scale", 1), "steps": num("steps", 16, int),
                "px": num("px", 64, int), "smooth": bool(v.get("smooth")),
                "color": ops.parse_color(v.get("color", "#ffffff")),
            }
        quant = num("quantize_colors", 256, int) if v.get("quantize_on") else 0
        if quant and not 1 <= quant <= 256:
            raise ops.OpError("Colors must be between 1 and 256.")
        return "media", {
            "layout": v.get("layout", "frames"), "no_scale": bool(v.get("no_scale")),
            "scale": num("scale", 1), "smooth": bool(v.get("smooth")), "every": num("every", 1, int),
            "quantize": quant, "svg_style": v.get("svg_style", "vector"), "svg_scale": num("svg_scale", 1),
            "steps": num("steps", 16, int),
            "color": ops.parse_color(v.get("color", "#ffffff")) if v.get("color_on") else None,
            "fps": num("fps", 4), "want_audio": bool(v.get("want_audio", True)),
        }

    # preview -------------------------------------------------------------
    def cancel_preview(self):
        if self.pv_task:
            self.pv_task.cancel()

    def schedule_preview(self):
        self.token += 1
        self.cancel_preview()                           # a newer setting makes the running render pointless
        self.app.page.run_task(self._preview, self.token)

    async def _preview(self, token: int):
        await asyncio.sleep(0.25)                       # debounce typing / wheel scrolling
        if token != self.token:
            return
        try:
            kind, opts = self.collect()
        except ops.OpError:
            return
        self.preview_box.opacity = 0.4
        self.app.page.update()
        task = tasks.Task("preview")
        self.pv_task = task
        frame = self.frame

        def job():
            try:
                if kind == "font":
                    return ops.preview_font(self.path, opts, log=lambda m: None, bg=None, task=task)
                return ops.preview_media(self.path, opts, log=lambda m: None, bg=None, task=task, frame=frame)
            except Exception as ex:
                self.app.log(f"preview: {ex}", 2)
                return None

        png = await self.app.run(job, report=False)
        if token != self.token or task.cancelled:
            return
        if png:
            self.preview_img.src = png
            self.preview_box.opacity = 1.0
        self.app.page.update()

    def on_closed(self):
        """Dialog gone (✕, tap outside, or after a finished import): stop anything still running."""
        self.token += 1
        self.cancel_preview()

    # run ------------------------------------------------------------------
    def open(self):
        self.options.controls = self.option_controls()
        self.sync_frame_row()
        content = ft.Column([ft.Container(self.preview_box, alignment=ft.Alignment.CENTER), self.frame_row,
                             self.options], spacing=14, scroll=ft.ScrollMode.AUTO, width=300, height=520)
        self.holder["dlg"] = self.app.dialog("Upload", content, [
            ft.Container(pill_button("Import", self.do_import, filled=True), padding=ft.Padding(right=8))],
            on_close=self.on_closed)
        self.schedule_preview()

    async def do_import(self, e):
        if self.busy:
            return
        try:
            kind, opts = self.collect()
        except ops.OpError as ex:
            self.app.toast(str(ex), error=True)
            return
        v, path, token = self.view, self.path, self.app.api.token
        self.busy = True
        self.token += 1
        self.cancel_preview()                           # free the converter before the real import
        if kind == "font":
            fn = lambda task: ops.import_font(v.bp_path, v.card_dir, path, opts, log=self.app.log, token=token, task=task)
        else:
            fn = lambda task: ops.import_media(v.bp_path, v.card_dir, path, opts, log=self.app.log, token=token, task=task)
        res = await v.ctx.edit_async("Importing", fn)
        self.busy = False
        if res:
            self.app.close_dialog(self.holder["dlg"])
            await v.after_edit(res, "Imported." if len(res) == 1 else f"Imported {len(res)} actors.")
        else:
            self.schedule_preview()                     # cancelled or failed: dialog stays, settings kept
