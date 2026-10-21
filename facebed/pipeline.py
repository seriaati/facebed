import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import NoReturn
from urllib.parse import parse_qs, urlencode, urlparse

from bottle import request
from curl_cffi.requests import Response as CffiResponse

from facebed.client import cffi
from facebed.errors import (
    FacebedException,
    NoDataException,
    ParseException,
    ShareResolutionException,
    UnsupportedRouteException,
    UpstreamException,
)
from facebed.metrics import service_metrics
from facebed.models.post import ParsedPost
from facebed.parsers import comment, generic, photo, photocom, reels, video_watch
from facebed.parsers.identity import get_terminal_video_id
from facebed.share import resolve_share_link
from facebed.urls import is_facebook_host, is_generic_post_path, is_share_path, normalize_facebook_path


def _invoke_parser(parser, post_path: str, http_response: CffiResponse | None = None):
    if http_response is None:
        return parser(post_path)
    return parser(post_path, http_response=http_response)


PARSER_FALLBACK_EXCEPTIONS = (
    NoDataException,
    ParseException,  # includes payload-shape errors, converted by errors.shape_errors
)
ROUTE_FALLBACK_EXCEPTIONS = (*PARSER_FALLBACK_EXCEPTIONS, UpstreamException)


def _allow_route_upstream_fallback(error: Exception) -> bool:
    if isinstance(error, UnsupportedRouteException):
        return False
    if isinstance(error, NoDataException) and error.terminal:
        return False
    return not isinstance(error, UpstreamException) or error.status_code in {
        403, 404, 500, 502, 503, 504,
    }


def _prefer_parser_error(first: FacebedException, second: FacebedException) -> FacebedException:
    for error in (first, second):
        if isinstance(error, UnsupportedRouteException):
            return error
    for error in (first, second):
        if (
            isinstance(error, NoDataException)
            and error.reason_code != 'no_data'
        ):
            return error
    if isinstance(second, ParseException) and second.html:
        return second
    if isinstance(first, ParseException) and first.html:
        return first
    return second


def _raise_preferred(first: FacebedException, second: FacebedException) -> NoReturn:
    preferred = _prefer_parser_error(first, second)
    # chain the discarded attempt so its traceback isn't lost
    raise preferred from (second if preferred is first else first)


def _parse_with_generic(parser, post_path: str, http_response: CffiResponse | None = None) -> ParsedPost:
    try:
        return _invoke_parser(parser, post_path, http_response)
    except PARSER_FALLBACK_EXCEPTIONS as first_error:
        if not _allow_route_upstream_fallback(first_error):
            raise
        try:
            return _invoke_parser(generic.process_post, post_path, http_response)
        except PARSER_FALLBACK_EXCEPTIONS as second_error:
            _raise_preferred(first_error, second_error)


def _parse_video_path(post_path: str, http_response: CffiResponse | None = None) -> ParsedPost:
    parsed = urlparse(post_path)
    video_id = get_terminal_video_id(post_path)
    if not video_id:
        search = re.search(
            r'(?:^|/)(?:[^/]+/)?v/(\d+)(?:/|$)',
            parsed.path,
            re.IGNORECASE,
        )
        video_id = search.group(1) if search else None
    if not video_id:
        return _invoke_parser(generic.process_post, post_path, http_response)

    path_parts = [part for part in parsed.path.split('/') if part]
    videos_index = next(
        (
            index for index, part in enumerate(path_parts)
            if part.casefold() == 'videos'
        ),
        None,
    )
    video_parts = (
        path_parts[videos_index + 1:]
        if videos_index is not None else []
    )
    slugged_video_path = bool(
        len(video_parts) >= 2
        and not video_parts[0].casefold().startswith('pcb.')
    )

    original_response = http_response
    generic_post = None
    generic_error = None
    if slugged_video_path:
        try:
            generic_post = _invoke_parser(
                generic.process_post, post_path, original_response
            )
            if generic_post.video_links:
                return generic_post
        except ROUTE_FALLBACK_EXCEPTIONS as error:
            generic_error = error
            if not _allow_route_upstream_fallback(error):
                raise

    reel_path = f'reel/{video_id}'
    if parsed.query:
        reel_path += f'?{parsed.query}'
    reel_response = http_response
    if http_response is not None:
        final_path = normalize_facebook_path(str(getattr(http_response, 'url', '')))
        if urlparse(final_path).path.rstrip('/') != urlparse(reel_path).path.rstrip('/'):
            reel_response = None
    try:
        return _invoke_parser(reels.process_post, reel_path, reel_response)
    except ROUTE_FALLBACK_EXCEPTIONS as reel_error:
        if not _allow_route_upstream_fallback(reel_error):
            raise
        if not slugged_video_path:
            try:
                generic_post = _invoke_parser(
                    generic.process_post, post_path, original_response
                )
                if generic_post.video_links:
                    return generic_post
            except ROUTE_FALLBACK_EXCEPTIONS as error:
                generic_error = error
                if not _allow_route_upstream_fallback(error):
                    raise
        try:
            return _invoke_parser(
                video_watch.process_post, post_path, original_response
            )
        except ROUTE_FALLBACK_EXCEPTIONS as watch_error:
            if generic_post is not None:
                return generic_post
            _raise_preferred(_prefer_parser_error(reel_error, generic_error), watch_error)


