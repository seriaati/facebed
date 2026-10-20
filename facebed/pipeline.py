import re
import threading
from contextlib import contextmanager
from urllib.parse import parse_qs, urlencode, urlparse

from bottle import request, response
from curl_cffi.requests import Response as CffiResponse

from facebed.client import cffi
from facebed.constants import WWWFB
from facebed.embeds import format_full_post_embed
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
from facebed.parsers.comment import CommentParser
from facebed.parsers.json_parser import JsonParser
from facebed.parsers.photo import SinglePhotoParser
from facebed.parsers.photocom import PhotocomParser
from facebed.parsers.reels import ReelsParser
from facebed.parsers.video_watch import VideoWatchParser
from facebed.utils import Utils


def is_facebook_url(url: str) -> bool:
    if urlparse(url).netloc:
        url = Utils.normalize_facebook_path(url)
    wwwfb = f'{WWWFB}/'
    username_pattern = '[a-zA-Z0-9-._]*'  # also covers /watch
    full_url = f'{wwwfb}{url}'
    parsed_url = urlparse(full_url)

    is_group_post = re.match(f'^/groups/{username_pattern}', parsed_url.path)
    is_permalink = parsed_url.path.startswith('/permalink.php')
    is_story = parsed_url.path.startswith('/story.php')
    is_post = re.match(f'/{username_pattern}/posts', parsed_url.path)
    is_photo = parsed_url.path.startswith('/photo')
    is_photo_album = re.match(f'/{username_pattern}/photos(?:/|$)', parsed_url.path)

    return is_permalink or is_post or is_story or is_photo or is_photo_album or is_group_post


def _invoke_parser(parser, post_path: str, http_response: CffiResponse | None = None):
    if http_response is None:
        return parser(post_path)
    return parser(post_path, http_response=http_response)


PARSER_FALLBACK_EXCEPTIONS = (
    NoDataException,
    ParseException,
    KeyError,
    TypeError,
    IndexError,
    ValueError,
    AttributeError,
)
ROUTE_FALLBACK_EXCEPTIONS = PARSER_FALLBACK_EXCEPTIONS + (UpstreamException,)


def _allow_route_upstream_fallback(error: Exception) -> bool:
    if isinstance(error, UnsupportedRouteException):
        return False
    if isinstance(error, NoDataException) and getattr(error, 'terminal', False):
        return False
    return not isinstance(error, UpstreamException) or error.status_code in {
        403, 404, 500, 502, 503, 504,
    }


def _as_parser_error(error: Exception) -> FacebedException:
    if isinstance(error, FacebedException):
        return error
    return ParseException(f'{type(error).__name__}: {error}')


def _prefer_parser_error(first: Exception, second: Exception) -> FacebedException:
    first = _as_parser_error(first)
    second = _as_parser_error(second)
    for error in (first, second):
        if isinstance(error, UnsupportedRouteException):
            return error
    for error in (first, second):
        if (
            isinstance(error, NoDataException)
            and getattr(error, 'reason_code', 'no_data') != 'no_data'
        ):
            return error
    if isinstance(second, ParseException) and second.html:
        return second
    if isinstance(first, ParseException) and first.html:
        return first
    return second


def _parse_with_generic(parser, post_path: str, http_response: CffiResponse | None = None) -> ParsedPost:
    try:
        return _invoke_parser(parser, post_path, http_response)
    except PARSER_FALLBACK_EXCEPTIONS as first_error:
        if not _allow_route_upstream_fallback(first_error):
            raise
        try:
            return _invoke_parser(JsonParser.process_post, post_path, http_response)
        except PARSER_FALLBACK_EXCEPTIONS as second_error:
            raise _prefer_parser_error(first_error, second_error)


def _parse_video_path(post_path: str, http_response: CffiResponse | None = None) -> ParsedPost:
    parsed = urlparse(post_path)
    video_id = JsonParser.get_terminal_video_id(post_path)
    if not video_id:
        search = re.search(
            r'(?:^|/)(?:[^/]+/)?v/(\d+)(?:/|$)',
            parsed.path,
            re.IGNORECASE,
        )
        video_id = search.group(1) if search else None
    if not video_id:
        return _invoke_parser(JsonParser.process_post, post_path, http_response)

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
                JsonParser.process_post, post_path, original_response
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
        final_path = Utils.normalize_facebook_path(str(getattr(http_response, 'url', '')))
        if urlparse(final_path).path.rstrip('/') != urlparse(reel_path).path.rstrip('/'):
            reel_response = None
    try:
        return _invoke_parser(ReelsParser.process_post, reel_path, reel_response)
    except ROUTE_FALLBACK_EXCEPTIONS as reel_error:
        if not _allow_route_upstream_fallback(reel_error):
            raise
        if not slugged_video_path:
            try:
                generic_post = _invoke_parser(
                    JsonParser.process_post, post_path, original_response
                )
                if generic_post.video_links:
                    return generic_post
            except ROUTE_FALLBACK_EXCEPTIONS as error:
                generic_error = error
                if not _allow_route_upstream_fallback(error):
                    raise
        try:
            return _invoke_parser(
                VideoWatchParser.process_post, post_path, original_response
            )
        except ROUTE_FALLBACK_EXCEPTIONS as watch_error:
            if generic_post is not None:
                return generic_post
            raise _prefer_parser_error(
                _prefer_parser_error(reel_error, generic_error), watch_error
            )


