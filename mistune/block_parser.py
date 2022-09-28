import re
from .util import (
    unikey,
    escape_url,
    safe_entity,
    expand_tab,
    expand_leading_tab,
)
from .core import Parser
from .helpers import (
    LINK_LABEL,
    HTML_TAGNAME,
    HTML_ATTRIBUTES,
    BLOCK_TAGS,
    PRE_TAGS,
    unescape_char,
    parse_link_href,
    parse_link_title,
)
from .list_parser import parse_list

_INDENT_CODE_TRIM = re.compile(r'^ {1,4}', flags=re.M)
_AXT_HEADING_TRIM = re.compile(r'(\s+|^)#+\s*$')
_BLOCK_QUOTE_TRIM = re.compile(r'^ ?', flags=re.M)
_BLOCK_QUOTE_LEADING = re.compile(r'^ *>', flags=re.M)

_LINE_BLANK_END = re.compile(r'\n[ \t]*\n$')
_BLANK_TO_LINE = re.compile(r'[ \t]*\n')

_BLOCK_TAGS_PATTERN = '|'.join(BLOCK_TAGS) + '|' + '|'.join(PRE_TAGS)
_OPEN_TAG_END = re.compile(HTML_ATTRIBUTES + r'[ \t]*>[ \t]*(?:\n|$)')
_CLOSE_TAG_END = re.compile(r'[ \t]*>[ \t]*(?:\n|$)')