def _ensure_facebook_page_target(post_path: str) -> None:
    parsed_url = urlparse(post_path)
    if parsed_url.netloc:
        hostname = (parsed_url.hostname or '').lower()
        if not is_facebook_host(hostname):
            raise UnsupportedRouteException('refusing to fetch non-Facebook host')


def _dispatch_post(post_path: str, http_response: CffiResponse | None = None) -> ParsedPost:
    _ensure_facebook_page_target(post_path)
    if comment.get_target(post_path) is not None:
        _mark_parser_pipeline_entry()
        return _invoke_parser(comment.process_post, post_path, http_response)
    parsed_path = urlparse(post_path).path
    if is_share_path(post_path):
        _mark_parser_pipeline_entry()
        return _invoke_parser(generic.process_post, post_path, http_response)
    if (
        re.search(r'(?:^|/)videos/', parsed_path, re.IGNORECASE)
        or re.search(r'(?:^|/)(?:[^/]+/)?v/\d+(?:/|$)', parsed_path, re.IGNORECASE)
    ):
        _mark_parser_pipeline_entry()
        return _parse_video_path(post_path, http_response)
    if re.match(r'^/?reel/[^/?]+', parsed_path, re.IGNORECASE):
        _mark_parser_pipeline_entry()
        return _parse_with_generic(reels.process_post, post_path, http_response)
    if re.match(r'^/?photo(?:\.php)?/?$', parsed_path, re.IGNORECASE):
        _mark_parser_pipeline_entry()
        return _parse_with_generic(photo.process_post, post_path, http_response)
    if re.match(r'^/?watch(?:/|$)', parsed_path, re.IGNORECASE):
        _mark_parser_pipeline_entry()
        return _parse_with_generic(video_watch.process_post, post_path, http_response)
    if is_generic_post_path(post_path):
        _mark_parser_pipeline_entry()
        return _invoke_parser(generic.process_post, post_path, http_response)
    raise UnsupportedRouteException('unsupported Facebook route')


_parser_pipeline_local = threading.local()


@contextmanager
def _parser_pipeline_scope():
    previous = getattr(_parser_pipeline_local, 'state', None)
    state = {'entered': False}
    _parser_pipeline_local.state = state
    try:
        yield state
    finally:
        if previous is None:
            delattr(_parser_pipeline_local, 'state')
        else:
            _parser_pipeline_local.state = previous


def _mark_parser_pipeline_entry() -> None:
    state = getattr(_parser_pipeline_local, 'state', None)
    if state is not None and not state['entered']:
        state['entered'] = True
        service_metrics.record_request()


@dataclass
class RequestOutcome:
    post: ParsedPost | None
    error: Exception | None
    # the upstream page the attempt ended on, for error reports and dumps
    selected_response: CffiResponse | None
    parser_entered: bool


def run_tracked_request(path: str) -> RequestOutcome:
    with _parser_pipeline_scope() as state:
        post, error, selected_response = _run_request(path)
        return RequestOutcome(post, error, selected_response, state['entered'])


