import re
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from facebed import jq
from facebed.client import cffi
from facebed.parsers.blocks import (
    get_declared_urls,
    get_initial_routes,
    get_json_blocks,
)
from facebed.urls import (
    ensure_full_url,
    is_facebook_url,
    is_post_like_path,
    is_safe_media_url,
    is_share_path,
    normalize_facebook_path,
)

IDENTITY_FIELDS = frozenset({
    'id', 'video_id', 'videoid', 'post_id', 'postid', 'story_fbid',
    'storyfbid', 'story_id', 'storyid', 'fbid', 'legacy_fbid',
    'top_level_post_id', 'mf_story_key', 'feedback_id',
})
IDENTITY_URL_FIELDS = frozenset({
    'url', 'wwwurl', 'permalink', 'permalink_url', 'shareable_url',
    'canonical_url',
})
CONTENT_ROUTE_NAMES = frozenset({
    'comet.fbweb.CometSinglePostDialogRoute',
    'comet.fbweb.CometGroupLoggedOutPermalinkRoute',
    'comet.fbweb.CometFBReelsRoute',
    'comet.fbweb.CometVideoHomeLOEVideoPermalinkRoute',
    'comet.fbweb.CometPhotoRoute',
})


def direct_identity_ids(node: dict) -> set[str]:
    identities: set[str] = set()
    if not isinstance(node, dict):
        return identities
    for key, item in node.items():
        key_name = str(key).lower()
        if key_name in IDENTITY_FIELDS:
            if isinstance(item, (str, int)) and str(item):
                identities.add(str(item))
            elif isinstance(item, list):
                identities.update(
                    str(part) for part in item
                    if isinstance(part, (str, int)) and str(part)
                )
        elif key_name in IDENTITY_URL_FIELDS and isinstance(item, str):
            parsed_item = urlparse(item)
            if (
                not parsed_item.netloc
                or is_facebook_url(item, require_https=True)
            ):
                identities.update(get_requested_ids(item))
    return identities


def directly_contains_exact_id(node: dict, requested_ids: list[str]) -> bool:
    return bool(set(map(str, requested_ids)).intersection(
        direct_identity_ids(node)
    ))


def direct_video_identity_ids(node: dict) -> set[str]:
    if not isinstance(node, dict):
        return set()
    identities = {
        str(node[key])
        for key in ('id', 'video_id', 'videoid')
        if isinstance(node.get(key), (str, int)) and str(node[key])
    }
    legacy = node.get('videoDeliveryLegacyFields')
    if isinstance(legacy, dict):
        identities.update(
            str(legacy[key])
            for key in ('id', 'video_id', 'videoid')
            if isinstance(legacy.get(key), (str, int)) and str(legacy[key])
        )
    return identities


def direct_video_url(node: dict) -> str | None:
    if not isinstance(node, dict):
        return None
    for key in ('browser_native_hd_url', 'browser_native_sd_url'):
        value = node.get(key)
        if isinstance(value, str) and is_safe_media_url(value):
            return value
    legacy = node.get('videoDeliveryLegacyFields')
    if isinstance(legacy, dict):
        for key in ('browser_native_hd_url', 'browser_native_sd_url'):
            value = legacy.get(key)
            if isinstance(value, str) and is_safe_media_url(value):
                return value
    return None


def get_video_delivery_nodes(blocks: list[dict]) -> list[dict]:
    nodes: list[dict] = []
    seen: set[int] = set()
    for block in blocks:
        for node in [block, *jq.nodes(block)]:
            if not isinstance(node, dict) or not direct_video_url(node):
                continue
            marker = id(node)
            if marker not in seen:
                seen.add(marker)
                nodes.append(node)
    return nodes


def has_target_video_delivery(blocks: list[dict], target_ids: list[str]) -> bool:
    target_id_set = set(map(str, target_ids))
    return any(
        target_id_set.intersection(
            direct_video_identity_ids(node)
        )
        for node in get_video_delivery_nodes(blocks)
    )


def has_bounded_target_payload(
    blocks: list[dict], target_ids: list[str]
) -> bool:
    if not target_ids:
        return False
    for block in blocks:
        for node in [block, *jq.nodes(block)]:
            if not isinstance(node, dict):
                continue
            if not directly_contains_exact_id(node, target_ids):
                continue
            if direct_video_url(node):
                return True
            if isinstance(node.get('creation_story'), dict):
                return True
            for key in (
                'actors', 'attachments', 'comet_sections', 'content',
                'message', 'owner', 'title', 'image', 'photo_image',
                'container_story', 'attached_comment',
            ):
                value = node.get(key)
                if value not in (None, '', [], {}):
                    return True
    return False


