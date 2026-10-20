
from bs4 import BeautifulSoup

from facebed import jq
from facebed.client import cffi
from facebed.errors import NoDataException, UnsupportedRouteException
from facebed.parsers.blocks import (
    get_declared_urls,
    get_initial_routes,
    get_json_blocks,
)
from facebed.parsers.identity import (
    contains_exact_id,
    direct_video_identity_ids,
    direct_video_url,
    get_requested_ids,
    get_target_ids,
    has_bounded_target_payload,
    has_target_video_delivery,
)
from facebed.urls import (
    facebook_path_segments,
    is_control_path,
    is_facebook_url,
    is_post_like_path,
)

PROBE_HAS_DATA = 'has_data'
PROBE_UNKNOWN = 'unknown'
PROBE_NO_DATA = 'no_data'
PROBE_LOGIN_WALL = 'login_wall'
PROBE_TARGET_NOT_FOUND = 'target_not_found'
PROBE_TARGET_IDENTITY_MISMATCH = 'target_identity_mismatch'
PROBE_PRIVATE_GROUP = 'private_group_post_unavailable'
PROBE_TARGET_VIDEO_MISSING = 'target_video_payload_missing'
PROBE_UNSUPPORTED_PROFILE = 'unsupported_profile_target'
PROBE_UNSUPPORTED_MARKETPLACE = 'unsupported_marketplace_target'
PROBE_UNSUPPORTED_MEDIA_SET = 'unsupported_media_set_target'


def probe_page_type(
    html_parser: BeautifulSoup,
    post_path: str = '',
    response_url: str | None = None,
) -> str:
    blocks = get_json_blocks(html_parser)
    routes = get_initial_routes(blocks)
    route_names = {
        str(route.get('canonicalRouteName', '')) for route in routes
    }
    if response_url is None and cffi.selected_get_response is not None:
        response_url = str(
            getattr(cffi.selected_get_response, 'url', '') or ''
        )
    response_url = str(response_url or '')
    raw_ids = get_requested_ids(post_path)
    target_ids, route_ids = get_target_ids(
        html_parser, post_path, response_url=response_url
    )
    required_ids = route_ids or raw_ids or target_ids
    canonical_urls = get_declared_urls(
        html_parser, property_name='canonical'
    )
    og_urls = get_declared_urls(
        html_parser, property_name='og:url'
    )

    unsupported_routes = {
        'comet.fbweb.CometProfilePlusLoggedOutRoute':
            PROBE_UNSUPPORTED_PROFILE,
        'comet.fbweb.CometProfileLoggedOutRoute':
            PROBE_UNSUPPORTED_PROFILE,
        'comet.fbweb.CometMarketplaceHoistedPermalinkRoute':
            PROBE_UNSUPPORTED_MARKETPLACE,
        'comet.fbweb.CometProfileLegacyAlbumViewRoute':
            PROBE_UNSUPPORTED_MEDIA_SET,
    }
    for route_name, result in unsupported_routes.items():
        if route_name in route_names:
            return result

    def has_private_group_surface() -> bool:
        for block in blocks:
            for group in jq.find_all(block, 'group'):
                if not isinstance(group, dict):
                    continue
                join_states = {
                    str(group.get('viewer_join_state', '')),
                    str(group.get('viewer_forum_join_state', '')),
                }
                privacy = group.get('privacy_info')
                title = jq.find_first(privacy, 'text') if isinstance(privacy, dict) else None
                if (
                    join_states.intersection({
                        'CANNOT_JOIN', 'CANNOT_JOIN_OR_REQUEST'
                    })
                    and title == 'Private group'
                ):
                    return True
        return False

    def reel_content_state(reel_id: str) -> tuple[bool, bool]:
        has_context = False
        has_missing_stub = False
        for block in blocks:
            for story in jq.find_all(block, 'creation_story'):
                if (
                    isinstance(story, dict)
                    and contains_exact_id(story, [reel_id])
                ):
                    has_context = True
            for video in jq.find_all(block, 'video'):
                if (
                    isinstance(video, dict)
                    and reel_id
                    in direct_video_identity_ids(video)
                    and 'creation_story' in video
                    and video.get('creation_story') is None
                ):
                    has_missing_stub = True
            for context in jq.find_all(block, 'short_form_video_context'):
                if not isinstance(context, dict):
                    continue
                for key in ('video', 'playback_video'):
                    video = context.get(key)
                    if (
                        isinstance(video, dict)
                        and reel_id
                        in direct_video_identity_ids(video)
                    ):
                        has_context = True
                shareable_url = context.get('shareable_url')
                if (
                    isinstance(shareable_url, str)
                    and reel_id
                    in get_requested_ids(shareable_url)
                ):
                    has_context = True
        return has_context, has_missing_stub

    def reel_consensus_id() -> str | None:
        reel_routes = [
            route for route in routes
            if route.get('canonicalRouteName')
            == 'comet.fbweb.CometFBReelsRoute'
        ]
        if len(reel_routes) != 1:
            return None
        route = reel_routes[0]
        route_ids = set(get_requested_ids(str(route.get('url', ''))))
        params = route.get('params')
        param_id = (
            str(params.get('video_id'))
            if isinstance(params, dict) and params.get('video_id') else ''
        )
        if len(route_ids) != 1 or not param_id or route_ids != {param_id}:
            return None
        requested_id_set = set(target_ids)  # Honor aliases proved by get_target_ids.
        selected_id_set = set(get_requested_ids(response_url))
        if (
            (requested_id_set and requested_id_set != {param_id})
            or (selected_id_set and selected_id_set != {param_id})
            or not (requested_id_set or selected_id_set)
        ):
            return None
        # A complete target-bound story can identify a reel with stale SEO tags.
        for block in blocks:
            for story in jq.find_all(block, 'creation_story'):
                if not isinstance(story, dict):
                    continue
                context = story.get('short_form_video_context')
                if not isinstance(context, dict):
                    continue
                video = context.get('video')
                playback = context.get('playback_video')
                if (
                    str(story.get('post_id', '')) == param_id
                    and direct_video_identity_ids(video) == {param_id}
                    and direct_video_identity_ids(playback) == {param_id}
                    and direct_video_url(playback)
                ):
                    return param_id

        if not canonical_urls or not og_urls:
            return None
        for urls in (canonical_urls, og_urls):
            if not all(
                is_facebook_url(url, require_https=True)
                and set(get_requested_ids(url)) == {param_id}
                for url in urls
            ):
                return None
        return param_id

    declared_urls = [*canonical_urls, *og_urls]
    auth_wall = bool(
        _authentication_wall_reason(response_url, html_parser)
        or (
            declared_urls
            and all(
                is_control_path(
                    url, 'login', 'checkpoint', 'recover'
                )
                for url in declared_urls
            )
        )
    )

    if 'comet.fbweb.CometVideoHomeVideoNotFoundRoute' in route_names:
        return PROBE_TARGET_NOT_FOUND

    if 'comet.fbweb.CometGroupAboutRoute' in route_names:
        canonical_about = any(
            len(facebook_path_segments(url)) >= 3
            and facebook_path_segments(url)[0].casefold() == 'groups'
            and facebook_path_segments(url)[-1].casefold() == 'about'
            for url in canonical_urls
        )
        if (
            any(
                is_post_like_path(path)
                for path in (post_path, response_url) if path
            )
            and canonical_about
            and has_private_group_surface()
            and not has_bounded_target_payload(
                blocks, required_ids
            )
        ):
            return PROBE_PRIVATE_GROUP

    if (
        'comet.fbweb.CometVideoHomeLOEVideoPermalinkRoute' in route_names
        and not route_ids
        and (
            raw_ids
            or is_post_like_path(post_path)
        )
    ):
        return PROBE_TARGET_IDENTITY_MISMATCH

    if 'comet.fbweb.CometFBReelsRoute' in route_names:
        reel_id = reel_consensus_id()
        has_context, has_missing_stub = (
            reel_content_state(reel_id) if reel_id else (False, False)
        )
        has_delivery = bool(
            reel_id
            and has_target_video_delivery(blocks, [reel_id])
        )
        if has_context and has_delivery:
            return PROBE_HAS_DATA
        if auth_wall:
            return PROBE_LOGIN_WALL
        if has_missing_stub and not has_context and not has_delivery:
            return PROBE_TARGET_VIDEO_MISSING
        if (
            not reel_id
            and is_post_like_path(post_path)
            and (
                route_ids
                or raw_ids
                or get_requested_ids(response_url)
            )
        ):
            return PROBE_TARGET_IDENTITY_MISMATCH

    if has_bounded_target_payload(blocks, required_ids):
        return PROBE_HAS_DATA
    if auth_wall:
        return PROBE_LOGIN_WALL
    return PROBE_UNKNOWN


