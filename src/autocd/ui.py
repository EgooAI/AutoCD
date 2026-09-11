"""Small terminal helpers: ANSI colors, bounded panels and safe text."""

import os
import re
import shutil
import sys
import unicodedata


STYLES = {"title": "1", "muted": "2", "accent": "36", "success": "32",
          "warning": "33", "error": "31"}
SGR = re.compile(r"\x1b\[[0-9;]*m")
TOKENS = re.compile(r"\x1b\[[0-9;]*m|.", re.DOTALL)


def terminal_text(value):
    # External text must never be interpreted as terminal commands.
    return "".join(c if c in "\n\t" or unicodedata.category(c) not in {"Cc", "Cs"}
                   else "�" for c in str(value))


def inline(value):
    return terminal_text(value).replace("\n", " ↵ ").replace("\t", "    ")


def has_color(stream=None):
    stream = stream if stream is not None else sys.stdout
    return (stream.isatty() and "NO_COLOR" not in os.environ
            and os.environ.get("TERM", "") != "dumb")


def color(value, tone="accent", *, stream=None):
    text = inline(value)
    return f"\x1b[{STYLES[tone]}m{text}\x1b[0m" if has_color(stream) else text


def cells(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1
               for c in SGR.sub("", text))


def wrap(text, width):
    """Wrap by terminal cells, retaining our own SGR colors across line breaks."""
    line, used, active = "", 0, ""
    for token in TOKENS.findall(text):
        if SGR.fullmatch(token):
            line += token
            active = "" if token == "\x1b[0m" else token
            continue
        size = cells(token)
        if token == "\n" or used + size > width:
            yield line + ("\x1b[0m" if active else "")
            line, used = active, 0
            if token == "\n":
                continue
        line += token
        used += size
    yield line + ("\x1b[0m" if active else "")


def panel(title, lines, *, stream=None):
    stream = stream if stream is not None else sys.stdout
    width = max(8, min(96, shutil.get_terminal_size(fallback=(88, 24)).columns))
    label = next(wrap(inline(title), width - 6))
    top = "┌─ " + label + " " + "─" * (width - cells(label) - 5) + "┐"
    print(color(top, "muted", stream=stream), file=stream)
    edge = color("│", "muted", stream=stream)
    for line in lines:
        for part in wrap(line, width - 4):
            print(f"{edge} {part}{' ' * (width - 4 - cells(part))} {edge}", file=stream)
    print(color("└" + "─" * (width - 2) + "┘", "muted", stream=stream), file=stream)


def heading(title, subtitle=None):
    print()
    lines = [color(title, "title")]
    if subtitle:
        lines.append(color(subtitle, "muted"))
    panel("AutoCD", lines)


def action(key, label, detail=None, *, tone=None):
    tone = tone or ("error" if key == "x" else "warning" if key == "d" else "accent")
    text = color(f"[{key}]", tone) + " " + inline(label)
    if detail:
        text += "  " + color(detail, "muted")
    return text


def actions(groups, footer=None):
    lines = []
    available = max(4, min(96, shutil.get_terminal_size(fallback=(88, 24)).columns) - 4)
    for title, entries in groups:
        if lines:
            lines.append("")
        lines.append(color(title, "title"))
        packed = ""
        for entry in entries:
            item = action(*entry)
            if packed and cells(packed) + cells(item) + 3 > available:
                lines.append(packed)
                packed = ""
            packed += ("   " if packed else "") + item
        if packed:
            lines.append(packed)
    if footer:
        lines.extend(["", color(footer, "muted")])
    panel("操作", lines)


def notice(message, tone="accent", *, stream=None):
    stream = stream if stream is not None else sys.stdout
    label = {"success": "完成", "warning": "注意", "error": "未完成"}.get(tone, "提示")
    print(color(f"[{label}]", tone, stream=stream) + " " + terminal_text(message), file=stream)


def prompt(label, default=None):
    suffix = f" [{inline(default)}]" if default is not None and default != "" else ""
    value = input(color("›", "accent") + f" {inline(label)}{suffix}: ").strip()
    return value if value else default


def confirm(title, message, word="y"):
    panel(title, [*(color(line, "warning") for line in message.splitlines()),
                  color(f"输入 {word} 确认；Enter 取消", "muted")])
    return (prompt("确认", "") or "").lower() == word


def pause():
    input(color("›", "accent") + " 按 Enter 返回菜单…")