class BlockParser(Parser):
    BLANK_LINE = re.compile(r'(^[ \t\v\f]*\n)+', re.M)
    STRICT_BLOCK_QUOTE = re.compile(r'( {0,3}>[^\n]*(?:\n|$))+')

    RAW_HTML = (
        r'^ {0,3}('
        r'</?' + HTML_TAGNAME + r'|'
        r'<!--|' # comment
        r'<\?|'  # script
        r'<![A-Z]|'
        r'<!\[CDATA\[)'
    )

    BLOCK_HTML = (
        r'^ {0,3}(?:'
        r'(?:</?' + _BLOCK_TAGS_PATTERN + r'(?:[ \t]+|\n|$))'
        r'|<!--' # comment
        r'|<\?'  # script
        r'|<![A-Z]'
        r'|<!\[CDATA\[)'
    )

    SPECIFICATION = {
        'blank_line': r'(^[ \t\v\f]*\n)+',
        'axt_heading': r'^ {0,3}(?P<axt_1>#{1,6})(?!#+)(?P<axt_2>[ \t]*|[ \t]+.*?)$',
        'setex_heading': r'^ {0,3}(?P<setext_1>=|-){1,}[ \t]*$',
        'fenced_code': (
            r'^(?P<fenced_1> {0,3})(?P<fenced_2>`{3,}|~{3,})'
            r'[ \t]*(?P<fenced_3>.*?)$'
        ),
        'indent_code': (
            r'^(?: {4}| *\t)[^\n]+(?:\n+|$)'
            r'((?:(?: {4}| *\t)[^\n]+(?:\n+|$))|\s)*'
        ),
        'thematic_break': r'^ {0,3}((?:-[ \t]*){3,}|(?:_[ \t]*){3,}|(?:\*[ \t]*){3,})$',
        'ref_link': r'^ {0,3}\[(?P<link_1>' + LINK_LABEL + r')\]:',
        'block_quote': r'^ {0,3}>(?P<quote_1>.*?)$',
        'list': (
            r'^(?P<list_1> {0,3})'
            r'(?P<list_2>[\*\+-]|\d{1,9}[.)])'
            r'(?P<list_3>[ \t]*|[ \t].+)$'
        ),
        'block_html': BLOCK_HTML,
        'raw_html': RAW_HTML,
    }

    DEFAULT_RULES = (
        'fenced_code',
        'indent_code',
        'axt_heading',
        'setex_heading',
        'thematic_break',
        'block_quote',
        'list',
        'ref_link',
        'raw_html',
        'blank_line',
    )

    def __init__(self, block_quote_rules=None, list_rules=None, max_nested_level=6):
        super(BlockParser, self).__init__()

        if block_quote_rules is None:
            block_quote_rules = list(self.DEFAULT_RULES)

        if list_rules is None:
            list_rules = list(self.DEFAULT_RULES)

        self.block_quote_rules = block_quote_rules
        self.list_rules = list_rules
        self.max_nested_level = max_nested_level
        # register default parse methods
        self._methods = {
            name: getattr(self, 'parse_' + name) for name in self.SPECIFICATION
        }

    def parse_blank_line(self, m, state):
        state.append_token({'type': 'blank_line'})
        return m.end()

    def parse_thematic_break(self, m, state):
        state.append_token({'type': 'thematic_break'})
        # $ does not count '\n'
        return m.end() + 1

    def parse_indent_code(self, m, state):
        # it is a part of the paragraph
        end_pos = state.append_paragraph()
        if end_pos:
            return end_pos

        code = m.group(0)
        code = expand_leading_tab(code)
        code = _INDENT_CODE_TRIM.sub('', code)
        code = code.strip('\n')
        state.append_token({'type': 'block_code', 'raw': code})
        return m.end()

    def parse_fenced_code(self, m, state):
        spaces = m.group('fenced_1')
        marker = m.group('fenced_2')
        info = m.group('fenced_3')

        c = marker[0]
        if info and c == '`':
            # CommonMark Example 145
            # Info strings for backtick code blocks cannot contain backticks
            if info.find(c) != -1:
                return

        _end = re.compile(
            r'^ {0,3}' + c + '{' + str(len(marker)) + r',}[ \t]*(?:\n|$)', re.M)
        cursor_start = m.end() + 1

        m2 = _end.search(state.src, cursor_start)
        if m2:
            code = state.src[cursor_start:m2.start()]
            end_pos = m2.end()
        else:
            code = state.src[cursor_start:]
            end_pos = state.cursor_max

        if spaces and code:
            _trim_pattern = re.compile('^ {0,' + str(len(spaces)) + '}', re.M)
            code = _trim_pattern.sub('', code)

        token = {'type': 'block_code', 'raw': code, 'fenced': True}
        if info:
            info = unescape_char(info)
            token['attrs'] = {'info': safe_entity(info.strip())}

        state.append_token(token)
        return end_pos

    def parse_axt_heading(self, m, state):
        level = len(m.group('axt_1'))
        text = m.group('axt_2').strip()
        # remove last #
        if text:
            text = _AXT_HEADING_TRIM.sub('', text)

        token = {'type': 'heading', 'text': text, 'attrs': {'level': level}}
        state.append_token(token)
        return m.end() + 1

    def parse_setex_heading(self, m, state):
        last_token = state.last_token()
        if last_token and last_token['type'] == 'paragraph':
            level = 1 if m.group('setext_1') == '=' else 2
            last_token['type'] = 'heading'
            last_token['attrs'] = {'level': level}
            return m.end() + 1

        sc = self.compile_sc(['thematic_break', 'list'])
        m = sc.match(state.src, state.cursor)
        if m:
            return self.parse_method(m, state)

    def parse_ref_link(self, m, state):
        end_pos = state.append_paragraph()
        if end_pos:
            return end_pos

        key = unikey(m.group('link_1'))
        if not key:
            return

        href, href_pos = parse_link_href(state.src, m.end(), block=True)
        if href is None:
            return

        _blank = self.BLANK_LINE.search(state.src, href_pos)
        if _blank:
            max_pos = _blank.start()
        else:
            max_pos = state.cursor_max

        title, title_pos = parse_link_title(state.src, href_pos, max_pos)
        if title_pos:
            m = _BLANK_TO_LINE.match(state.src, title_pos)
            if m:
                title_pos = m.end()
            else:
                title_pos = None
                title = None

        if title_pos is None:
            m = _BLANK_TO_LINE.match(state.src, href_pos)
            if m:
                href_pos = m.end()
            else:
                href_pos = None
                href = None

        end_pos = title_pos or href_pos
        if not end_pos:
            return

        if key not in state.env['ref_links']:
            href = unescape_char(href)
            attrs = {'url': escape_url(href)}
            if title:
                attrs['title'] = safe_entity(title)
            state.env['ref_links'][key] = attrs
        return end_pos

    def parse_block_quote(self, m, state):
        # cleanup at first to detect if it is code block
        text = m.group('quote_1') + '\n'
        text = expand_leading_tab(text, 3)
        text = _BLOCK_QUOTE_TRIM.sub('', text)

        sc = self.compile_sc(['blank_line', 'indent_code', 'fenced_code'])
        require_marker = bool(sc.match(text))

        state.cursor = m.end() + 1

        end_pos = None
        if require_marker:
            m = self.STRICT_BLOCK_QUOTE.match(state.src, state.cursor)
            if m:
                quote = m.group(0)
                quote = _BLOCK_QUOTE_LEADING.sub('', quote)
                quote = expand_leading_tab(quote, 3)
                quote = _BLOCK_QUOTE_TRIM.sub('', quote)
                text += quote
                state.cursor = m.end()
        else:
            prev_blank_line = False
            break_sc = self.compile_sc([
                'blank_line', 'thematic_break', 'fenced_code',
                'list', 'block_html',
            ])
            while state.cursor < state.cursor_max:
                m = self.STRICT_BLOCK_QUOTE.match(state.src, state.cursor)
                if m:
                    quote = m.group(0)
                    quote = _BLOCK_QUOTE_LEADING.sub('', quote)
                    quote = expand_leading_tab(quote, 3)
                    quote = _BLOCK_QUOTE_TRIM.sub('', quote)
                    text += quote
                    state.cursor = m.end()
                    if not quote.strip():
                        prev_blank_line = True
                    else:
                        prev_blank_line = bool(_LINE_BLANK_END.search(quote))
                    continue

                if prev_blank_line:
                    # CommonMark Example 249
                    # because of laziness, a blank line is needed between
                    # a block quote and a following paragraph
                    break

                m = break_sc.match(state.src, state.cursor)
                if m:
                    end_pos = self.parse_method(m, state)
                    if end_pos:
                        break

                # lazy continuation line
                pos = state.find_line_end()
                line = state.get_text(pos)
                line = expand_leading_tab(line, 3)
                text += line
                state.cursor = pos

        # according to CommonMark Example 6, the second tab should be
        # treated as 4 spaces
        text = expand_tab(text)

        # scan children state
        child = state.child_state(text)
        if state.depth() >= self.max_nested_level - 1:
            rules = list(self.block_quote_rules)
            rules.remove('block_quote')
        else:
            rules = self.block_quote_rules

        self.parse(child, rules)
        token = {'type': 'block_quote', 'children': child.tokens}
        if end_pos:
            state.prepend_token(token)
            return end_pos
        state.append_token(token)
        return state.cursor

    def parse_list(self, m, state):
        text = m.group('list_3')
        if not text.strip():
            # Example 285
            # an empty list item cannot interrupt a paragraph
            end_pos = state.append_paragraph()
            if end_pos:
                return end_pos

        marker = m.group('list_2')
        ordered = len(marker) > 1
        attrs = {'ordered': ordered, 'tight': True}
        if ordered:
            start = int(marker[:-1])
            if start != 1:
                # Example 304
                # we allow only lists starting with 1 to interrupt paragraphs
                end_pos = state.append_paragraph()
                if end_pos:
                    return end_pos
                attrs['start'] = start

        state.cursor = m.end() + 1
        groups = (m.group('list_1'), marker, text)
        token = parse_list(self, attrs, groups, state)
        # add attrs to list item token
        for tok in token['children']:
            tok['attrs'] = {'depth': attrs['depth'], 'tight': attrs['tight']}

        end_pos = token.pop('_end_pos', None)
        if end_pos:
            state.prepend_token(token)
            return end_pos

        state.append_token(token)
        return state.cursor

    def parse_block_html(self, m, state):
        return self.parse_raw_html(m, state)

    def parse_raw_html(self, m, state):
        marker = m.group(0).strip()

        # rule 2
        if marker == '<!--':
            return _parse_html_to_end(state, '-->', m.end())

        # rule 3
        if marker == '<?':
            return _parse_html_to_end(state, '?>', m.end())

        # rule 5
        if marker == '<![CDATA[':
            return _parse_html_to_end(state, ']]>', m.end())

        # rule 4
        if marker.startswith('<!'):
            return _parse_html_to_end(state, '>', m.end())

        close_tag = None
        open_tag = None
        if marker.startswith('</'):
            close_tag = marker[2:].lower()
            # rule 6
            if close_tag in BLOCK_TAGS:
                return _parse_html_to_newline(state, self.BLANK_LINE)
        else:
            open_tag = marker[1:].lower()
            # rule 1
            if open_tag in PRE_TAGS:
                end_tag = '</' + open_tag + '>'
                return _parse_html_to_end(state, end_tag, m.end())
            # rule 6
            if open_tag in BLOCK_TAGS:
                return _parse_html_to_newline(state, self.BLANK_LINE)

        # Blocks of type 7 may not interrupt a paragraph.
        end_pos = state.append_paragraph()
        if end_pos:
            return end_pos

        # rule 7
        start_pos = m.end()
        end_pos = state.find_line_end()
        if (open_tag and _OPEN_TAG_END.match(state.src, start_pos, end_pos)) or \
           (close_tag and _CLOSE_TAG_END.match(state.src, start_pos, end_pos)):
            return _parse_html_to_newline(state, self.BLANK_LINE)

    def postprocess_paragraph(self, token, parent):
        """A method to post process paragraph token. Developers CAN
        subclass BlockParser and rewrite this method to update the
        common paragraph token."""
        attrs = parent.get('attrs')
        if attrs and attrs.get('tight'):
            token['type'] = 'block_text'

    def parse(self, state, rules=None):
        sc = self.compile_sc(rules)

        while state.cursor < state.cursor_max:
            m = sc.search(state.src, state.cursor)
            if not m:
                break

            end_pos = m.start()
            if end_pos > state.cursor:
                text = state.get_text(end_pos)
                state.add_paragraph(text)
                state.cursor = end_pos

            end_pos = self.parse_method(m, state)
            if end_pos:
                state.cursor = end_pos
            else:
                end_pos = state.find_line_end()
                text = state.get_text(end_pos)
                state.add_paragraph(text)
                state.cursor = end_pos

        if state.cursor < state.cursor_max:
            text = state.src[state.cursor:]
            state.add_paragraph(text)
            state.cursor = state.cursor_max

    def render(self, state, inline):
        return self._call_render(state.tokens, state, inline)

    def _call_render(self, tokens, state, inline, parent=None):
        data = self._iter_render(tokens, state, inline, parent)
        if inline.renderer:
            return inline.renderer(data)
        return list(data)

    def _iter_render(self, tokens, state, inline, parent):
        for tok in tokens:
            if 'children' in tok:
                children = self._call_render(tok['children'], state, inline, tok)
                tok['children'] = children
            elif 'text' in tok:
                text = tok.pop('text')
                children = inline(text.strip(), state.env)
                tok['children'] = children
                if tok['type'] == 'paragraph' and parent:
                    self.postprocess_paragraph(tok, parent)
            yield tok


def _parse_html_to_end(state, end_marker, start_pos):
    marker_pos = state.src.find(end_marker, start_pos)
    if marker_pos == -1:
        text = state.src[state.cursor:]
        end_pos = state.cursor_max
    else:
        text = state.get_text(marker_pos)
        state.cursor = marker_pos
        end_pos = state.find_line_end()
        text += state.get_text(end_pos)

    state.append_token({'type': 'block_html', 'raw': text})
    return end_pos


def _parse_html_to_newline(state, newline):
    m = newline.search(state.src, state.cursor)
    if m:
        end_pos = m.start()
        text = state.get_text(end_pos)
    else:
        text = state.src[state.cursor:]
        end_pos = state.cursor_max

    state.append_token({'type': 'block_html', 'raw': text})
    return end_pos
