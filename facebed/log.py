import json
import logging
import re
from html import unescape
from urllib.parse import unquote


def _readable_log_url(url: str) -> str:
    def decode_percent(match):
        try:
            decoded = unquote(match[0], errors='strict')
        except UnicodeDecodeError:
            return match[0]
        if any(
            ord(char) < 32 or 0x7f <= ord(char) < 0xa0 or char in '\u2028\u2029'
            for char in decoded
        ):
            return match[0]
        return decoded

    return re.sub(r'(?:%[0-9a-fA-F]{2})+', decode_percent, url)


class ConsoleFormatter(logging.Formatter):
    def format(self, record):
        formatted = re.sub(
            r'https?://\S+', lambda match: _readable_log_url(match[0]),
            super().format(record), flags=re.IGNORECASE,
        )
        return re.sub(
            r'https://(?:www\.)?(?:facebook|facebed)\.com(?![\w.@:-])/*',
            '/', formatted, flags=re.IGNORECASE,
        )


_console_handler = logging.StreamHandler()
_console_handler.setFormatter(ConsoleFormatter(
    '[%(levelname)s] [%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S',
))
logging.basicConfig(level=logging.INFO, handlers=[_console_handler])


def readable_log_title(title: str) -> str:
    def decode_unicode(match):
        value = match[0]
        try:
            decoded = chr(int(value[2:], 16)) if value.startswith('\\U') else json.loads(f'"{value}"')
        except ValueError:
            return value
        if any(ord(char) < 32 or 0x7f <= ord(char) < 0xa0 or 0xd800 <= ord(char) <= 0xdfff for char in decoded):
            return value
        return decoded

    title = re.sub(r'(?:\\u[0-9a-fA-F]{4})+|\\U[0-9a-fA-F]{8}', decode_unicode, unescape(title))
    return ' '.join(unescape(title).split())
