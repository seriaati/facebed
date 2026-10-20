import gzip
import hashlib
import logging
import re
import threading
import traceback
from urllib.parse import urlparse

from curl_cffi.requests import Response as CffiResponse
from discord_webhook import DiscordEmbed, DiscordWebhook

from facebed.client import cffi
from facebed.config import get_config
from facebed.errors import (
    NoDataException,
)
from facebed.urls import ensure_full_url, facebook_path_segments, is_control_path


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


def failed_post_type(path: str, upstream_response: CffiResponse | None, reason: str) -> str:
    if reason.startswith('unsupported_') and reason.endswith('_target'):
        return reason.removeprefix('unsupported_').removesuffix('_target').replace('media_set', 'album')
    if reason == 'private_group_post_unavailable':
        return 'group_post'
    if reason == 'target_video_payload_missing':
        return 'reel'
    paths = [path]
    if upstream_response is not None:
        paths.insert(0, str(upstream_response.url))
    for candidate in paths:
        parts = facebook_path_segments(candidate)
        if not parts or is_control_path(candidate, 'login', 'checkpoint', 'recover'):
            continue
        if parts[0] == 'groups':
            return 'group_post'
        if parts[0] == 'share':
            return 'share'
        kinds = {
            'reel': 'reel', 'videos': 'video', 'watch': 'video', 'v': 'video',
            'posts': 'post', 'permalink': 'post', 'story.php': 'post',
            'permalink.php': 'post', 'photo.php': 'photo', 'photo': 'photo',
            'photos': 'photo', 'marketplace': 'marketplace', 'media': 'album',
        }
        if parts[0] in kinds:
            return kinds[parts[0]]
        if len(parts) > 1 and parts[1] in {'posts', 'videos', 'photos', 'v'}:
            return kinds[parts[1]]
        if len(parts) == 1 or parts[0] in {'people', 'p'}:
            return 'profile'
    return 'unknown'


def send_dump_report(
    path: str,
    http_response: CffiResponse | None = None,
) -> None:
    http_response = http_response or cffi.selected_get_response or cffi.last_get_response
    if http_response is None:
        logging.info('no body-bearing GET captured for dump /%s', path)
        return
    try:
        raw_bytes = bytes(http_response.content or b'')
        filename = re.sub(r'[^a-zA-Z0-9]', '_', path)[:80] + '_dump.html'
        display_path = '/' + path.lstrip('/')
        url = ensure_full_url(path)
        embed = DiscordEmbed(
            title='manual dump report',
            description=f'🔗 [`{display_path}`]({url})\n📋 Manual dump requested by user',
            color='3498DB',
        )
        embed.add_embed_field(name='Attached Payload', value=f'`{filename}`', inline=True)
        embed.add_embed_field(name='Response Size', value=f'{len(raw_bytes)} bytes', inline=True)
        embed.add_embed_field(name='Final URL', value=str(getattr(http_response, 'url', url)), inline=True)
        embed.add_embed_field(name='Status', value=str(getattr(http_response, 'status_code', 'unknown')), inline=True)
        warn(file_content=raw_bytes, filename=filename, embed=embed)
        logging.info('dump report sent for /%s', path)
    except Exception:
        logging.error("couldn't dump /%s\n%s", path, traceback.format_exc())


def _discord_text(value: object, limit: int = 1024) -> str:
    text = str(value).replace('\x00', '')
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 1)] + '…'


def _human_bytes(size: int) -> str:
    amount = float(size)
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if amount < 1024 or unit == 'GiB':
            precision = 0 if unit == 'B' else 1
            return f'{amount:.{precision}f} {unit}'
        amount /= 1024
    return f'{size} B'


