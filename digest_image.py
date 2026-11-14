"""Offline typography renderer for the daily discount newspaper image."""

import json
import os
import tempfile
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

SOURCE_NAMES = {
    "jump": "Jump · Switch 2",
    "switch": "Jump · Switch",
    "heybox": "小黑盒 · Switch 系列",
    "steam": "Steam",
}


def mode_name(value):
    aliases = {"图片": "image", "文字": "text", "默认": "default"}
    value = aliases.get(value, value)
    if value not in {"image", "text", "default"}:
        raise ValueError("日报发送方式：image（图片）、text（文字）、default（跟随全局）。")
    return value


class DigestImageRenderer:
    width = 1200

    def __init__(self, directory: Path, font_path: str = ""):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.font_path = (
            Path(font_path)
            if font_path
            else Path(__file__).parent / "assets/fonts/NotoSansSC-Regular.otf"
        )
        if not self.font_path.is_file():
            raise ValueError("日报中文字体不存在，请检查 digest_font_path 或插件自带字体。")
        self.fonts = {}

    def font(self, size):
        if size not in self.fonts:
            self.fonts[size] = ImageFont.truetype(str(self.font_path), size)
        return self.fonts[size]

    def wrap(self, text, size, width, max_lines=3):
        font = self.font(size)
        lines = [""]
        for char in str(text)[:800]:
            if char == "\n" or font.getlength(lines[-1] + char) > width:
                if len(lines) == max_lines:
                    while lines[-1] and font.getlength(lines[-1] + "…") > width:
                        lines[-1] = lines[-1][:-1]
                    lines[-1] += "…"
                    break
                lines.append("" if char == "\n" else char)
            else:
                lines[-1] += char
        return lines

    def render(self, snapshot):
        """Snapshot contains no account IDs, remote resources, HTML or executable markup."""
        encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()
        key = sha256(encoded + b"newspaper-v1").hexdigest()
        target = self.directory / (key + ".png")
        if target.is_file():
            return target
        # Bounds also protect against unexpectedly long upstream titles.
        rows = []
        for game in snapshot["games"][:10]:
            name = self.wrap(game["name"], 35, 1000, 3)
            meta = " · ".join(
                x for x in [game.get("source"), game.get("platform"), game.get("region")] if x
            )
            display_id = "steam:" + game["id"] if game.get("source") == "Steam" else game["id"]
            meta += " · " + display_id
            if game.get("expires"):
                expiry = datetime.fromtimestamp(
                    game["expires"], ZoneInfo(snapshot.get("timezone", "Asia/Shanghai"))
                )
                meta += " · 截止 " + expiry.strftime("%m-%d %H:%M")
            metadata = self.wrap(meta, 22, 1000, 2)
            note = self.wrap(game.get("note", ""), 19, 1000, 1)
            height = len(name) * 47 + len(metadata) * 32 + 62 + (28 if any(note) else 0) + 32
            rows.append((game, name, metadata, note, height))
        body_height = sum(row[-1] for row in rows) if rows else 380
        height = max(1050, 295 + body_height + 258)
        paper, ink, muted, accent = "#fffefa", "#373735", "#77746e", "#db790c"
        image = Image.new("RGB", (self.width, height), paper)
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, self.width, 244), fill="#fbf8ef")

        def text(x, y, content, size, color=ink, bold=False):
            draw.text(
                (x, y),
                content,
                font=self.font(size),
                fill=color,
                anchor="lt",
                stroke_width=1 if bold else 0,
                stroke_fill=color,
            )

        # Top ornament, wide title/date block and large weekday echo the supplied reference.
        draw.line((485, 8, 568, 8), fill="#f3ce93", width=3)
        draw.ellipse((595, 4, 605, 14), fill="#f3ce93")
        draw.line((632, 8, 715, 8), fill="#f3ce93", width=3)
        for x in (590, 600, 610):
            draw.ellipse((x, 28, x + 3, 31), fill="#f3ce93")
        text(54, 68, "每天", 48, bold=True)
        text(164, 66, "60s", 54, accent, True)
        text(275, 68, "看懂折扣", 48, bold=True)
        day = datetime.strptime(snapshot["day"], "%Y-%m-%d")
        text(56, 155, day.strftime("%Y年%m月%d日"), 27, bold=True)
        source = SOURCE_NAMES.get(snapshot["source"], snapshot["source"])
        text(56, 197, source, 23, muted)
        draw.line((791, 57, 760, 206), fill="#d4d1ca", width=4)
        text(817, 73, "星期" + "一二三四五六日"[day.weekday()], 83, bold=True)
        text(825, 178, "游戏折扣日报", 25, muted)
        draw.line((0, 244, self.width, 244), fill="#e8e5dd", width=1)
        y = 289
        start = snapshot.get("start", 0)
        for index, (game, name, metadata, note, row_height) in enumerate(rows, 1):
            draw.ellipse((54, y + 4, 82, y + 32), fill="#f0efeb", outline="#dcdad3", width=1)
            num = str(start + index)
            number_width = self.font(18).getlength(num)
            text(68 - number_width / 2, y + 8, num, 18, muted)
            baseline = y
            for line in name:
                text(109, baseline, line, 35, bold=True)
                baseline += 47
            for line in metadata:
                text(110, baseline + 4, line, 22, muted)
                baseline += 32
            baseline += 9
            currency = game.get("currency") or ""
            final = game.get("final")
            original = game.get("original")
            percent = game.get("percent", 0)
            if final is None:
                price = "当前地区未返回价格"
            elif game.get("free"):
                price = "免费游戏"
            elif percent > 0 and original is not None and original > final:
                price = f"{currency} {original / 100:.2f} → {final / 100:.2f}   -{percent}%"
            else:
                price = f"{currency} {final / 100:.2f}"
            text(110, baseline, price, 31, accent, True)
            baseline += 45
            if any(note):
                text(110, baseline, note[0], 19, muted)
            draw.line(
                (109, y + row_height - 19, 1144, y + row_height - 19), fill="#efede6", width=1
            )
            y += row_height
        if not rows:
            text(110, y + 75, "本次扫描范围内暂无符合条件的折扣", 34, bold=True)
            text(110, y + 137, "明天继续为你关注好价格。", 26, muted)
        footer = height - 242
        draw.rectangle((0, footer, self.width, footer + 95), fill="#fbf8ef")
        draw.line((0, footer, self.width, footer), fill="#e6e1d5", width=1)
        quote = "好游戏值得等待，好价格值得记录。"
        quote_width = self.font(29).getlength(quote)
        text((self.width - quote_width) / 2, footer + 33, quote, 29, bold=True)
        text(74, footer + 28, "「", 37, "#e9c18b")
        text(1090, footer + 28, "」", 37, "#e9c18b")
        draw.line((0, footer + 95, self.width, footer + 95), fill="#e6e1d5", width=1)
        total = snapshot.get("total", len(rows))
        page = snapshot.get("page", 1)
        pages = snapshot.get("pages", 1)
        text(
            55,
            footer + 125,
            f"{source}  /  共 {total} 款当前优惠  /  第 {page} / {pages} 页",
            21,
            muted,
        )
        text(55, footer + 163, "仅为来源扫描范围；价格、地区及语言以商店实时结算为准。", 20, muted)
        generated = snapshot.get("generated", snapshot["day"])
        timezone = snapshot.get("timezone", "Asia/Shanghai")
        text(55, footer + 202, f"更新于 {generated}  {timezone}   ·   GAME DEALS", 19, muted)
        # Atomic output prevents readers seeing a partially written cached image.
        descriptor, tmp = tempfile.mkstemp(dir=self.directory, suffix=".png")
        os.close(descriptor)
        try:
            image.save(tmp, format="PNG", optimize=True)
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return target

    def cleanup(self, keep_seconds=7 * 86400):
        """Only generated cache files older than a week, never source fonts/data."""
        import time

        for path in self.directory.glob("*.png"):
            if time.time() - path.stat().st_mtime > keep_seconds:
                path.unlink(missing_ok=True)
