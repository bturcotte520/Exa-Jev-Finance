from copy import deepcopy

import pytest

from app.rendering import display_run, render_passage


URL = "https://example.com/news/story"


def test_markdown_blocks_and_inline_formatting():
    result = render_passage("## Results\n\n**Revenue** grew *quickly*.\n\n- First\n- Second\n\n1. One\n2. Two\n\n`code` and ~~removed~~\n\n```python\nprint('ok')\n```", URL)
    for expected in ("<h2>Results</h2>", "<strong>Revenue</strong>", "<em>quickly</em>", "<ul>", "<ol>", "<code>code</code>", "<s>removed</s>", '<code class="language-python">'):
        assert expected in result


def test_table_and_entities():
    result = render_passage("| Company | Change |\n| --- | --- |\n| A &amp; B | 3% |", URL)
    assert "<table>" in result
    assert "<th>Company</th>" in result
    assert "<td>A &amp; B</td>" in result


def test_table_alignment_does_not_require_inline_styles():
    result = render_passage("| Amount |\n| ---: |\n| 3 |", URL)
    assert 'class="align-right"' in result
    assert 'style=' not in result


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,test", "vbscript:alert(1)", "file:///etc/passwd", "mailto:test@example.com", "https://user:password@example.com", "jav&#x61;script:alert(1)"])
def test_unsafe_links_are_not_clickable(url):
    assert "<a " not in render_passage(f"[click]({url})", URL)


def test_http_links_and_relative_links():
    result = render_passage("[Source](https://example.org/report) and [Details](/report)", URL)
    assert 'href="https://example.org/report"' in result
    assert 'href="https://example.com/report"' in result
    assert result.count('rel="noopener noreferrer"') == 2


def test_raw_html_is_text_and_images_do_not_load():
    result = render_passage('<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n![Tracking](https://example.com/pixel)', URL)
    assert "<script" not in result and "<img" not in result
    assert "&lt;script&gt;" in result
    assert "Tracking" in result


def test_display_is_derived_and_does_not_change_evidence():
    run = {"sources": [{"url": URL, "passages": ["**Original** passage"], "passages_html": ['<script>bad</script>']}]}
    original = deepcopy(run)
    result = display_run(run)
    assert result["sources"][0]["passages_html"] == ["<p><strong>Original</strong> passage</p>\n"]
    assert run == original
