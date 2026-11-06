"""Display-only Markdown. Original evidence and provider inputs stay unchanged."""

from copy import deepcopy
from html import escape
from urllib.parse import urljoin, urlsplit

from markdown_it import MarkdownIt
from .briefing import build_briefing


def render_passage(passage, source_url):
    parser = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])

    def safe_link(url):
        try:
            parsed = urlsplit(urljoin(source_url, url))
            return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username and not parsed.password
        except ValueError:
            return False

    def link_open(renderer, tokens, index, options, env):
        token = tokens[index]
        token.attrSet("href", urljoin(source_url, token.attrGet("href")))
        token.attrSet("target", "_blank")
        token.attrSet("rel", "noopener noreferrer")
        return renderer.renderToken(tokens, index, options, env)

    def image(renderer, tokens, index, options, env):
        # Evidence should not fetch remote images or tracking pixels.
        return escape(tokens[index].content)

    def table_cell(renderer, tokens, index, options, env):
        token = tokens[index]
        alignment = token.attrs.pop("style", "").removeprefix("text-align:")
        if alignment in {"left", "center", "right"}:
            token.attrSet("class", f"align-{alignment}")
        return renderer.renderToken(tokens, index, options, env)

    parser.validateLink = safe_link
    parser.add_render_rule("link_open", link_open)
    parser.add_render_rule("image", image)
    parser.add_render_rule("th_open", table_cell)
    parser.add_render_rule("td_open", table_cell)
    return parser.render(passage)


def display_run(run):
    result = deepcopy(run)
    if "briefing" in result:
        result["briefing"] = build_briefing(result)
    for source in result.get("sources", []):
        # Never trust an HTML field in a stored/imported recording; derive it anew.
        source["passages_html"] = [render_passage(passage, source["url"]) for passage in source.get("passages", [])]
    return result
