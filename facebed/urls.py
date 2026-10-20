import re
from urllib.parse import urlparse

from facebed.constants import WWWFB


def normalize_facebook_path(url_or_path: str) -> str:
    value = str(url_or_path)
    parsed = urlparse(value)
    if not parsed.netloc and re.match(
        r'^(?:www\.|web\.|m\.|mbasic\.)?facebook\.com/', value, re.IGNORECASE
    ):
        parsed = urlparse(f'https://{value}')
    if not parsed.netloc:
        return value.lstrip('/')

    hostname = (parsed.hostname or '').lower()
    if hostname == 'facebook.com' or hostname.endswith('.facebook.com'):
        normalized = parsed.path.lstrip('/')
        if parsed.query:
            normalized += f'?{parsed.query}'
        return normalized
    return value


def facebook_path_segments(url_or_path: str) -> tuple[str, ...]:
    value = str(url_or_path)
    parsed = urlparse(value)
    if parsed.netloc:
        hostname = (parsed.hostname or '').lower()
        if hostname != 'facebook.com' and not hostname.endswith('.facebook.com'):
            return ()
        path = parsed.path
    else:
        normalized = normalize_facebook_path(value)
        parsed_normalized = urlparse('/' + normalized.lstrip('/'))
        path = parsed_normalized.path
    return tuple(part for part in path.split('/') if part)


def is_facebook_url(url: str, *, require_https: bool = False) -> bool:
    parsed = urlparse(str(url))
    hostname = (parsed.hostname or '').lower()
    if hostname != 'facebook.com' and not hostname.endswith('.facebook.com'):
        return False
    if require_https and parsed.scheme.lower() != 'https':
        return False
    return parsed.scheme.lower() in {'http', 'https'}


def is_safe_media_url(url: str) -> bool:
    parsed = urlparse(str(url))
    return bool(
        parsed.scheme.lower() == 'https'
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
    )


def is_control_path(url_or_path: str, *names: str) -> bool:
    parts = facebook_path_segments(url_or_path)
    if not parts:
        return False
    first = parts[0].casefold().removesuffix('.php')
    return first in {str(name).casefold().removesuffix('.php') for name in names}


def is_post_like_path(url_or_path: str) -> bool:
    value = normalize_facebook_path(url_or_path)
    path = urlparse('/' + value.lstrip('/')).path
    return bool(
        re.search(
            r'/(?:posts|permalink|reel|watch|videos|v|photos)(?:/|$)',
            '/' + path.lstrip('/'),
            re.IGNORECASE,
        )
        or re.match(r'^/?(?:story|permalink|photo)\.php/?$', path, re.IGNORECASE)
    )


def is_share_path(path: str) -> bool:
    return bool(re.match(r'^/?share(?:/|$)', urlparse(path).path, re.IGNORECASE))


def is_group_landing_target(path: str) -> bool:
    normalized_path = urlparse(ensure_full_url(path)).path.strip('/')
    return normalized_path.startswith('groups/') and (
        normalized_path.endswith('/about')
        or len(normalized_path.split('/')) <= 2
    )


def ensure_full_url(u: str) -> str:
    value = str(u)
    parsed = urlparse(value)
    if not parsed.netloc and re.match(r'^(?:www\.|web\.|m\.|mbasic\.)?facebook\.com/', value, re.IGNORECASE):
        parsed = urlparse(f'https://{value}')
    if parsed.netloc:
        hostname = (parsed.hostname or '').lower()
        if hostname == 'facebook.com' or hostname.endswith('.facebook.com'):
            suffix = parsed.path.lstrip('/')
            if parsed.query:
                suffix += f'?{parsed.query}'
            return f'{WWWFB}/{suffix}'
        return value
    return f'{WWWFB}/{value.removeprefix("/")}'
