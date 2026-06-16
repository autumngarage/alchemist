"""ASCII hero banner for `alchemist scan`, `doctor`, and other splash moments."""

from __future__ import annotations

import os
import sys

# Rendered from `figlet -f standard alchemist` and embedded so we don't take a
# runtime dependency.
_ALCHEMIST_GLYPHS: tuple[str, ...] = (
    r"       _      _                    _     _   ",
    r"  __ _| | ___| |__   ___ _ __ ___ (_)___| |_ ",
    r" / _` | |/ __| '_ \ / _ \ '_ ` _ \| / __| __|",
    r"| (_| | | (__| | | |  __/ | | | | | \__ \ |_ ",
    r" \__,_|_|\___|_| |_|\___|_| |_| |_|_|___/\__|",
)

_ANSI_AMBER = "\033[38;5;222m"   # primary glyph color
_ANSI_GOLD = "\033[38;5;230m"    # subtitle / attribution
_ANSI_RESET = "\033[0m"


def _color_enabled(stream) -> bool:
    """True when the stream is a TTY and NO_COLOR is unset."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("CLICOLOR") == "0":
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def render_banner(
    subtitle: str | None = None,
    version: str | None = None,
    *,
    use_color: bool = True,
) -> list[str]:
    """Return the banner as a list of lines (no trailing newline per line)."""
    lines: list[str] = [""]
    for glyph in _ALCHEMIST_GLYPHS:
        if use_color:
            lines.append(f"  {_ANSI_AMBER}{glyph}{_ANSI_RESET}")
        else:
            lines.append(f"  {glyph}")

    sub_parts: list[str] = []
    if subtitle:
        sub_parts.append(subtitle)
    if version:
        sub_parts.append(f"v{version}")
    if sub_parts:
        sub_text = "  ·  ".join(sub_parts)
        if use_color:
            lines.append(f"  {_ANSI_GOLD}{sub_text}{_ANSI_RESET}")
        else:
            lines.append(f"  {sub_text}")
    if use_color:
        lines.append(f"  {_ANSI_GOLD}by Autumn Garage{_ANSI_RESET}")
    else:
        lines.append("  by Autumn Garage")
    lines.append("")
    return lines


def print_banner(
    subtitle: str | None = None,
    version: str | None = None,
    *,
    stream=None,
) -> None:
    """Write the banner to ``stream`` (default: stderr)."""
    target = stream if stream is not None else sys.stderr
    use_color = _color_enabled(target)
    for line in render_banner(subtitle, version, use_color=use_color):
        print(line, file=target)


def alchemist_version() -> str | None:
    """Return the resolved alchemist version for banner display."""
    from alchemist import __version__

    return str(__version__) if __version__ else None


SUBTITLE_TAGLINE = "transmute issues into pull requests"
SUBTITLE_DOCTOR = "tool + auth health check"
SUBTITLE_SCAN = "scan for dispatched issues"