def record_scrape_error(error: Exception, parser_entered: bool) -> None:
    if isinstance(error, UnsupportedRouteException):
        return
    if not parser_entered and isinstance(error, ShareResolutionException):
        if error.count_as_error:
            service_metrics.record_error()
        return
    if not parser_entered:
        service_metrics.record_request()
    service_metrics.record_error()


def _run_request(path: str) -> tuple[ParsedPost | None, Exception | None, CffiResponse | None]:
    try:
        _ensure_facebook_page_target(path)
    except UnsupportedRouteException as error:
        return None, error, None
    with cffi.request_scope():
        try:
            parsed_post = _handle_facebook_path(path)
        except Exception as error:  # noqa: BLE001 -- request boundary: any failure gets the fallback embed
            selected_response = cffi.selected_get_response or cffi.last_get_response
            if selected_response is None and isinstance(error, FacebedException):
                selected_response = error.upstream_response
            return None, error, selected_response
        return parsed_post, None, cffi.selected_get_response or cffi.last_get_response


def _normalize_request_path(path: str) -> str:
    parsed = urlparse(path)
    route = parsed.path.strip('/')
    query = parse_qs(parsed.query, keep_blank_values=True)
    if route == 'story.php':
        owners = query.get('id', [])
        stories = query.get('story_fbid', [])
        if (
            len(owners) == len(stories) == 1
            and re.fullmatch(r'[0-9]+', owners[0])
            and re.fullmatch(r'(?:[0-9]+|pfbid[A-Za-z0-9]+)', stories[0])
        ):
            # Legacy story URLs can return login walls for public posts.
            comment_query = {key: query[key] for key in ('comment_id', 'reply_comment_id') if key in query}
            return parsed._replace(
                path=f'/{owners[0]}/posts/{stories[0]}', query=urlencode(comment_query, doseq=True)
            ).geturl().lstrip('/')
    if route == 'watch' and len(query.get('v', [])) == 1:
        video_id = re.fullmatch(r'([0-9]+)[ \t\r\n\f\v/]+', query['v'][0])
        if video_id:
            query['v'] = [video_id[1]]
            return parsed._replace(query=urlencode(query, doseq=True)).geturl()
    return path


def _handle_facebook_path(path: str) -> ParsedPost:
    prefetched_response = None
    comment_target = comment.get_target(path)
    if comment_target is None and '3' in request.query.getall('type'):
        try:
            _mark_parser_pipeline_entry()
            return photocom.process_post(path)
        except PARSER_FALLBACK_EXCEPTIONS:
            pass

    path = _normalize_request_path(path)
    share_source_path = None
    if is_share_path(path):
        share_source_path = path
        path, prefetched_response = resolve_share_link(path)
        if comment_target is not None:
            resolved_target = comment.get_target(path)
            if resolved_target is not None and resolved_target != comment_target:
                raise NoDataException(
                    'Share resolution changed the requested comment',
                    reason_code='comment_identity_mismatch', terminal=True,
                )
            parsed = urlparse(path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            query['comment_id'] = [comment_target[0]]
            if comment_target[1] is not None:
                query['reply_comment_id'] = [comment_target[1]]
            path = parsed._replace(query=urlencode(query, doseq=True)).geturl()

    return _handle_resolved_facebook_path(
        path,
        prefetched_response=prefetched_response,
        share_source_path=share_source_path,
    )


def _handle_resolved_facebook_path(
    path: str,
    prefetched_response: CffiResponse | None = None,
    share_source_path: str | None = None,
) -> ParsedPost:

    try:
        parsed_post = _dispatch_post(path, prefetched_response)
    except ROUTE_FALLBACK_EXCEPTIONS as primary_error:
        if comment.get_target(path) is not None:
            raise
        if not _allow_route_upstream_fallback(primary_error):
            raise
        if share_source_path and share_source_path != path:
            try:
                _mark_parser_pipeline_entry()
                parsed_post = _invoke_parser(
                    generic.process_post,
                    share_source_path,
                    prefetched_response,
                )
            except ROUTE_FALLBACK_EXCEPTIONS as share_error:
                if not _allow_route_upstream_fallback(share_error):
                    raise
                _raise_preferred(primary_error, share_error)
        else:
            raise
    return parsed_post
