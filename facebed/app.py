import json
import logging
import re
from functools import wraps

import crawleruseragents
from bottle import Bottle, request, response, static_file

from facebed import media_store
from facebed.constants import ASSETS_DIR, WWWFB
from facebed.embeds import format_error_message_embed, format_redirect_page
from facebed.errors import NoDataException
from facebed.formatting import get_credit
from facebed.log import readable_log_title
from facebed.metrics import service_metrics
from facebed.notifier import (
    failed_post_type,
    send_dump_report,
    send_restricted_page_report,
)
from facebed.pipeline import record_scrape_error, run_tracked_request

logger = logging.getLogger(__name__)

app: Bottle = Bottle()


@app.route('/m/<key:re:[rwf]?[0-9]{1,25}>/<slot:re:(a|qa|q?[0-9])>.<ext:re:(jpg|mp4)>')
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


@app.route('/<path:path>')
def index(path: str):
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
        return format_redirect_page(f'{WWWFB}/{path}')

    selected_response = None
    try:
        result, selected_response, parser_entered = run_tracked_request(path)
        if not parser_entered:
            service_metrics.record_request()
    except Exception as exc:
        record_scrape_error(exc)
        selected_response = getattr(exc, 'selected_response', None)
        if selected_response is None:
            selected_response = getattr(exc, 'upstream_response', None)
        reason = getattr(exc, 'reason_code', 'internal_error')
        logger.warning(
            'embed fallback type=%s reason=%s url=%s detail=%s',
            failed_post_type(original_path, selected_response, reason),
            reason,
            f'{WWWFB}/{original_path}',
            ' '.join(str(exc).split()),
        )
        if isinstance(exc, NoDataException):
            send_restricted_page_report(original_path, exc, selected_response)
        result = format_error_message_embed(f'{WWWFB}/{original_path}')

    if dump_requested:
        send_dump_report(path, selected_response)
        response.headers['Cache-Control'] = 'no-store'
    return result


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


def log_to_logger(fn):
    @wraps(fn)
    def _log_to_logger(*argsz, **kwargs):
        actual_response = fn(*argsz, **kwargs)
        title = readable_log_title(request.environ.get('facebed.post_title', ''))
        logger.info(
            '%s %s %s %s%s', request.remote_addr, request.method,
            request.url, response.status, f' {title}' if title else '',
        )
        return actual_response

    return _log_to_logger
