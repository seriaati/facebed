import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote as _quote_

from yattag import indent

from facebed.config import get_config
from facebed.constants import FACEBOOK_REACTION_EMOJIS, FACEBOOK_REACTION_NAME_IDS


def quote(s: str) -> str:
    return "".join([
        _quote_(char) if char in r"<>\"'#%{}[]|\\^~`" else char
        for char in s
    ])


def get_credit() -> str:
    return 'facebed by pi.kt'


def prettify(txt: str) -> str:
    return indent(txt, indentation ='    ', newline = '\n', indent_text = True)


def timestamp_to_str(ts: int) -> str:
    if ts < 0:
        return ''
    dt = datetime.fromtimestamp(ts, timezone(timedelta(hours=get_config().timezone)))
    tztext = dt.strftime('%z')[:3]
    return '⌚ ' + dt.strftime('%Y/%m/%d %H:%M:%S ') + f'UTC{tztext}'


def human_format(num):
    if type(num) == int or re.match('^[0-9]+$', str(num)):
        num = int(num)
        num = float('{:.3g}'.format(num))
        magnitude = 0
        while abs(num) >= 1000:
            magnitude += 1
            num /= 1000.0
        return '{}{}'.format('{:f}'.format(num).rstrip('0').rstrip('.'), ['', 'K', 'M', 'B', 'T'][magnitude])
    else:
        return str(num)


def get_top_reaction_ids(feedback: dict) -> tuple[str, ...]:
    if not isinstance(feedback, dict):
        return ()
    top_reactions = feedback.get('top_reactions')
    if not isinstance(top_reactions, dict):
        return ()
    edges = top_reactions.get('edges')
    if not isinstance(edges, list):
        return ()

    ranked: list[tuple[float, int, str]] = []
    seen_ids: set[str] = set()
    for position, edge in enumerate(edges):
        if not isinstance(edge, dict):
            continue
        node = edge.get('node')
        if not isinstance(node, dict):
            continue

        reaction_id = str(node.get('id') or '')
        if reaction_id not in FACEBOOK_REACTION_EMOJIS:
            reaction_name = str(
                node.get('localized_name') or node.get('name') or ''
            ).casefold()
            reaction_id = FACEBOOK_REACTION_NAME_IDS.get(reaction_name, '')
        if not reaction_id or reaction_id in seen_ids:
            continue

        count = edge.get('reaction_count')
        if count is None:
            count = edge.get('i18n_reaction_count')
        text = str(count or '').strip().upper().replace(',', '')
        multiplier = 1
        if text.endswith(('K', 'M', 'B')):
            multiplier = {
                'K': 1_000,
                'M': 1_000_000,
                'B': 1_000_000_000,
            }[text[-1]]
            text = text[:-1]
        try:
            numeric_count = float(text) * multiplier
        except (TypeError, ValueError):
            continue
        if numeric_count <= 0:
            continue

        seen_ids.add(reaction_id)
        ranked.append((-numeric_count, position, reaction_id))

    ranked.sort()
    reaction_ids = tuple(item[2] for item in ranked[:2])
    return reaction_ids if len(reaction_ids) == 2 else ()


def format_reactions_str(
    likes: str,
    cmts: str,
    shares: str,
    top_reaction_ids: tuple[str, ...] = (),
) -> str:
    emojis: list[str] = []
    for reaction_id in top_reaction_ids:
        emoji = FACEBOOK_REACTION_EMOJIS.get(reaction_id)
        if emoji and emoji not in emojis:
            emojis.append(emoji)
    reaction_prefix = ' '.join(emojis[:2]) if len(emojis) >= 2 else '❤️'
    likes_str = f'{reaction_prefix} {likes}' if likes != 'null' else ''
    cmts_str = f'💬 {cmts}' if cmts != 'null' else ''
    shares_str = f'🔁 {shares}' if shares != 'null' else ''
    fmt = ' • '.join([x for x in [likes_str, cmts_str, shares_str] if x]).replace(',', '.')
    return fmt
