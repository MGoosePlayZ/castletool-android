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
import theme
from app import Screen
from cardbar import CardContext, card_bar
from screens_library import card_bg
from theme import BG, MAJOR, MINOR1, SEP, castle_icon, minor, text
from widgets import ColorField, NumberBox, icon_button, info_button, pill_button

ARCHIVES = {"zip", "tar", "gz", "tgz", "bz2", "xz", "tbz2", "txz"}
MEDIA_EXTS = sorted({e.lstrip(".") for e in (ct.IMAGE_EXTS | ct.VIDEO_EXTS | ct.VECTOR_EXTS | ct.FONT_EXTS)} | ARCHIVES)
AUDIO_EXTS = sorted({e.lstrip(".") for e in ct.AUDIO_EXTS} | ARCHIVES)
FIELD_TEXT = ft.TextStyle(font_family=theme.BODY, color=MAJOR, size=14)
BLANK_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABpfZFQAAAAABJRU5ErkJggg==")


def blueprint_screen(app, ctx: CardContext, bp_path: Path) -> Screen:
    view = BlueprintView(app, ctx, bp_path)
    return Screen(title="", build=lambda a: view.build(), bar=lambda a: card_bar(a, ctx, "blueprint"),
                  on_leave=view.dispose, title_font=False)


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
        self.reload()
        self.root = ft.Column([], spacing=0, scroll=ft.ScrollMode.AUTO, expand=True)

    # ── state ─────────────────────────────────────────────────────────────
    def reload(self) -> bool:
        if not self.bp_path.exists():
            return False
        self.bp = ops.load_blueprint(self.bp_path)
        self.name = ops.blueprint_title(self.bp, self.bp_path.stem)
        self.bg = card_bg(self.card_dir)
        return True

    def dispose(self):
        self.alive = False
        self.anim_token += 1
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
        d2 = drawing.get_drawing2(self.bp)
        holder = ft.Container(width=76, height=76, bgcolor=self.bg, border_radius=10, border=ft.Border.all(1, SEP),
                              alignment=ft.Alignment.CENTER)
        if d2 and drawing.frame_count(d2):
            holder.content = ft.Image(src=drawing.render_png(self.bp, 152, self.bg), width=72, height=72,
                                      fit=ft.BoxFit.CONTAIN, gapless_playback=True)
        else:
            holder.content = castle_icon("brick", 28, MINOR1)
        return ft.Container(ft.Row([
            holder, text(self.name, 20, bold=True, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS, expand=True)],
            spacing=14, vertical_alignment=ft.CrossAxisAlignment.CENTER), padding=14)

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
            out.append(ft.Container(castle_icon("pencil", 30, MINOR1), height=140, bgcolor=self.bg, border_radius=12,
                                    border=ft.Border.all(1, SEP), alignment=ft.Alignment.CENTER))
        out.append(ft.Container(pill_button("Upload", self.on_pick_media, castle_icon("upload", 18, BG), filled=True),
                                margin=ft.Margin.only(top=14)))
        out.append(self.log_box())
        return ft.Column(out, spacing=6)

    def preview_block(self, d2: dict) -> ft.Control:
        info = drawing.drawing_info(d2)
        n = info["frames"]
        img = ft.Image(src=drawing.render_png(d2, 240, self.bg), width=240, height=240, fit=ft.BoxFit.CONTAIN,
                       gapless_playback=True)
        controls: list[ft.Control] = [
            ft.Container(img, bgcolor=self.bg, border_radius=12, border=ft.Border.all(1, SEP),
                         alignment=ft.Alignment.CENTER, height=250)]
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
                frames = await asyncio.to_thread(drawing.render_animation, d2, 240, self.bg)
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
            out.append(ft.Container(castle_icon("music", 30, MINOR1), height=120, border_radius=12,
                                    border=ft.Border.all(1, SEP), alignment=ft.Alignment.CENTER))
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
        state = {"dragging": False, "icon": "play"}

        play_btn = icon_button(castle_icon("play", 26), None, size=44)
        stop_btn = icon_button(castle_icon("stop", 22), None, size=40)
        slider = ft.Slider(min=0, max=dur_ms, value=0, expand=True, active_color=MAJOR, inactive_color=SEP)
        # fixed-width time slots: DM Sans digits aren't tabular, so the slider must not jiggle
        elapsed = ft.Container(text("0:00", 12, MINOR1, text_align=ft.TextAlign.RIGHT), width=40,
                               alignment=ft.Alignment.CENTER_RIGHT)
        length = ft.Container(text(_clock(eng.duration), 12, MINOR1, text_align=ft.TextAlign.LEFT), width=40,
                              alignment=ft.Alignment.CENTER_LEFT)

        def paint_icon():
            icon = "pause" if eng.playing else "play"
            if icon != state["icon"]:
                state["icon"] = icon
                play_btn.content = castle_icon(icon, 26)

        def toggle(e):
            if eng.playing:
                eng.pause()
            else:
                eng.play()
            paint_icon()
            self.app.page.update()

        def stop(e):
            eng.stop()
            slider.value = 0
            elapsed.content.value = "0:00"
            paint_icon()
            self.app.page.update()

        def drag_start(e):
            state["dragging"] = True

        def drag_end(e):
            state["dragging"] = False
            eng.seek(float(e.control.value) / 1000)

        play_btn.on_click, stop_btn.on_click = toggle, stop
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
        row = ft.Row([play_btn, stop_btn, elapsed, slider, length], spacing=2,
                     vertical_alignment=ft.CrossAxisAlignment.CENTER)
        if eng.warnings:
            row.controls.append(info_button(self.app, "\n".join(eng.warnings), "Preview"))
        return row

    async def on_pick_midi(self, e):
        path = await self.app.pick(["mid", "midi"], "Choose a MIDI file")
        if not path:
            return
        self.busy = True
        self.render()
        res = await self.ctx.edit_async(lambda: ops.import_midi(self.bp_path, self.card_dir, path,
                                                                log=self.app.log))
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
            out.append(ft.Container(castle_icon("unmuted", 30, MINOR1), height=120, border_radius=12,
                                    border=ft.Border.all(1, SEP), alignment=ft.Alignment.CENTER))
        out.append(ft.Container(ft.Row([
            pill_button("Upload", self.on_pick_audio, castle_icon("upload", 18, BG), filled=True, busy=self.busy),
            info_button(self.app, "Sounds longer than 30 seconds are split into parts that play back to back. "
                                  "Only .mp3 files can be added on devices that can't encode MP3.", "Audio"),
        ], spacing=6), margin=ft.Margin.only(top=14)))
        out.append(self.log_box())
        return ft.Column(out, spacing=8)

    async def on_pick_audio(self, e):
        if not await self.app.ensure_online():
            return
        path = await self.app.pick(AUDIO_EXTS, "Choose audio or an archive of audio")
        if not path:
            return
        token = self.app.api.token
        self.busy = True
        self.render()
        res = await self.ctx.edit_async(
            lambda: ops.import_audio(self.bp_path, self.card_dir, path, log=self.app.log, token=token))
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
        self.preview_img = ft.Image(width=200, height=200, fit=ft.BoxFit.CONTAIN, gapless_playback=True, src=BLANK_PNG)
        self.preview_box = ft.Container(self.preview_img, width=220, height=220, bgcolor=view.bg, border_radius=12,
                                        border=ft.Border.all(1, SEP), alignment=ft.Alignment.CENTER, opacity=0.4)
        self.options = ft.Column([], spacing=10)
        self.import_host = ft.Container()

    # widgets -------------------------------------------------------------
    def set(self, key: str, value):
        self.vals[key] = value
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
            c.append(ft.Row([self.choice("layout", "Several files", [("frames", "Frames of one actor"),
                                                                     ("separate", "Separate actors")], "frames"),
                             info_button(self.app, "Frames of one actor: every file becomes a frame of this blueprint. "
                                                   "Separate actors: every file becomes its own blueprint.", "Several files")],
                            spacing=4))
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
    def schedule_preview(self):
        self.token += 1
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
        fn = ops.preview_font if kind == "font" else ops.preview_media
        try:
            png = await asyncio.to_thread(lambda: fn(self.path, opts, log=lambda m: None, bg=self.view.bg))
        except Exception:
            png = None
        if token != self.token:
            return
        if png:
            self.preview_img.src = png
            self.preview_box.opacity = 1.0
        self.app.page.update()

    # run ------------------------------------------------------------------
    def open(self):
        self.options.controls = self.option_controls()
        self.paint_import()
        body = ft.Column([ft.Container(self.preview_box, alignment=ft.Alignment.CENTER), self.options],
                         spacing=14, scroll=ft.ScrollMode.AUTO, width=300, height=520)
        self.app.dialog("Upload", body, [
            ft.TextButton(content=text("Cancel", 14, MINOR1), on_click=self.cancel),
            ft.Container(self.import_host, padding=ft.Padding(right=8))])
        self.schedule_preview()

    def paint_import(self):
        self.import_host.content = pill_button("Import", self.do_import, filled=True, busy=self.busy)

    def cancel(self, e):
        self.token += 1
        self.app.page.pop_dialog()

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
        self.paint_import()
        self.app.page.update()
        if kind == "font":
            fn = lambda: ops.import_font(v.bp_path, v.card_dir, path, opts, log=self.app.log, token=token)
        else:
            fn = lambda: ops.import_media(v.bp_path, v.card_dir, path, opts, log=self.app.log, token=token)
        res = await v.ctx.edit_async(fn)
        self.busy = False
        if res:
            self.token += 1
            self.app.page.pop_dialog()
            await v.after_edit(res, "Imported." if len(res) == 1 else f"Imported {len(res)} actors.")
        else:
            self.paint_import()
            self.app.page.update()
