import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import quote as _quote_
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from curl_cffi.requests import Response as CffiResponse
from discord_webhook import DiscordEmbed, DiscordWebhook
from yattag import indent

from facebed.config import get_config
from facebed.constants import (
    FACEBOOK_REACTION_EMOJIS,
    FACEBOOK_REACTION_NAME_IDS,
    SHARE_BODY_USER_AGENT,
    SHARE_HEAD_USER_AGENT,
)
from facebed.errors import (
    NoDataException,
    ShareResolutionException,
    UnsupportedRouteException,
    UpstreamException,
)


def quote(s: str) -> str:
    return "".join([
        _quote_(char) if char in r"<>\"'#%{}[]|\\^~`" else char
        for char in s
    ])

def get_credit() -> str:
    return 'facebed by pi.kt'


class Utils:
    @staticmethod
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

    @staticmethod
    def facebook_path_segments(url_or_path: str) -> tuple[str, ...]:
        value = str(url_or_path)
        parsed = urlparse(value)
        if parsed.netloc:
            hostname = (parsed.hostname or '').lower()
            if hostname != 'facebook.com' and not hostname.endswith('.facebook.com'):
                return ()
            path = parsed.path
        else:
            normalized = Utils.normalize_facebook_path(value)
            parsed_normalized = urlparse('/' + normalized.lstrip('/'))
            path = parsed_normalized.path
        return tuple(part for part in path.split('/') if part)

    @staticmethod
    def is_facebook_url(url: str, *, require_https: bool = False) -> bool:
        parsed = urlparse(str(url))
        hostname = (parsed.hostname or '').lower()
        if hostname != 'facebook.com' and not hostname.endswith('.facebook.com'):
            return False
        if require_https and parsed.scheme.lower() != 'https':
            return False
        return parsed.scheme.lower() in {'http', 'https'}

    @staticmethod
    def is_safe_media_url(url: str) -> bool:
        parsed = urlparse(str(url))
        return bool(
            parsed.scheme.lower() == 'https'
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
        )

    @staticmethod
    def is_control_path(url_or_path: str, *names: str) -> bool:
        parts = Utils.facebook_path_segments(url_or_path)
        if not parts:
            return False
        first = parts[0].casefold().removesuffix('.php')
        return first in {str(name).casefold().removesuffix('.php') for name in names}

    @staticmethod
    def is_post_like_path(url_or_path: str) -> bool:
        value = Utils.normalize_facebook_path(url_or_path)
        path = urlparse('/' + value.lstrip('/')).path
        return bool(
            re.search(
                r'/(?:posts|permalink|reel|watch|videos|v|photos)(?:/|$)',
                '/' + path.lstrip('/'),
                re.IGNORECASE,
            )
            or re.match(r'^/?(?:story|permalink|photo)\.php/?$', path, re.IGNORECASE)
        )

    @staticmethod
    def is_share_path(path: str) -> bool:
        return bool(re.match(r'^/?share(?:/|$)', urlparse(path).path, re.IGNORECASE))

    @staticmethod
    def is_group_landing_target(path: str) -> bool:
        from facebed.parsers.json_parser import JsonParser  # circular import

        normalized_path = urlparse(JsonParser.ensure_full_url(path)).path.strip('/')
        return normalized_path.startswith('groups/') and (
            normalized_path.endswith('/about')
            or len(normalized_path.split('/')) <= 2
        )

    @staticmethod
    def resolve_share_link(path: str) -> tuple[str, CffiResponse | None]:
        from facebed.client import cffi  # circular import
        from facebed.parsers.json_parser import JsonParser  # circular import

        with cffi.request_scope():
            try:
                resolved_path, prefetched_response = Utils._resolve_share_link(path)
                resolved_url = JsonParser.ensure_full_url(resolved_path)
                if not urlparse(resolved_url).path.strip('/'):
                    raise ShareResolutionException(
                        'Facebook share resolution did not produce a post target'
                    )
                return resolved_path, prefetched_response
            except ShareResolutionException:
                raise
            except UnsupportedRouteException:
                raise
            except NoDataException as exc:
                raise ShareResolutionException(
                    str(exc),
                    getattr(exc, 'upstream_response', None),
                    count_as_error=False,
                    reason_code=getattr(exc, 'reason_code', 'share_resolution_failed'),
                    terminal=getattr(exc, 'terminal', False),
                ) from exc

    @staticmethod
    def _resolve_share_link(path: str) -> tuple[str, CffiResponse | None]:
        from facebed.client import cffi  # circular import
        from facebed.parsers.json_parser import JsonParser  # circular import

        source_path = Utils.normalize_facebook_path(path)
        url = JsonParser.ensure_full_url(source_path)
        source_parts = Utils.facebook_path_segments(source_path)
        is_share_v = (
            len(source_parts) >= 2
            and source_parts[0].casefold() == 'share'
            and source_parts[1].casefold() == 'v'
        )
        logging.info(f'resolving share link {url}')
        head_response = None
        try:
            head_response = cffi.head(
                url,
                headers={'User-Agent': SHARE_HEAD_USER_AGENT},
                _check_status=False,
            )
        except UpstreamException as exc:
            logging.warning('share HEAD failed for %s: %s', url, exc)

        needs_get = head_response is None
        if head_response is not None:
            head_url = str(head_response.url)
            head_path = Utils.normalize_facebook_path(head_url)
            parsed_head_url = urlparse(head_url)
            head_hostname = (parsed_head_url.hostname or '').lower()
            head_is_facebook = (
                head_hostname == 'facebook.com'
                or head_hostname.endswith('.facebook.com')
            )
            head_is_group_landing = Utils.is_group_landing_target(head_path)
            head_is_post_like = Utils.is_post_like_path(head_path)
            head_parts = Utils.facebook_path_segments(head_path)
            head_is_video_like = bool(
                head_parts
                and (
                    head_parts[0].casefold() in {'watch', 'reel'}
                    or any(part.casefold() == 'videos' for part in head_parts)
                    or (
                        head_parts[0].casefold() == 'groups'
                        and any(
                            part.casefold() in {'posts', 'permalink'}
                            for part in head_parts
                        )
                    )
                )
            )
            head_usable = (
                head_is_facebook
                and not head_is_group_landing
                and head_is_post_like
                and (not is_share_v or head_is_video_like)
            )
            needs_get = (
                head_response.status_code >= 400
                or Utils.is_share_path(head_path)
                or Utils.is_control_path(head_url, 'login', 'checkpoint', 'recover')
                or not head_usable
            )
            if not needs_get:
                logging.info(f'resolved to {head_response.url}')
                return head_path, None

        def inspect_direct(response: CffiResponse):
            response_url = str(response.url)
            parsed_response_url = urlparse(response_url)
            response_host = (parsed_response_url.hostname or '').lower()
            response_is_facebook = (
                response_host == 'facebook.com'
                or response_host.endswith('.facebook.com')
            )
            direct_url = response_url
            html_parser = BeautifulSoup(response.text, 'html.parser')
            canonical_candidates = []
            canonical_link = html_parser.select_one('link[rel="canonical"]')
            if canonical_link and canonical_link.get('href'):
                canonical_candidates.append(str(canonical_link['href']))
            for canonical_meta in html_parser.select('meta[property="og:url"]'):
                if canonical_meta and canonical_meta.get('content'):
                    canonical_candidates.append(str(canonical_meta['content']))
            declared_target = False
            for canonical_url in canonical_candidates:
                candidate_url = JsonParser.ensure_full_url(canonical_url)
                candidate_path = Utils.normalize_facebook_path(candidate_url)
                parsed_candidate = urlparse(candidate_url)
                candidate_host = (parsed_candidate.hostname or '').lower()
                candidate_is_facebook = (
                    candidate_host == 'facebook.com'
                    or candidate_host.endswith('.facebook.com')
                )
                candidate_path_only = parsed_candidate.path.strip('/')
                candidate_first_segment = candidate_path_only.partition('/')[0].lower()
                candidate_page = candidate_first_segment.removesuffix('.php')
                candidate_is_usable = (
                    bool(candidate_path_only)
                    and not Utils.is_share_path(candidate_path)
                    and candidate_page not in {'login', 'checkpoint', 'recover'}
                    and not Utils.is_group_landing_target(candidate_path)
                )
                if candidate_is_facebook and candidate_is_usable:
                    direct_url = candidate_url
                    declared_target = True
                    break
            direct_path = Utils.normalize_facebook_path(direct_url)
            page_type = 'unknown'
            if response_is_facebook:
                page_type = JsonParser.probe_page_type(
                    html_parser,
                    direct_path,
                    response_url=response_url,
                )
                if (
                    page_type == JsonParser.PROBE_UNKNOWN
                    and source_path != direct_path
                ):
                    page_type = JsonParser.probe_page_type(
                        html_parser,
                        source_path,
                        response_url=response_url,
                    )
            return (
                direct_url,
                direct_path,
                page_type,
                declared_target,
                response_is_facebook,
            )

        direct_response = cffi.get(
            url,
            headers={'User-Agent': SHARE_BODY_USER_AGENT},
            _check_status=False,
            _retry_status_responses=False,
        )
        (
            direct_url,
            direct_path,
            page_type,
            declared_target,
            response_is_facebook,
        ) = inspect_direct(direct_response)
        if (
            page_type != 'has_data'
            and direct_response.status_code in cffi.retry_statuses
        ):
            direct_response = cffi.get(
                url,
                headers={'User-Agent': SHARE_BODY_USER_AGENT},
                _check_status=False,
                _retry_status_responses=False,
                _bypass_cache=True,
            )
            (
                direct_url,
                direct_path,
                page_type,
                declared_target,
                response_is_facebook,
            ) = inspect_direct(direct_response)
        logging.info(f'resolved to {direct_url}')
        if not response_is_facebook:
            if declared_target:
                if Utils.is_group_landing_target(direct_path):
                    raise NoDataException(
                        'Facebook resolved share link to a group landing page'
                    )
                return direct_path, None
            raise NoDataException('Facebook redirected share link outside Facebook')
        if page_type in {
            JsonParser.PROBE_LOGIN_WALL,
            JsonParser.PROBE_PRIVATE_GROUP,
        }:
            JsonParser.raise_for_probe_result(page_type, direct_path)
        if direct_response.status_code >= 400 and page_type != 'has_data':
            raise UpstreamException(
                f'Facebook returned HTTP {direct_response.status_code} for {url}', direct_response
            )
        JsonParser.raise_for_probe_result(page_type, direct_path)
        if Utils.is_share_path(direct_path):
            if page_type == 'has_data':
                return source_path, direct_response
            raise NoDataException(
                'Facebook left the share URL unresolved without post data',
                reason_code='share_target_unresolved',
            )
        if Utils.is_control_path(
            str(direct_response.url), 'login', 'checkpoint', 'recover'
        ):
            if page_type == 'has_data':
                return source_path, direct_response
            raise NoDataException(
                'Facebook redirected share link to authentication',
                reason_code='login_wall',
            )

        if Utils.is_group_landing_target(direct_path):
            raise NoDataException(
                'Facebook resolved share link to a group landing page',
                reason_code='private_group_post_unavailable',
            )
        if (
            page_type == JsonParser.PROBE_UNKNOWN
            and declared_target
            and direct_path != Utils.normalize_facebook_path(str(direct_response.url))
        ):
            # Metadata can identify a post without containing its payload.
            # Let the parser fetch that target instead of reusing the share page.
            return direct_path, None
        return (
            direct_path,
            direct_response
            if page_type in {
                JsonParser.PROBE_HAS_DATA,
                JsonParser.PROBE_UNKNOWN,
            }
            else None,
        )

    @staticmethod
    def prettify(txt: str) -> str:
        return indent(txt, indentation ='    ', newline = '\n', indent_text = True)

    @staticmethod
    def warn(msg: str = None, file_content: bytes = None, filename: str = None, embed: DiscordEmbed = None):
        def worker():
            wh = get_config().notifier_webhook
            if not wh or not wh.startswith('https://discord.com/api/webhooks/'):
                return
            try:
                webhook = DiscordWebhook(
                    url=wh,
                    content=msg,
                    rate_limit_retry=True,
                    timeout=30,
                )
                if embed:
                    webhook.add_embed(embed)
                if file_content is not None and filename:
                    webhook.add_file(file=file_content, filename=filename)
                webhook.execute()
            except Exception:
                logging.warning(f"couldn't warn about {msg or embed}")

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def timestamp_to_str(ts: int) -> str:
        if ts < 0:
            return ''
        dt = datetime.fromtimestamp(ts, timezone(timedelta(hours=get_config().timezone)))
        tztext = dt.strftime('%z')[:3]
        return '⌚ ' + dt.strftime('%Y/%m/%d %H:%M:%S ') + f'UTC{tztext}'

    @staticmethod
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

    @staticmethod
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

    @staticmethod
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