def get_terminal_video_id(post_path: str) -> str | None:
    parsed = urlparse(ensure_full_url(post_path))
    parts = [part for part in parsed.path.split('/') if part]
    videos_index = next(
        (
            index for index, part in enumerate(parts)
            if part.casefold() == 'videos'
        ),
        None,
    )
    if videos_index is None:
        return None
    video_parts = parts[videos_index + 1:]
    if video_parts and re.fullmatch(
        r'&sa=U&ved=[A-Za-z0-9_-]+&usg=[A-Za-z0-9_-]+', video_parts[-1]
    ):
        video_parts = video_parts[:-1]
    if (
        len(video_parts) not in {1, 2}
        or not video_parts[-1].isdigit()
    ):
        return None
    return video_parts[-1]


def get_requested_ids(post_path: str) -> list[str]:
    parsed = urlparse(ensure_full_url(post_path))
    requested_ids = []
    query = parse_qs(parsed.query)
    for key in ('story_fbid', 'fbid', 'v', 'multi_permalinks', 'post_id'):
        requested_ids.extend(query.get(key, []))

    posts_match = re.search(r'/posts/(.+)', parsed.path, re.IGNORECASE)
    if posts_match:
        post_parts = [part for part in posts_match.group(1).split('/') if part]
        post_id = next(
            (part for part in post_parts if part.lower().startswith('pfbid')),
            None,
        )
        if post_id is None:
            post_id = next(
                (part for part in reversed(post_parts) if part.isdigit()),
                post_parts[0] if post_parts else None,
            )
        if post_id:
            requested_ids.append(post_id)

    video_id = get_terminal_video_id(post_path)
    if video_id:
        requested_ids.append(video_id)

    for pattern in (
        r'/reel/([^/?]+)',
        r'/watch/([^/?]+)',
        r'(?<!share)/v/(\d+)(?:/|$)',
        r'/permalink/([^/?]+)',
        r'/photos/(?:[^/]+/)?([^/?]+)/?$',
    ):
        match = re.search(pattern, parsed.path, re.IGNORECASE)
        if match:
            requested_ids.append(match.group(1))
    return list(dict.fromkeys(value for value in requested_ids if value))