def _restricted_page_attachment(
    origin_url: str,
    reason: str,
    raw_bytes: bytes,
) -> tuple[bytes, str]:
    digest = hashlib.sha256(raw_bytes).hexdigest()
    origin_name = urlparse(origin_url).path.strip('/') or 'facebook'
    stem = re.sub(r'[^a-zA-Z0-9._-]', '_', origin_name)[:52].strip('._-')
    reason_name = re.sub(r'[^a-zA-Z0-9._-]', '_', reason)[:24].strip('._-')
    filename = '_'.join(filter(None, (stem, reason_name, digest[:12])))
    file_content = raw_bytes
    suffix = '.html'
    if len(raw_bytes) > 7 * 1024 * 1024:
        file_content = gzip.compress(raw_bytes, compresslevel=6, mtime=0)
        suffix += '.gz'
    return file_content, (filename or f'facebook_{digest[:12]}') + suffix


def _upstream_redirect_chain(http_response: CffiResponse) -> str:
    chain = [*(getattr(http_response, 'history', None) or []), http_response]
    return '\n'.join(
        f'`{getattr(item, "status_code", "?")}` {_discord_text(getattr(item, "url", "?"), 850)}'
        for item in chain
    )


def send_restricted_page_report(
    origin_path: str,
    error: NoDataException,
    http_response: CffiResponse | None = None,
) -> None:
    wh = get_config().notifier_webhook
    if not wh or not wh.startswith('https://discord.com/api/webhooks/'):
        return

    try:
        origin_url = ensure_full_url(origin_path)
        reason = str(getattr(error, 'reason_code', 'no_data'))
        title = {
            'login_wall': 'Facebook login wall detected',
            'private_group_post_unavailable': 'Restricted Facebook page detected',
        }.get(reason, 'Facebook content unavailable or restricted')
        embed = DiscordEmbed(
            title=title,
            description=_discord_text(' '.join(str(error).split()), 1500),
            color='ED4245',
        )
        if len(origin_url) <= 2048:
            embed.set_url(origin_url)
        embed.set_timestamp()
        embed.set_footer(text='facebed notifier • exact upstream response attached')
        embed.add_embed_field(
            name='Detection', value=f'`{_discord_text(reason, 120)}`', inline=True
        )
        embed.add_embed_field(
            name='Page type',
            value=f'`{failed_post_type(origin_path, http_response, reason)}`',
            inline=True,
        )
        embed.add_embed_field(
            name='Origin URL', value=_discord_text(origin_url, 900), inline=False
        )

        file_content = None
        filename = None
        if http_response is None:
            embed.add_embed_field(
                name='Upstream response',
                value='No body-bearing upstream response was captured.',
                inline=False,
            )
        else:
            final_url = str(getattr(http_response, 'url', '') or origin_url)
            status = getattr(http_response, 'status_code', 'unknown')
            headers = getattr(http_response, 'headers', {}) or {}
            content_type = (
                headers.get('Content-Type')
                or headers.get('content-type')
                or 'unknown'
            )
            raw_bytes = bytes(getattr(http_response, 'content', b'') or b'')
            file_content, filename = _restricted_page_attachment(
                origin_url, reason, raw_bytes
            )
            embed.add_embed_field(
                name='Upstream response',
                value=(
                    f'HTTP `{status}` • `{_discord_text(content_type, 250)}` • '
                    f'`{_human_bytes(len(raw_bytes))}`'
                ),
                inline=False,
            )
            embed.add_embed_field(
                name='Final upstream URL',
                value=_discord_text(final_url, 900),
                inline=False,
            )
            history = getattr(http_response, 'history', None) or []
            if history:
                embed.add_embed_field(
                    name=f'Redirect chain ({len(history)} hop(s))',
                    value=_discord_text(_upstream_redirect_chain(http_response), 900),
                    inline=False,
                )

        warn(
            file_content=file_content,
            filename=filename,
            embed=embed,
        )
        logging.info(
            'restricted page report queued reason=%s url=%s bytes=%s',
            reason,
            origin_url,
            len(file_content) if file_content is not None else 0,
        )
    except Exception:
        logging.error(
            "couldn't report restricted page /%s\n%s",
            origin_path,
            traceback.format_exc(),
        )