def raise_for_probe_result(page_type: str, post_path: str) -> None:
    unsupported_messages = {
        PROBE_UNSUPPORTED_PROFILE:
            'Facebook share target is a profile rather than a post',
        PROBE_UNSUPPORTED_MARKETPLACE:
            'Facebook share target is a Marketplace listing',
        PROBE_UNSUPPORTED_MEDIA_SET:
            'Facebook share target is a media-set album',
    }
    if page_type in unsupported_messages:
        raise UnsupportedRouteException(
            unsupported_messages[page_type], reason_code=page_type
        )
    messages = {
        PROBE_LOGIN_WALL:
            f'Facebook served a login wall for {post_path}',
        PROBE_PRIVATE_GROUP:
            f'Facebook served a private group landing without the requested post for {post_path}',
        PROBE_TARGET_VIDEO_MISSING:
            f'Facebook response has no target-local playable video for {post_path}',
        PROBE_TARGET_NOT_FOUND:
            f'Facebook reported that the requested target was not found for {post_path}',
        PROBE_TARGET_IDENTITY_MISMATCH:
            f'Facebook route identities disagree for {post_path}',
        PROBE_NO_DATA:
            f'Facebook response did not contain target data for {post_path}',
    }
    if page_type in messages:
        raise NoDataException(
            messages[page_type], reason_code=page_type, terminal=True
        )


def _authentication_wall_reason(
    final_url: str,
    html_parser: BeautifulSoup,
) -> str | None:
    if is_control_path(final_url, 'login'):
        return 'login redirect'
    if is_control_path(final_url, 'checkpoint'):
        return 'checkpoint redirect'
    if is_control_path(final_url, 'recover'):
        return 'account recovery redirect'
    route_names = {
        str(route.get('canonicalRouteName', ''))
        for route in get_initial_routes(
            get_json_blocks(html_parser)
        )
    }
    if route_names.intersection({
        'comet.fbweb.CometLoginRoute',
        'comet.fbweb.CometLoginPageRoute',
        'comet.fbweb.CometCheckpointRoute',
        'comet.fbweb.CometRecoveryRoute',
    }):
        return 'authentication route'
    for form in html_parser.find_all('form'):
        if not is_control_path(str(form.get('action', '')), 'login'):
            continue
        inputs = form.find_all('input')
        input_names = {
            str(node.get('name', '')).casefold() for node in inputs
        }
        input_types = {
            str(node.get('type', '')).casefold() for node in inputs
        }
        if 'password' in input_types and input_names.intersection({
            'email', 'pass', 'password'
        }):
            return 'login form'
    return None