def get_target_ids(
    html_parser: BeautifulSoup,
    post_path: str,
    response_url: str | None = None,
) -> tuple[list[str], list[str]]:
    raw_ids = get_requested_ids(post_path)
    route_proven_ids: list[str] = []
    used_post_to_video_alias = False
    source_is_share = is_share_path(post_path)
    blocks = get_json_blocks(html_parser)

    selected_response = cffi.selected_get_response
    if response_url is None and selected_response is not None:
        response_url = str(getattr(selected_response, 'url', '') or '')
    response_url = str(response_url or '')
    response_ids = get_requested_ids(response_url)
    response_history = (
        getattr(selected_response, 'history', None)
        if selected_response is not None else None
    )
    response_was_redirected = bool(response_history)

    canonical_urls = get_declared_urls(
        html_parser, property_name='canonical'
    )
    og_urls = get_declared_urls(
        html_parser, property_name='og:url'
    )

    def add(value, target: list[str]) -> None:
        if isinstance(value, (str, int)) and str(value) and str(value) not in target:
            target.append(str(value))

    def share_key(value: str) -> str:
        normalized = normalize_facebook_path(value)
        return urlparse(ensure_full_url(normalized)).path.rstrip('/')

    source_share_key = share_key(post_path) if source_is_share else ''

    def media_links_source_to_target(candidate_id: str) -> bool:
        for block in blocks:
            for media in jq.find_all(block, 'media'):
                if not isinstance(media, dict):
                    continue
                if candidate_id not in direct_video_identity_ids(
                    media
                ):
                    continue
                creation_story = media.get('creation_story')
                if not isinstance(creation_story, dict):
                    continue
                comet_sections = creation_story.get('comet_sections')
                title = (
                    comet_sections.get('title')
                    if isinstance(comet_sections, dict) else None
                )
                story = title.get('story') if isinstance(title, dict) else None
                post_id = story.get('post_id') if isinstance(story, dict) else None
                if isinstance(post_id, (str, int)) and str(post_id) in raw_ids:
                    return True
        return False

    def reel_links_source_to_target(candidate_id: str) -> bool:
        if len(set(raw_ids)) != 1:
            return False
        for block in blocks:
            for story in jq.find_all(block, 'creation_story'):
                if not isinstance(story, dict):
                    continue
                context = story.get('short_form_video_context')
                if not isinstance(context, dict):
                    continue
                if (
                    str(story.get('post_id', '')) in raw_ids
                    and direct_video_identity_ids(context.get('video'))
                    == {candidate_id}
                    and direct_video_identity_ids(context.get('playback_video'))
                    == {candidate_id}
                    and direct_video_url(context.get('playback_video'))
                ):
                    return True
        return False

    def declared_url_consensus(urls: list[str], target_id: str) -> bool:
        return bool(urls) and all(
            is_facebook_url(url, require_https=True)
            and set(get_requested_ids(url)) == {target_id}
            for url in urls
        )

    def redirect_provenance_matches_source() -> bool:
        if (
            selected_response is None
            or not response_history
            or response_url
            != str(getattr(selected_response, 'url', '') or '')
        ):
            return False
        first_hop_url = str(
            getattr(response_history[0], 'url', '') or ''
        )
        return bool(
            is_facebook_url(first_hop_url, require_https=True)
            and '/posts/' in urlparse(first_hop_url).path.lower()
            and set(raw_ids).intersection(
                get_requested_ids(first_hop_url)
            )
        )

    for route in get_initial_routes(blocks):
        route_name = str(route.get('canonicalRouteName', ''))
        route_url = str(route.get('url', ''))
        if (
            route_name not in CONTENT_ROUTE_NAMES
            and not is_post_like_path(route_url)
        ):
            continue
        route_ids = get_requested_ids(route_url)

        candidate_ids: list[str] = []
        for value in route_ids:
            add(value, candidate_ids)

        params = route.get('params')
        route_share_keys: list[str] = []
        route_param_ids: set[str] = set()
        if isinstance(params, dict):
            for key in ('story_token', 'story_fbid', 'fbid', 'video_id', 'v', 'post_id'):
                add(params.get(key), candidate_ids)
                if params.get(key) not in (None, ''):
                    route_param_ids.add(str(params[key]))
            if params.get('share_url'):
                route_share_keys.append(share_key(str(params['share_url'])))

        route_url_query = parse_qs(urlparse(str(route.get('url', ''))).query)
        for share_url in route_url_query.get('share_url', []):
            route_share_keys.append(share_key(str(share_url)))

        for view_key in ('rootView', 'hostableView'):
            view = route.get(view_key)
            props = view.get('props') if isinstance(view, dict) else None
            if not isinstance(props, dict):
                continue
            for key in ('storyID', 'videoID', 'postID'):
                add(props.get(key), candidate_ids)

        route_matches_request = bool(
            raw_ids and set(raw_ids).intersection(candidate_ids)
        )
        opaque_share_target = bool(
            source_is_share
            and not raw_ids
            and candidate_ids
            and source_share_key in route_share_keys
        )
        final_id_set = {
            value for value in response_ids
            if value.isdigit()
        }
        final_video_id = (
            next(iter(final_id_set))
            if len(final_id_set) == 1 else None
        )
        declared_final_target = bool(
            final_video_id
            and set(route_ids) == {final_video_id}
            and route_param_ids
            and route_param_ids == {final_video_id}
            and declared_url_consensus(canonical_urls, final_video_id)
            and declared_url_consensus(og_urls, final_video_id)
        )
        direct_loe_target = (
            next(iter(set(route_ids)))
            if (
                route_name
                == 'comet.fbweb.CometVideoHomeLOEVideoPermalinkRoute'
                and len(set(route_ids)) == 1
                and route_param_ids == set(route_ids)
            )
            else None
        )
        direct_loe_consensus = bool(
            direct_loe_target
            and set(raw_ids) == {direct_loe_target}
            and (
                not response_url
                or is_facebook_url(
                    response_url, require_https=True
                )
            )
            and (
                not response_ids
                or set(response_ids) == {direct_loe_target}
            )
            and declared_url_consensus(
                canonical_urls, str(direct_loe_target)
            )
            and declared_url_consensus(og_urls, str(direct_loe_target))
            and has_target_video_delivery(
                blocks, [str(direct_loe_target)]
            )
        )
        if (
            route_name
            == 'comet.fbweb.CometVideoHomeLOEVideoPermalinkRoute'
            and route_matches_request
            and not direct_loe_consensus
        ):
            route_matches_request = False
        raw_ids_are_pfbids = bool(raw_ids) and all(
            value.lower().startswith('pfbid') for value in raw_ids
        )
        post_to_video_alias = bool(
            not source_is_share
            and raw_ids
            and '/posts/' in urlparse(
                ensure_full_url(post_path)
            ).path.lower()
            and response_was_redirected
            and redirect_provenance_matches_source()
            and is_facebook_url(response_url, require_https=True)
            and (
                (
                    '/videos/' in urlparse(response_url).path.lower()
                    and route_name == 'comet.fbweb.CometVideoHomeLOEVideoPermalinkRoute'
                )
                or (
                    '/reel/' in urlparse(response_url).path.lower()
                    and route_name == 'comet.fbweb.CometFBReelsRoute'
                )
            )
            and declared_final_target
            and has_target_video_delivery(
                blocks, [str(final_video_id)]
            )
            and (
                (
                    route_name == 'comet.fbweb.CometVideoHomeLOEVideoPermalinkRoute'
                    and (raw_ids_are_pfbids or media_links_source_to_target(str(final_video_id)))
                )
                or (
                    route_name == 'comet.fbweb.CometFBReelsRoute'
                    and reel_links_source_to_target(str(final_video_id))
                )
            )
        )
        if not (
            route_matches_request
            or opaque_share_target
            or post_to_video_alias
        ):
            continue
        if post_to_video_alias:
            used_post_to_video_alias = True
            add(final_video_id, route_proven_ids)
        elif direct_loe_consensus:
            add(direct_loe_target, route_proven_ids)
        else:
            for value in candidate_ids:
                add(value, route_proven_ids)

    selection_ids = (
        route_proven_ids
        if used_post_to_video_alias
        else list(dict.fromkeys([*route_proven_ids, *raw_ids]))
    )
    return (
        selection_ids,
        route_proven_ids,
    )


