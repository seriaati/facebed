import json
import logging
import re
from functools import wraps

import crawleruseragents
from bottle import Bottle, request, response, static_file

from facebed import media_store
from facebed.constants import ASSETS_DIR, WWWFB
from facebed.embeds import format_error_message_embed, format_full_post_embed, format_redirect_page
from facebed.errors import FacebedException, NoDataException
from facebed.formatting import get_credit
from facebed.log import readable_log_title
from facebed.metrics import service_metrics
from facebed.models.post import ParsedPost
from facebed.notifier import (
    failed_post_type,
    send_dump_report,
    send_restricted_page_report,
)
from facebed.pipeline import record_scrape_error, run_tracked_request

logger = logging.getLogger(__name__)

app: Bottle = Bottle()


def log_access(title: str = '') -> None:
    title = readable_log_title(title)
    logger.info(
        '%s %s %s %s%s', request.remote_addr, request.method,
        request.url, response.status, f' {title}' if title else '',
    )


def log_to_logger(fn):
    @wraps(fn)
    def _log_to_logger(*argsz, **kwargs):
        actual_response = fn(*argsz, **kwargs)
        log_access()
        return actual_response

    return _log_to_logger


def _successful_embed(parsed_post: ParsedPost) -> str:
    response.status = 200
    response.headers['Cache-Control'] = 'public, max-age=900'
    return format_full_post_embed(parsed_post)


@app.route('/m/<key:re:[rwfm]?[0-9]{1,25}>/<slot:re:(a|qa|q?[0-9])>.<ext:re:(jpg|mp4)>')
def media_redirect(key: str, slot: str, ext: str):
    """Short media URLs for component embeds; the real (signed, ~1 KB) CDN URL would blow the byte cap."""
    try:
        url = media_store.resolve(key, slot)
    except Exception:
        logger.warning('media refetch failed for /m/%s/%s.%s', key, slot, ext, exc_info=True)
        url = ''
    if not url:
        response.status = 404
        return ''
    response.status = 302
    response.headers['Location'] = url
    return ''


@app.route('/healthz')
def healthz():
    uptime_secs, request_count, error_count = service_metrics.snapshot()
    payload = {
        'status': 'ok',
        'uptime_secs': uptime_secs,
        'requests': request_count,
        'errors': error_count,
    }
    response.content_type = 'application/json'
    response.headers['Cache-Control'] = 'no-store'
    return json.dumps(payload)


# logs its own access line, with the post title, instead of going through log_to_logger
@app.route('/<path:path>', skip=[log_to_logger])
def index(path: str):
    body, title = _embed_for_path(path)
    log_access(title)
    return body


def _embed_for_path(path: str) -> tuple[str, str]:
    dump_requested = bool(re.search(r'(?:^|/)dump/?$', path, re.IGNORECASE))
    if dump_requested:
        path = re.sub(r'(?:^|/)dump/?$', '', path, flags=re.IGNORECASE)
    path = path.rstrip('/')
    if request.query_string:
        path = f'{path}?{request.query_string}'
    original_path = path

    response.headers['Vary'] = 'User-Agent'
    response.headers['Cache-Control'] = 'no-store'
    if not crawleruseragents.is_crawler(
        request.headers.get('User-Agent', ''), case_sensitive=False
    ):
        response.status = 302
        response.headers['Location'] = f'{WWWFB}/{path}'
        return format_redirect_page(f'{WWWFB}/{path}'), ''

    title = ''
    outcome = run_tracked_request(path)
    if outcome.error is None:
        if not outcome.parser_entered:
            service_metrics.record_request()
        result = _successful_embed(outcome.post)
        title = outcome.post.author_name
    else:
        error = outcome.error
        record_scrape_error(error, outcome.parser_entered)
        expected = isinstance(error, FacebedException)
        reason = error.reason_code if expected else 'internal_error'
        logger.warning(
            'embed fallback type=%s reason=%s url=%s detail=%s',
            failed_post_type(original_path, outcome.selected_response, reason),
            reason,
            f'{WWWFB}/{original_path}',
            ' '.join(str(error).split()),
            exc_info=None if expected else error,  # anything else is a bug, keep its traceback
        )
        if isinstance(error, NoDataException):
            send_restricted_page_report(original_path, error, outcome.selected_response)
        result = format_error_message_embed(f'{WWWFB}/{original_path}')

    if dump_requested:
        send_dump_report(path, outcome.selected_response)
        response.headers['Cache-Control'] = 'no-store'
    return result, title


@app.route('/favicon.ico')
def favicon():
    response.content_type = 'image/x-icon'
    return static_file('favicon.ico', root=str(ASSETS_DIR))


@app.route('/banner.png')
def banner():
    response.content_type = 'image/png'
    return static_file('banner.png', root=str(ASSETS_DIR))


@app.route('/')
def root():
    with (ASSETS_DIR / 'index.html').open(encoding='utf-8') as f:
        return f.read().replace('{|CREDIT|}', get_credit())
