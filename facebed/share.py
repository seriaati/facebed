import logging
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from curl_cffi.requests import Response as CffiResponse

from facebed.client import cffi
from facebed.constants import SHARE_BODY_USER_AGENT, SHARE_HEAD_USER_AGENT
from facebed.errors import (
    NoDataException,
    ShareResolutionException,
    UpstreamException,
)
from facebed.parsers.probe import (
    PROBE_HAS_DATA,
    PROBE_LOGIN_WALL,
    PROBE_MARKETPLACE,
    PROBE_PRIVATE_GROUP,
    PROBE_UNKNOWN,
    probe_page_type,
    raise_for_probe_result,
)
from facebed.urls import (
    ensure_full_url,
    facebook_path_segments,
    is_control_path,
    is_facebook_host,
    is_group_landing_target,
    is_post_like_path,
    is_share_path,
    normalize_facebook_path,
)

logger = logging.getLogger(__name__)


def resolve_share_link(path: str) -> tuple[str, CffiResponse | None]:
    with cffi.request_scope():
        try:
            resolved_path, prefetched_response = _resolve_share_link(path)
        except ShareResolutionException:
            raise
        except NoDataException as exc:
            raise ShareResolutionException(
                str(exc),
                exc.upstream_response,
                count_as_error=False,
                reason_code=exc.reason_code,
                terminal=exc.terminal,
            ) from exc
        if not urlparse(ensure_full_url(resolved_path)).path.strip('/'):
            raise ShareResolutionException(
                'Facebook share resolution did not produce a post target'
            )
        return resolved_path, prefetched_response


def _resolve_share_link(path: str) -> tuple[str, CffiResponse | None]:
    source_path = normalize_facebook_path(path)
    url = ensure_full_url(source_path)
    source_parts = facebook_path_segments(source_path)
    is_share_v = (
        len(source_parts) >= 2
        and source_parts[0].casefold() == 'share'
        and source_parts[1].casefold() == 'v'
    )
    logger.info('resolving share link %s', url)
    head_response = None
    try:
        head_response = cffi.head(
            url,
            headers={'User-Agent': SHARE_HEAD_USER_AGENT},
            _check_status=False,
        )
    except UpstreamException as exc:
        logger.warning('share HEAD failed for %s: %s', url, exc)

    needs_get = head_response is None
    if head_response is not None:
        head_url = str(head_response.url)
        head_path = normalize_facebook_path(head_url)
        parsed_head_url = urlparse(head_url)
        head_hostname = (parsed_head_url.hostname or '').lower()
        head_is_facebook = is_facebook_host(head_hostname)
        head_is_group_landing = is_group_landing_target(head_path)
        head_is_post_like = is_post_like_path(head_path)
        head_parts = facebook_path_segments(head_path)
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
            or is_share_path(head_path)
            or is_control_path(head_url, 'login', 'checkpoint', 'recover')
            or not head_usable
        )
        if not needs_get:
            logger.info('resolved to %s', head_response.url)
            return head_path, None

    def inspect_direct(response: CffiResponse):
        response_url = str(response.url)
        parsed_response_url = urlparse(response_url)
        response_host = (parsed_response_url.hostname or '').lower()
        response_is_facebook = is_facebook_host(response_host)
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
            candidate_url = ensure_full_url(canonical_url)
            candidate_path = normalize_facebook_path(candidate_url)
            parsed_candidate = urlparse(candidate_url)
            candidate_host = (parsed_candidate.hostname or '').lower()
            candidate_is_facebook = is_facebook_host(candidate_host)
            candidate_path_only = parsed_candidate.path.strip('/')
            candidate_first_segment = candidate_path_only.partition('/')[0].lower()
            candidate_page = candidate_first_segment.removesuffix('.php')
            candidate_is_usable = (
                bool(candidate_path_only)
                and not is_share_path(candidate_path)
                and candidate_page not in {'login', 'checkpoint', 'recover'}
                and not is_group_landing_target(candidate_path)
            )
            if candidate_is_facebook and candidate_is_usable:
                direct_url = candidate_url
                declared_target = True
                break
        direct_path = normalize_facebook_path(direct_url)
        page_type = 'unknown'
        if response_is_facebook:
            page_type = probe_page_type(
                html_parser,
                direct_path,
                response_url=response_url,
            )
            if (
                page_type == PROBE_UNKNOWN
                and source_path != direct_path
            ):
                page_type = probe_page_type(
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
    logger.info('resolved to %s', direct_url)
    if not response_is_facebook:
        if declared_target:
            if is_group_landing_target(direct_path):
                raise NoDataException(
                    'Facebook resolved share link to a group landing page'
                )
            return direct_path, None
        raise NoDataException('Facebook redirected share link outside Facebook')
    if page_type in {
        PROBE_LOGIN_WALL,
        PROBE_PRIVATE_GROUP,
    }:
        raise_for_probe_result(page_type, direct_path)
    if direct_response.status_code >= 400 and page_type != 'has_data':
        raise UpstreamException(
            f'Facebook returned HTTP {direct_response.status_code} for {url}', direct_response
        )
    raise_for_probe_result(page_type, direct_path)
    if is_share_path(direct_path):
        if page_type == 'has_data':
            return source_path, direct_response
        raise NoDataException(
            'Facebook left the share URL unresolved without post data',
            reason_code='share_target_unresolved',
        )
    if is_control_path(
        str(direct_response.url), 'login', 'checkpoint', 'recover'
    ):
        if page_type == 'has_data':
            return source_path, direct_response
        raise NoDataException(
            'Facebook redirected share link to authentication',
            reason_code='login_wall',
        )

    if is_group_landing_target(direct_path):
        raise NoDataException(
            'Facebook resolved share link to a group landing page',
            reason_code='private_group_post_unavailable',
        )
    if (
        page_type == PROBE_UNKNOWN
        and declared_target
        and direct_path != normalize_facebook_path(str(direct_response.url))
    ):
        # Metadata can identify a post without containing its payload.
        # Let the parser fetch that target instead of reusing the share page.
        return direct_path, None
    return (
        direct_path,
        direct_response
        if page_type in {
            PROBE_HAS_DATA,
            PROBE_MARKETPLACE,
            PROBE_UNKNOWN,
        }
        else None,
    )
