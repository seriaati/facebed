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
        formatted = super().format(record)
        if '%' not in formatted:
            return formatted
        return re.sub(
            r'https?://\S+', lambda match: _readable_log_url(match[0]),
            formatted, flags=re.IGNORECASE,
        )


def setup_logging(level: str = 'INFO') -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(ConsoleFormatter(
        '[%(levelname)s] [%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S',
    ))
    # third-party libraries only surface warnings; facebed's own level is configurable
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    set_log_level(level)


def set_log_level(level: str) -> None:
    logging.getLogger('facebed').setLevel(level.upper())


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
