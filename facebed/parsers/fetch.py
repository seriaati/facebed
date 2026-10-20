import logging
from contextlib import contextmanager

from bs4 import BeautifulSoup
from curl_cffi.requests import Response as CffiResponse

from facebed.client import cffi
from facebed.config import get_config
from facebed.cookies import acc
from facebed.errors import ParseException, UnsupportedRouteException, UpstreamException
from facebed.parsers.probe import (
    PROBE_HAS_DATA,
    PROBE_LOGIN_WALL,
    PROBE_PRIVATE_GROUP,
    PROBE_UNSUPPORTED_MARKETPLACE,
    PROBE_UNSUPPORTED_MEDIA_SET,
    PROBE_UNSUPPORTED_PROFILE,
    probe_page_type,
    raise_for_probe_result,
)
from facebed.urls import ensure_full_url, is_facebook_url


@contextmanager
def fetch_page(
    post_path: str,
    http_response: CffiResponse | None = None,
):
    url = ensure_full_url(post_path)
    owns_response = http_response is None
    if owns_response:
        http_response = cffi.get(
            url,
            _check_status=False,
            _retry_status_responses=False,
        )

    def prepare_page(current_response: CffiResponse):
        cffi.select_response(current_response)
        current_url = str(getattr(current_response, 'url', '') or '')
        if current_url and not is_facebook_url(
            current_url, require_https=True
        ):
            raise UnsupportedRouteException(
                'Facebook redirected outside Facebook',
                reason_code='offsite_redirect',
            )
        current_html = current_response.text
        current_parser = BeautifulSoup(current_html, 'html.parser')
        page_type = probe_page_type(
            current_parser,
            post_path,
            response_url=current_url,
        )
        return current_html, current_parser, page_type

    raw_html, html_parser, page_type = prepare_page(http_response)
    if (
        owns_response
        and http_response.status_code in cffi.retry_statuses
        and page_type != PROBE_HAS_DATA
    ):
        http_response = cffi.get(
            url,
            _check_status=False,
            _retry_status_responses=False,
            _bypass_cache=True,
        )
        raw_html, html_parser, page_type = prepare_page(http_response)
    # tried logged out first so the account is only used when actually needed
    if page_type not in {
        PROBE_HAS_DATA,
        PROBE_UNSUPPORTED_PROFILE,
        PROBE_UNSUPPORTED_MARKETPLACE,
        PROBE_UNSUPPORTED_MEDIA_SET,
    }:
        cookies = acc.get_cookies()
        if cookies:
            logging.info(f'no data logged out for {post_path}, retrying with cookies')
            # cookies are tied to the browser that issued them, so match its UA
            headers = {'user-agent': get_config().user_agent} if get_config().user_agent else {}
            http_response = cffi.get(
                url,
                cookies=cookies,
                headers=headers,
                _check_status=False,
                _retry_status_responses=False,
            )
            raw_html, html_parser, page_type = prepare_page(http_response)
    if page_type in {
        PROBE_LOGIN_WALL,
        PROBE_PRIVATE_GROUP,
    }:
        raise_for_probe_result(page_type, post_path)
    if (
        http_response.status_code >= 400
        and page_type != PROBE_HAS_DATA
    ):
        raise UpstreamException(
            f'Facebook returned HTTP {http_response.status_code} for {url}',
            http_response,
        )
    raise_for_probe_result(page_type, post_path)
    try:
        yield html_parser
    except ParseException as e:
        if not e.html:
            e.html = raw_html
            e.url = url
        raise