def _ensure_facebook_page_target(post_path: str) -> None:
    parsed_url = urlparse(post_path)
    if parsed_url.netloc:
        hostname = (parsed_url.hostname or '').lower()
        if hostname != 'facebook.com' and not hostname.endswith('.facebook.com'):
            raise UnsupportedRouteException('refusing to fetch non-Facebook host')


def _dispatch_post(post_path: str, http_response: CffiResponse | None = None) -> ParsedPost:
    _ensure_facebook_page_target(post_path)
    if CommentParser.get_target(post_path) is not None:
        _mark_parser_pipeline_entry()
        return _invoke_parser(CommentParser.process_post, post_path, http_response)
    parsed_path = urlparse(post_path).path
    if Utils.is_share_path(post_path):
        _mark_parser_pipeline_entry()
        return _invoke_parser(JsonParser.process_post, post_path, http_response)
    if (
        re.search(r'(?:^|/)videos/', parsed_path, re.IGNORECASE)
        or re.search(r'(?:^|/)(?:[^/]+/)?v/\d+(?:/|$)', parsed_path, re.IGNORECASE)
    ):
        _mark_parser_pipeline_entry()
        return _parse_video_path(post_path, http_response)
    if re.match(r'^/?reel/[^/?]+', parsed_path, re.IGNORECASE):
        _mark_parser_pipeline_entry()
        return _parse_with_generic(ReelsParser.process_post, post_path, http_response)
    if re.match(r'^/?photo(?:\.php)?/?$', parsed_path, re.IGNORECASE):
        _mark_parser_pipeline_entry()
        return _parse_with_generic(SinglePhotoParser.process_post, post_path, http_response)
    if re.match(r'^/?watch(?:/|$)', parsed_path, re.IGNORECASE):
        _mark_parser_pipeline_entry()
        return _parse_with_generic(VideoWatchParser.process_post, post_path, http_response)
    if is_facebook_url(post_path):
        _mark_parser_pipeline_entry()
        return _invoke_parser(JsonParser.process_post, post_path, http_response)
    raise UnsupportedRouteException('unsupported Facebook route')


def _successful_embed(parsed_post: ParsedPost) -> str:
    response.status = 200
    response.headers['Cache-Control'] = 'public, max-age=900'
    result = format_full_post_embed(parsed_post)
    request.environ['facebed.post_title'] = parsed_post.author_name
    return result


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


def _run_tracked_request(
    path: str,
) -> tuple[str, CffiResponse | None, bool]:
    with _parser_pipeline_scope() as state:
        try:
            result, selected_response = _run_request(path)
        except Exception as exc:
            setattr(exc, 'parser_pipeline_entered', state['entered'])
            raise
        return result, selected_response, state['entered']


def _record_scrape_error(error: Exception) -> None:
    if isinstance(error, UnsupportedRouteException):
        return
    parser_entered = bool(getattr(error, 'parser_pipeline_entered', False))
    if not parser_entered and isinstance(error, ShareResolutionException):
        if error.count_as_error:
            service_metrics.record_error()
        return
    if not parser_entered:
        service_metrics.record_request()
    service_metrics.record_error()


def _attach_attempt_response(
    error: Exception,
    selected_response: CffiResponse | None,
) -> None:
    if (
        getattr(error, 'upstream_response', None) is None
        and selected_response is not None
    ):
        setattr(error, 'upstream_response', selected_response)
    setattr(error, 'selected_response', selected_response)


def _run_request(path: str) -> tuple[str, CffiResponse | None]:
    _ensure_facebook_page_target(path)
    with cffi.request_scope():
        try:
            result = _handle_facebook_path(path)
        except Exception as exc:
            selected_response = cffi.selected_get_response or cffi.last_get_response
            _attach_attempt_response(exc, selected_response)
            raise
        return result, cffi.selected_get_response or cffi.last_get_response

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


def _handle_facebook_path(path: str) -> str:
    prefetched_response = None
    comment_target = CommentParser.get_target(path)
    if comment_target is None and '3' in request.query.getall('type'):
        try:
            _mark_parser_pipeline_entry()
            return _successful_embed(PhotocomParser.process_post(path))
        except PARSER_FALLBACK_EXCEPTIONS:
            pass

    path = _normalize_request_path(path)
    share_source_path = None
    if Utils.is_share_path(path):
        share_source_path = path
        path, prefetched_response = Utils.resolve_share_link(path)
        if comment_target is not None:
            resolved_target = CommentParser.get_target(path)
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
) -> str:

    try:
        parsed_post = _dispatch_post(path, prefetched_response)
    except ROUTE_FALLBACK_EXCEPTIONS as primary_error:
        if CommentParser.get_target(path) is not None:
            raise
        if not _allow_route_upstream_fallback(primary_error):
            raise
        if share_source_path and share_source_path != path:
            try:
                _mark_parser_pipeline_entry()
                parsed_post = _invoke_parser(
                    JsonParser.process_post,
                    share_source_path,
                    prefetched_response,
                )
            except ROUTE_FALLBACK_EXCEPTIONS as share_error:
                if not _allow_route_upstream_fallback(share_error):
                    raise
                raise _prefer_parser_error(primary_error, share_error)
        else:
            raise
    return _successful_embed(parsed_post)
