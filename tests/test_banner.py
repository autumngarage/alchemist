"""Tests for the brand banner module."""

from __future__ import annotations

import io

from alchemist.banner import (
    SUBTITLE_TAGLINE,
    print_banner,
    render_banner,
)


def test_render_banner_no_color_includes_attribution():
    lines = render_banner(use_color=False)
    body = "\n".join(lines)
    assert "alchemist" in body or "_| |" in body  # glyph fragment present
    assert "by Autumn Garage" in body


def test_render_banner_subtitle_and_version_appear_together():
    lines = render_banner(subtitle=SUBTITLE_TAGLINE, version="0.0.1", use_color=False)
    body = "\n".join(lines)
    assert SUBTITLE_TAGLINE in body
    assert "v0.0.1" in body


def test_render_banner_color_path_wraps_with_ansi():
    lines = render_banner(use_color=True)
    body = "\n".join(lines)
    assert "\033[38;5;222m" in body  # amber primary
    assert "\033[38;5;230m" in body  # gold accent


def test_print_banner_writes_to_stream():
    buf = io.StringIO()
    print_banner(subtitle="hello", version="1.2.3", stream=buf)
    body = buf.getvalue()
    assert "hello" in body
    assert "v1.2.3" in body
    assert "by Autumn Garage" in body
