"""Markdown rendering for reference abstracts and wrangling notes."""
import re

from django.utils.safestring import mark_safe
from markdown_it import MarkdownIt

# Raw HTML stays disabled, so markdown-it escapes it. The one tag the metadata
# relies on is <br>, which is carried through as a placeholder character.
_BR_PLACEHOLDER = ''
_BR_RE = re.compile(r'<br\s*/?>', re.IGNORECASE)
_FENCE_RE = re.compile(r'^(`{3,}|~{3,})')
_BARE_URL_RE = re.compile(r'(?<![<(\[])\bhttps?://[^\s<>)\]]+')

_md = MarkdownIt('commonmark', {'html': False}).enable('table')


def render_markdown(text):
    """Render trusted-author markdown to safe HTML."""
    source = _BR_RE.sub(_BR_PLACEHOLDER, text or '')
    source = _autolink(source)
    return mark_safe(_md.render(source).replace(_BR_PLACEHOLDER, '<br>'))


def _autolink(text):
    """Turn bare URLs into autolinks, as GFM does, leaving code fences alone."""
    lines = []
    in_fence = False
    for line in text.split('\n'):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        lines.append(line if in_fence else _BARE_URL_RE.sub(r'<\g<0>>', line))
    return '\n'.join(lines)


def format_wrangling_notes(content):
    """
    Give each line of a plain-text notes file its own paragraph.

    Blank lines already present are kept, and none are added inside code fences
    or between table rows, which must stay on consecutive lines.
    """
    lines = (content or '').split('\n')
    output = []
    in_fence = False
    for i, line in enumerate(lines):
        output.append(line)
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        if i < len(lines) - 1:
            following = lines[i + 1]
            if (not in_fence and line and following
                    and not line.startswith('|') and not following.startswith('|')):
                output.append('')
    return '\n'.join(output)
