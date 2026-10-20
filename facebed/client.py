import logging
import threading
import time
from contextlib import contextmanager
from urllib.parse import urljoin, urlparse

from curl_cffi.requests import RequestsError, Response as CffiResponse, Session as CffiSession

from facebed.config import get_config
from facebed.constants import DEFAULT_FACEBOOK_USER_AGENT
from facebed.errors import UpstreamException
from facebed.urls import is_facebook_url


class CFFI:
    impersonate: str = 'chrome146'
    timeout: tuple[float, float] = (5.0, 20.0)
    retry_statuses: frozenset[int] = frozenset({429, 500, 502, 503, 504})
    max_attempts: int = 2

    def __init__(self) -> None:
        self._local = threading.local()

    @contextmanager
    def request_scope(self):
        if getattr(self._local, 'session', None) is not None:
            yield self._local.session
            return

        session_headers = get_headers()
        session_headers = {
            key: value
            for key, value in session_headers.items()
            if not key.lower().startswith('sec-ch-')
        }
        session_headers['user-agent'] = DEFAULT_FACEBOOK_USER_AGENT
        session = CffiSession(
            impersonate=self.impersonate,
            default_headers=False,
            headers=session_headers,
            discard_cookies=True,
            proxy=get_config().proxy or None,
        )
        self._local.session = session
        self._local.get_cache = {}
        self._local.responses = []
        self._local.last_get_response = None
        self._local.selected_get_response = None
        try:
            yield session
        finally:
            try:
                session.close()
            finally:
                for attr in (
                    'session', 'get_cache', 'responses', 'last_get_response',
                    'selected_get_response',
                ):
                    if hasattr(self._local, attr):
                        delattr(self._local, attr)

    def _get_session(self) -> CffiSession:
        return getattr(self._local, 'session', None)

    @property
    def last_get_response(self) -> CffiResponse | None:
        return getattr(self._local, 'last_get_response', None)

    @property
    def selected_get_response(self) -> CffiResponse | None:
        return getattr(self._local, 'selected_get_response', None)

    def select_response(self, response: CffiResponse | None) -> None:
        if response is not None:
            self._local.selected_get_response = response

    def get(self, url: str, **kwargs) -> CffiResponse:
        return self._request('GET', url, **kwargs)

    def head(self, url: str, **kwargs) -> CffiResponse:
        return self._request('HEAD', url, **kwargs)

    def _request(self, method: str, url: str, **kwargs) -> CffiResponse:
        sess = self._get_session()
        if sess is None:
            with self.request_scope():
                return self._request(method, url, **kwargs)

        method = method.upper()
        check_status = kwargs.pop('_check_status', True)
        retry_status_responses = kwargs.pop('_retry_status_responses', True)
        bypass_cache = kwargs.pop('_bypass_cache', False)
        follow_redirects = kwargs.pop('allow_redirects', True)
        max_redirects = kwargs.pop('max_redirects', 10)
        # account cookies are sent per hop to facebook only; upstream Set-Cookie values are still discarded
        cookies = kwargs.pop('cookies', None)
        headers = {
            key: value
            for key, value in dict(kwargs.pop('headers', {})).items()
            if str(key).lower() != 'cookie'
        }
        kwargs.setdefault('timeout', self.timeout)
        if not isinstance(max_redirects, int):
            raise TypeError('max_redirects must be an integer')
        cache = getattr(self._local, 'get_cache', {})
        def freeze(value):
            if isinstance(value, dict):
                return tuple(sorted((str(key), freeze(item)) for key, item in value.items()))
            if isinstance(value, (list, tuple)):
                return tuple(freeze(item) for item in value)
            if isinstance(value, set):
                return tuple(sorted(freeze(item) for item in value))
            try:
                hash(value)
                return value
            except TypeError:
                return repr(value)

        cache_key = (
            url,
            freeze({
                'headers': headers,
                'allow_redirects': follow_redirects,
                'max_redirects': max_redirects,
                'cookies': cookies,
                **{key: value for key, value in kwargs.items() if key != 'timeout'},
            }),
        )
        if method == 'GET' and not bypass_cache and cache_key in cache:
            cached = cache[cache_key]
            self._local.last_get_response = cached
            if check_status and cached.status_code >= 400:
                raise UpstreamException(
                    f'Facebook returned HTTP {cached.status_code} for {url}', cached
                )
            return cached

        last_error: RequestsError | None = None
        last_response: CffiResponse | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                current_method = method
                current_url = url
                history: list[CffiResponse] = []
                visited_urls = {url}
                while True:
                    request_kwargs = dict(kwargs)
                    if history:
                        request_kwargs.pop('params', None)
                    request_headers = dict(headers)
                    if request_headers:
                        request_kwargs['headers'] = request_headers
                    request_kwargs['allow_redirects'] = False
                    request_kwargs['max_redirects'] = 0
                    if cookies and is_facebook_url(current_url, require_https=True):
                        request_kwargs['cookies'] = cookies

                    sess.cookies.clear()
                    try:
                        upstream_response = sess.request(
                            current_method,
                            current_url,
                            **request_kwargs,
                        )
                    finally:
                        sess.cookies.clear()

                    last_response = upstream_response
                    upstream_response.cookies.clear()
                    content_bytes = (
                        len(upstream_response.content or b'')
                        if current_method == 'GET' else 0
                    )
                    final_url = str(upstream_response.url)
                    status_code = int(upstream_response.status_code)
                    location = upstream_response.headers.get('Location')
                    if current_url != final_url:
                        logging.info(
                            'upstream %s %s -> %s status=%s redirect_hop=%s bytes=%s attempt=%s',
                            current_method,
                            current_url,
                            final_url,
                            status_code,
                            len(history),
                            content_bytes,
                            attempt,
                        )
                    self._local.responses.append({
                        'method': current_method,
                        'requested_url': current_url,
                        'final_url': final_url,
                        'status': status_code,
                        'bytes': content_bytes,
                        'attempt': attempt,
                    })

                    should_redirect = bool(
                        follow_redirects
                        and location
                        and status_code in {301, 302, 303, 307, 308}
                    )
                    if not should_redirect:
                        upstream_response.history = history
                        upstream_response.redirect_count = len(history)
                        break

                    history.append(upstream_response)
                    if max_redirects >= 0 and len(history) > max_redirects:
                        raise UpstreamException(
                            f'Facebook exceeded {max_redirects} redirects for {url}',
                            upstream_response,
                        )
                    next_url = urljoin(final_url, location)
                    parsed_next_url = urlparse(next_url)
                    if (
                        parsed_next_url.scheme.lower() not in {'http', 'https'}
                        or not parsed_next_url.hostname
                        or parsed_next_url.username is not None
                        or parsed_next_url.password is not None
                    ):
                        raise UpstreamException(
                            f'Facebook returned an unsafe redirect target for {url}',
                            upstream_response,
                        )
                    if next_url in visited_urls:
                        raise UpstreamException(
                            f'Facebook entered a redirect loop for {url}',
                            upstream_response,
                        )
                    visited_urls.add(next_url)
                    current_url = next_url
                    if status_code == 303 and current_method != 'HEAD':
                        current_method = 'GET'
                        for key in ('data', 'json', 'files', 'multipart'):
                            kwargs.pop(key, None)

                if method == 'GET':
                    cache[cache_key] = upstream_response
                    self._local.last_get_response = upstream_response

                if (
                    retry_status_responses
                    and status_code in self.retry_statuses
                    and attempt < self.max_attempts
                ):
                    time.sleep(0.25)
                    continue
                if check_status and status_code >= 400:
                    raise UpstreamException(
                        f'Facebook returned HTTP {status_code} for {url}', upstream_response
                    )
                return upstream_response
            except RequestsError as exc:
                last_error = exc
                logging.warning(
                    'upstream %s %s failed on attempt %s/%s: %s',
                    method, url, attempt, self.max_attempts, exc,
                )
                if attempt < self.max_attempts and method in ('GET', 'HEAD'):
                    time.sleep(0.25)
                    continue
                break

        raise UpstreamException(
            f'Facebook request failed for {url}: {last_error}',
            last_response,
            transport_error=True,
        ) from last_error


cffi = CFFI()


def get_headers() -> dict:
    return {
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/jxl,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'accept-language': 'en-US,en;q=0.9',
        'cache-control': 'no-cache',
        'pragma': 'no-cache',
        'priority': 'u=0, i',
        'sec-ch-prefers-color-scheme': 'dark',
        'sec-ch-ua': '"Not)A;Brand";v="8", "Chromium";v="146"',
        'sec-ch-ua-full-version-list': '"Not)A;Brand";v="8.0.0.0", "Chromium";v="146.0.7680.80"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-model': '""',
        'sec-ch-ua-platform': '"Windows"',
        'sec-ch-ua-platform-version': '"19.0.0"',
        'sec-fetch-dest': 'document',
        'sec-fetch-mode': 'navigate',
        'sec-fetch-site': 'none',
        'sec-fetch-user': '?1',
        'sec-gpc': '1',
        'upgrade-insecure-requests': '1',
        'user-agent': DEFAULT_FACEBOOK_USER_AGENT,
    }

