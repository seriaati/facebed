# Discord component embed (Components V2 link preview) for a ParsedPost.
# Discord silently falls back to the OG card when the serialized JSON exceeds 3,000 bytes.
import json
import re

MAX_BYTES = 3000
MIN_CAPTION = 300  # below this the shared-post context is shrunk first
ACCENT = 0x0866FF
MAX_BUTTON_URL = 512
FB = 'https://www.facebook.com'

_URL_RE = re.compile(r'https?://\S+')
_TAG_RE = re.compile(r'(?<![\w&])#(\w+)')
_MD_RE = re.compile(r'([\\*_~`|\[\]()<>])')
_LINE_RE = re.compile(r'^(\s*)([#>\-+]|\d+\.)', re.M)


def md_escape(s: str) -> str:
    return _LINE_RE.sub(lambda m: m.group(1) + '\\' + m.group(2), _MD_RE.sub(r'\\\1', s))


def fmt_text(s: str) -> str:
    """Escape Markdown outside URLs, link #hashtags to Facebook."""
    out, pos = [], 0
    tokens = sorted([(m.start(), m.end(), 'u', m.group(0)) for m in _URL_RE.finditer(s)] +
                    [(m.start(), m.end(), 't', m.group(1)) for m in _TAG_RE.finditer(s)])
    for start, end, kind, val in tokens:
        if start < pos:
            continue
        out.append(md_escape(s[pos:start]))
        out.append(val if kind == 'u' else f'[#{md_escape(val)}]({FB}/hashtag/{val.lower()})')
        pos = end
    out.append(md_escape(s[pos:]))
    return ''.join(out)


def cut(s: str, n: int) -> str:
    s = s.strip()
    if len(s) <= n:
        return s
    s = re.sub(r'#\w*$', '', s[:n])  # don't leave a half hashtag to be linked
    return s.rstrip() + '…'


def quote_block(s: str) -> str:
    return '\n'.join('> ' + line for line in s.split('\n'))


def serialize(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c')


def size(payload: dict) -> int:
    return len(serialize(payload).encode('utf-8'))


def sep(spacing: int = 2, divider: bool = False) -> dict:
    return {'type': 14, 'divider': divider, 'spacing': spacing}


def gallery(urls: list[str]) -> dict:
    return {'type': 12, 'items': [{'media': {'url': u}} for u in urls]}


def ext(kind: str) -> str:
    return 'mp4' if kind == 'video' else 'jpg'


def name_link(name: str, url: str) -> str:
    label = md_escape(name or 'Facebook')
    return f'[{label}]({url})' if url else label


def build(post, slot, caption_len: int, shared_len: int = 300, shared_media: int = 10) -> dict:
    header = '### ' + name_link(post.author, post.author_url) + (' ✓' if post.verified else '')
    if post.group_name:
        header += '\n-# 👥 ' + name_link(post.group_name, post.group_url)
    texts = [{'type': 10, 'content': header}]
    text = post.own_text if post.own_text is not None else post.text
    if text and caption_len > 0:
        texts.append({'type': 10, 'content': fmt_text(cut(text, caption_len))})
    if post.avatar:
        comps = [{'type': 9, 'components': texts, 'accessory': {'type': 11, 'media': {'url': slot('a', 'jpg')}}}]
    else:
        comps = texts

    budget = 10  # gallery items are capped at 10 across the whole embed
    main = post.media[:budget]
    budget -= len(main)
    if main:
        comps += [sep(), gallery([slot(str(i), ext(k)) for i, (k, _) in enumerate(main)])]

    sh = post.shared
    if sh == 'unavailable':
        comps += [sep(divider=True), {'type': 10, 'content': '-# 🔁 Shared post is unavailable'}]
    elif sh:
        line = '**' + name_link(sh['author'], sh['url']) + '**' + (' ✓' if sh['verified'] else '')
        line += f" · <t:{sh['date']}:d>" if sh['date'] > 0 else ''
        stexts = [{'type': 10, 'content': '-# 🔁 Shared from\n' + line}]
        if sh['text']:
            stexts.append({'type': 10, 'content': quote_block(fmt_text(cut(sh['text'], shared_len)))})
        comps.append(sep(divider=True))
        if sh['avatar']:
            comps.append({'type': 9, 'components': stexts, 'accessory': {'type': 11, 'media': {'url': slot('qa', 'jpg')}}})
        else:
            comps += stexts
        smedia = sh['media'][:min(budget, shared_media)]
        if smedia:
            comps += [sep(spacing=1), gallery([slot(f'q{i}', ext(k)) for i, (k, _) in enumerate(smedia)])]

    likes, cmts, shares = post.counts
    stats = [f'❤️ **{likes:,}**' if likes is not None else '',
             f'💬 **{cmts:,}**' if cmts is not None else '',
             f'🔁 **{shares:,}**' if shares is not None else '',
             f'<t:{post.date}:f>' if post.date and post.date > 0 else '']
    footer = ' · '.join(x for x in stats if x)
    if footer:
        comps += [sep(divider=True), {'type': 10, 'content': footer}]

    buttons = [('Open on Facebook', post.url)]
    if post.author_url:
        buttons.append(('View profile', post.author_url))
    elif post.group_url:
        buttons.append(('View group', post.group_url))
    buttons = [{'type': 2, 'style': 5, 'label': label, 'url': url} for label, url in buttons
               if url and url.startswith(('http://', 'https://')) and len(url) <= MAX_BUTTON_URL]
    if buttons:
        comps += [sep(spacing=1), {'type': 1, 'components': buttons}]
    return {'component': {'type': 17, 'accent_color': ACCENT, 'components': comps}}


def build_fitting(post, slot) -> dict | None:
    """Largest caption that keeps the payload <= 3,000 bytes; tighten shared context if it crowds the caption out."""
    text = post.own_text if post.own_text is not None else post.text
    full = len(text or '')
    best = None
    for shared_len, shared_media in [(300, 10), (150, 4), (80, 1)]:
        if size(build(post, slot, 0, shared_len, shared_media)) > MAX_BYTES:
            continue
        lo, hi = 0, full
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if size(build(post, slot, mid, shared_len, shared_media)) <= MAX_BYTES:
                lo = mid
            else:
                hi = mid - 1
        best = build(post, slot, lo, shared_len, shared_media)
        if lo >= min(full, MIN_CAPTION) or not post.shared:
            return best
    return best


def render(post, origin: str) -> str | None:
    """Serialized payload for the inline <script>, or None when the post has no embed data or cannot fit."""
    if not post.key:
        return None

    def slot(name: str, extension: str) -> str:
        return f'{origin}/m/{post.key}/{name}.{extension}'

    payload = build_fitting(post, slot)
    return serialize(payload) if payload else None