def contains_exact_id(value, requested_ids: list[str]) -> bool:
    if not requested_ids:
        return False
    if isinstance(value, dict):
        id_fields = {
            'id', 'video_id', 'videoid', 'post_id', 'postid', 'story_fbid',
            'storyfbid', 'story_id', 'storyid', 'fbid', 'legacy_fbid', 'top_level_post_id',
            'mf_story_key', 'feedback_id',
        }
        for key, item in value.items():
            if str(key).lower() in id_fields and isinstance(item, (str, int)):
                if str(item) in requested_ids:
                    return True
            if str(key).lower() in id_fields and isinstance(item, list):
                if any(
                    isinstance(part, (str, int)) and str(part) in requested_ids
                    for part in item
                ):
                    return True
            if contains_exact_id(item, requested_ids):
                return True
    elif isinstance(value, list):
        return any(contains_exact_id(item, requested_ids) for item in value)
    return False


def contains_target_id(value, requested_ids: list[str]) -> bool:
    if not requested_ids:
        return False
    if isinstance(value, dict):
        identity_urls = {
            'url', 'wwwurl', 'permalink', 'permalink_url',
            'shareable_url', 'canonical_url',
        }
        for key, item in value.items():
            if str(key).lower() in identity_urls and isinstance(item, str):
                if set(get_requested_ids(item)).intersection(requested_ids):
                    return True
            if contains_target_id(item, requested_ids):
                return True
        return contains_exact_id(value, requested_ids)
    if isinstance(value, list):
        return any(contains_target_id(item, requested_ids) for item in value)
    return False


def select_requested_candidate(candidates, requested_ids: list[str]):
    candidates = [candidate for candidate in candidates if isinstance(candidate, dict)]
    for candidate in candidates:
        if contains_exact_id(candidate, requested_ids):
            return candidate
    for candidate in candidates:
        if contains_target_id(candidate, requested_ids):
            return candidate
    if requested_ids:
        return None
    return candidates[0] if candidates else None


def select_requested_field(blocks: list[dict], field_name: str, requested_ids: list[str]):
    matching = []
    for block in blocks:
        for node in [block, *jq.nodes(block)]:
            if field_name not in node:
                continue
            if contains_exact_id(node, requested_ids):
                matching.append((node, node[field_name]))
            elif contains_target_id(node, requested_ids):
                matching.append((node, node[field_name]))
    if matching:
        return min(matching, key=lambda item: len(str(item[0])))[1]
    if requested_ids:
        return None
    for block in blocks:
        value = jq.find_first(block, field_name)
        if value not in (None, [], ''):
            return value
    return None
