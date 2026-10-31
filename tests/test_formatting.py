import html

import pytest

from app.formatting import format_duration, split_html_text


@pytest.mark.parametrize("text", [
    "",
    "Привет, мир",
    "Нет, нет, нет! & <div> >",
    "BEGIN\n" + ("слово " * 1700) + "\nEND",
    "<&>" * 1500,
    "\n" * 5000,
    "супердлинноеслово" * 1000,
])
def test_split_preserves_every_character_and_safe_html(text):
    chunks = split_html_text(text, 3400)
    assert chunks
    assert all(len(chunk) <= 3400 for chunk in chunks)
    assert "".join(html.unescape(chunk) for chunk in chunks) == text


def test_escaping_boundary_never_splits_entity():
    chunks = split_html_text("&" * 20, 5)
    assert chunks == ["&amp;"] * 20


def test_reject_too_small_limit():
    with pytest.raises(ValueError):
        split_html_text("hello", 4)


def test_format_duration():
    assert format_duration(62) == "1:02"
    assert format_duration(3661) == "1:01:01"
    assert format_duration(-10) == "0:00"
