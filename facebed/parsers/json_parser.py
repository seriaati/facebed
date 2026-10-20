import base64
import json
import logging
import re
import traceback
from contextlib import contextmanager
from typing import Callable
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from curl_cffi.requests import Response as CffiResponse

from facebed.client import cffi
from facebed.config import get_config
from facebed.constants import WWWFB
from facebed.cookies import acc
from facebed.errors import (
    FacebedException,
    NoDataException,
    ParseException,
    UnsupportedRouteException,
    UpstreamException,
)
from facebed.jq import Jq
from facebed.models.post import ParsedPost, banned
from facebed.models.story import Story
from facebed.parsers.embed_extract import EmbedExtract
from facebed.utils import Utils


class JsonParser:
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

    @staticmethod
    def get_headers() -> dict:
        headers = {
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
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36',
        }

        return headers

    @staticmethod
    def get_json_blocks(html_parser: BeautifulSoup) -> list[dict]:
        script_elements = html_parser.find_all('script', attrs={'type': 'application/json'})
        
        data_blocks = []
        for e in script_elements:
            try:
                if e.text:
                    data = json.loads(e.text)
                    data_blocks.append(data)
            except json.JSONDecodeError:
                continue
        return data_blocks

    @staticmethod
    def get_initial_routes(blocks: list[dict]) -> list[dict]:
        routes = []
        for block in blocks:
            route_info = Jq.first(block, 'initialRouteInfo')
            route = route_info.get('route') if isinstance(route_info, dict) else None
            if isinstance(route, dict) and route not in routes:
                routes.append(route)
        return routes

    @staticmethod
    def get_declared_urls(
        html_parser: BeautifulSoup,
        *,
        property_name: str | None = None,
    ) -> list[str]:
        urls: list[str] = []
        if property_name in (None, 'canonical'):
            for link in html_parser.find_all('link'):
                rel = link.get('rel') or []
                if 'canonical' in rel and link.get('href'):
                    urls.append(str(link['href']))
        if property_name in (None, 'og:url'):
            for meta in html_parser.find_all('meta', attrs={'property': 'og:url'}):
                if meta.get('content'):
                    urls.append(str(meta['content']))
        return list(dict.fromkeys(urls))

    @staticmethod
    def direct_identity_ids(node: dict) -> set[str]:
        identities: set[str] = set()
        if not isinstance(node, dict):
            return identities
        for key, item in node.items():
            key_name = str(key).lower()
            if key_name in JsonParser.IDENTITY_FIELDS:
                if isinstance(item, (str, int)) and str(item):
                    identities.add(str(item))
                elif isinstance(item, list):
                    identities.update(
                        str(part) for part in item
                        if isinstance(part, (str, int)) and str(part)
                    )
            elif key_name in JsonParser.IDENTITY_URL_FIELDS and isinstance(item, str):
                parsed_item = urlparse(item)
                if (
                    not parsed_item.netloc
                    or Utils.is_facebook_url(item, require_https=True)
                ):
                    identities.update(JsonParser.get_requested_ids(item))
        return identities

    @staticmethod
    def directly_contains_exact_id(node: dict, requested_ids: list[str]) -> bool:
        return bool(set(map(str, requested_ids)).intersection(
            JsonParser.direct_identity_ids(node)
        ))

    @staticmethod
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

    @staticmethod
    def direct_video_url(node: dict) -> str | None:
        if not isinstance(node, dict):
            return None
        for key in ('browser_native_hd_url', 'browser_native_sd_url'):
            value = node.get(key)
            if isinstance(value, str) and Utils.is_safe_media_url(value):
                return value
        legacy = node.get('videoDeliveryLegacyFields')
        if isinstance(legacy, dict):
            for key in ('browser_native_hd_url', 'browser_native_sd_url'):
                value = legacy.get(key)
                if isinstance(value, str) and Utils.is_safe_media_url(value):
                    return value
        return None

    @staticmethod
    def get_video_delivery_nodes(blocks: list[dict]) -> list[dict]:
        nodes: list[dict] = []
        seen: set[int] = set()
        for block in blocks:
            for node in [block, *Jq.enumerate(block)]:
                if not isinstance(node, dict) or not JsonParser.direct_video_url(node):
                    continue
                marker = id(node)
                if marker not in seen:
                    seen.add(marker)
                    nodes.append(node)
        return nodes

    @staticmethod
    def has_target_video_delivery(blocks: list[dict], target_ids: list[str]) -> bool:
        target_id_set = set(map(str, target_ids))
        return any(
            target_id_set.intersection(
                JsonParser.direct_video_identity_ids(node)
            )
            for node in JsonParser.get_video_delivery_nodes(blocks)
        )

    @staticmethod
    def has_bounded_target_payload(
        blocks: list[dict], target_ids: list[str]
    ) -> bool:
        if not target_ids:
            return False
        for block in blocks:
            for node in [block, *Jq.enumerate(block)]:
                if not isinstance(node, dict):
                    continue
                if not JsonParser.directly_contains_exact_id(node, target_ids):
                    continue
                if JsonParser.direct_video_url(node):
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

    @staticmethod
    def get_terminal_video_id(post_path: str) -> str | None:
        parsed = urlparse(JsonParser.ensure_full_url(post_path))
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

    @staticmethod
    def get_requested_ids(post_path: str) -> list[str]:
        parsed = urlparse(JsonParser.ensure_full_url(post_path))
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

        video_id = JsonParser.get_terminal_video_id(post_path)
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

    @staticmethod
    def get_target_ids(
        html_parser: BeautifulSoup,
        post_path: str,
        response_url: str | None = None,
    ) -> tuple[list[str], list[str]]:
        raw_ids = JsonParser.get_requested_ids(post_path)
        route_proven_ids: list[str] = []
        used_post_to_video_alias = False
        source_is_share = Utils.is_share_path(post_path)
        blocks = JsonParser.get_json_blocks(html_parser)

        selected_response = cffi.selected_get_response
        if response_url is None and selected_response is not None:
            response_url = str(getattr(selected_response, 'url', '') or '')
        response_url = str(response_url or '')
        response_ids = JsonParser.get_requested_ids(response_url)
        response_history = (
            getattr(selected_response, 'history', None)
            if selected_response is not None else None
        )
        response_was_redirected = bool(response_history)

        canonical_urls = JsonParser.get_declared_urls(
            html_parser, property_name='canonical'
        )
        og_urls = JsonParser.get_declared_urls(
            html_parser, property_name='og:url'
        )

        def add(value, target: list[str]) -> None:
            if isinstance(value, (str, int)) and str(value) and str(value) not in target:
                target.append(str(value))

        def share_key(value: str) -> str:
            normalized = Utils.normalize_facebook_path(value)
            return urlparse(JsonParser.ensure_full_url(normalized)).path.rstrip('/')

        source_share_key = share_key(post_path) if source_is_share else ''

        def media_links_source_to_target(candidate_id: str) -> bool:
            for block in blocks:
                for media in Jq.all(block, 'media'):
                    if not isinstance(media, dict):
                        continue
                    if candidate_id not in JsonParser.direct_video_identity_ids(
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
                for story in Jq.all(block, 'creation_story'):
                    if not isinstance(story, dict):
                        continue
                    context = story.get('short_form_video_context')
                    if not isinstance(context, dict):
                        continue
                    if (
                        str(story.get('post_id', '')) in raw_ids
                        and JsonParser.direct_video_identity_ids(context.get('video'))
                        == {candidate_id}
                        and JsonParser.direct_video_identity_ids(context.get('playback_video'))
                        == {candidate_id}
                        and JsonParser.direct_video_url(context.get('playback_video'))
                    ):
                        return True
            return False

        def declared_url_consensus(urls: list[str], target_id: str) -> bool:
            return bool(urls) and all(
                Utils.is_facebook_url(url, require_https=True)
                and set(JsonParser.get_requested_ids(url)) == {target_id}
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
                Utils.is_facebook_url(first_hop_url, require_https=True)
                and '/posts/' in urlparse(first_hop_url).path.lower()
                and set(raw_ids).intersection(
                    JsonParser.get_requested_ids(first_hop_url)
                )
            )

        for route in JsonParser.get_initial_routes(blocks):
            route_name = str(route.get('canonicalRouteName', ''))
            route_url = str(route.get('url', ''))
            if (
                route_name not in JsonParser.CONTENT_ROUTE_NAMES
                and not Utils.is_post_like_path(route_url)
            ):
                continue
            route_ids = JsonParser.get_requested_ids(route_url)

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
                    or Utils.is_facebook_url(
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
                and JsonParser.has_target_video_delivery(
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
                    JsonParser.ensure_full_url(post_path)
                ).path.lower()
                and response_was_redirected
                and redirect_provenance_matches_source()
                and Utils.is_facebook_url(response_url, require_https=True)
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
                and JsonParser.has_target_video_delivery(
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

    @staticmethod
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
                if JsonParser.contains_exact_id(item, requested_ids):
                    return True
        elif isinstance(value, list):
            return any(JsonParser.contains_exact_id(item, requested_ids) for item in value)
        return False

    @staticmethod
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
                    if set(JsonParser.get_requested_ids(item)).intersection(requested_ids):
                        return True
                if JsonParser.contains_target_id(item, requested_ids):
                    return True
            return JsonParser.contains_exact_id(value, requested_ids)
        if isinstance(value, list):
            return any(JsonParser.contains_target_id(item, requested_ids) for item in value)
        return False

    @staticmethod
    def select_requested_candidate(candidates, requested_ids: list[str]):
        candidates = [candidate for candidate in candidates if isinstance(candidate, dict)]
        for candidate in candidates:
            if JsonParser.contains_exact_id(candidate, requested_ids):
                return candidate
        for candidate in candidates:
            if JsonParser.contains_target_id(candidate, requested_ids):
                return candidate
        if requested_ids:
            return None
        return candidates[0] if candidates else None

    @staticmethod
    def select_requested_field(blocks: list[dict], field_name: str, requested_ids: list[str]):
        matching = []
        for block in blocks:
            for node in [block, *Jq.enumerate(block)]:
                if field_name not in node:
                    continue
                if JsonParser.contains_exact_id(node, requested_ids):
                    matching.append((node, node[field_name]))
                elif JsonParser.contains_target_id(node, requested_ids):
                    matching.append((node, node[field_name]))
        if matching:
            return min(matching, key=lambda item: len(str(item[0])))[1]
        if requested_ids:
            return None
        for block in blocks:
            value = Jq.first(block, field_name)
            if value not in (None, [], ''):
                return value
        return None

    @staticmethod
    def probe_page_type(
        html_parser: BeautifulSoup,
        post_path: str = '',
        response_url: str | None = None,
    ) -> str:
        blocks = JsonParser.get_json_blocks(html_parser)
        routes = JsonParser.get_initial_routes(blocks)
        route_names = {
            str(route.get('canonicalRouteName', '')) for route in routes
        }
        if response_url is None and cffi.selected_get_response is not None:
            response_url = str(
                getattr(cffi.selected_get_response, 'url', '') or ''
            )
        response_url = str(response_url or '')
        raw_ids = JsonParser.get_requested_ids(post_path)
        target_ids, route_ids = JsonParser.get_target_ids(
            html_parser, post_path, response_url=response_url
        )
        required_ids = route_ids or raw_ids or target_ids
        canonical_urls = JsonParser.get_declared_urls(
            html_parser, property_name='canonical'
        )
        og_urls = JsonParser.get_declared_urls(
            html_parser, property_name='og:url'
        )

        unsupported_routes = {
            'comet.fbweb.CometProfilePlusLoggedOutRoute':
                JsonParser.PROBE_UNSUPPORTED_PROFILE,
            'comet.fbweb.CometProfileLoggedOutRoute':
                JsonParser.PROBE_UNSUPPORTED_PROFILE,
            'comet.fbweb.CometMarketplaceHoistedPermalinkRoute':
                JsonParser.PROBE_UNSUPPORTED_MARKETPLACE,
            'comet.fbweb.CometProfileLegacyAlbumViewRoute':
                JsonParser.PROBE_UNSUPPORTED_MEDIA_SET,
        }
        for route_name, result in unsupported_routes.items():
            if route_name in route_names:
                return result

        def has_private_group_surface() -> bool:
            for block in blocks:
                for group in Jq.all(block, 'group'):
                    if not isinstance(group, dict):
                        continue
                    join_states = {
                        str(group.get('viewer_join_state', '')),
                        str(group.get('viewer_forum_join_state', '')),
                    }
                    privacy = group.get('privacy_info')
                    title = Jq.first(privacy, 'text') if isinstance(privacy, dict) else None
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
                for story in Jq.all(block, 'creation_story'):
                    if (
                        isinstance(story, dict)
                        and JsonParser.contains_exact_id(story, [reel_id])
                    ):
                        has_context = True
                for video in Jq.all(block, 'video'):
                    if (
                        isinstance(video, dict)
                        and reel_id
                        in JsonParser.direct_video_identity_ids(video)
                        and 'creation_story' in video
                        and video.get('creation_story') is None
                    ):
                        has_missing_stub = True
                for context in Jq.all(block, 'short_form_video_context'):
                    if not isinstance(context, dict):
                        continue
                    for key in ('video', 'playback_video'):
                        video = context.get(key)
                        if (
                            isinstance(video, dict)
                            and reel_id
                            in JsonParser.direct_video_identity_ids(video)
                        ):
                            has_context = True
                    shareable_url = context.get('shareable_url')
                    if (
                        isinstance(shareable_url, str)
                        and reel_id
                        in JsonParser.get_requested_ids(shareable_url)
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
            route_ids = set(JsonParser.get_requested_ids(str(route.get('url', ''))))
            params = route.get('params')
            param_id = (
                str(params.get('video_id'))
                if isinstance(params, dict) and params.get('video_id') else ''
            )
            if len(route_ids) != 1 or not param_id or route_ids != {param_id}:
                return None
            requested_id_set = set(target_ids)  # Honor aliases proved by get_target_ids.
            selected_id_set = set(JsonParser.get_requested_ids(response_url))
            if (
                (requested_id_set and requested_id_set != {param_id})
                or (selected_id_set and selected_id_set != {param_id})
                or not (requested_id_set or selected_id_set)
            ):
                return None
            # A complete target-bound story can identify a reel with stale SEO tags.
            for block in blocks:
                for story in Jq.all(block, 'creation_story'):
                    if not isinstance(story, dict):
                        continue
                    context = story.get('short_form_video_context')
                    if not isinstance(context, dict):
                        continue
                    video = context.get('video')
                    playback = context.get('playback_video')
                    if (
                        str(story.get('post_id', '')) == param_id
                        and JsonParser.direct_video_identity_ids(video) == {param_id}
                        and JsonParser.direct_video_identity_ids(playback) == {param_id}
                        and JsonParser.direct_video_url(playback)
                    ):
                        return param_id

            if not canonical_urls or not og_urls:
                return None
            for urls in (canonical_urls, og_urls):
                if not all(
                    Utils.is_facebook_url(url, require_https=True)
                    and set(JsonParser.get_requested_ids(url)) == {param_id}
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
                    Utils.is_control_path(
                        url, 'login', 'checkpoint', 'recover'
                    )
                    for url in declared_urls
                )
            )
        )

        if 'comet.fbweb.CometVideoHomeVideoNotFoundRoute' in route_names:
            return JsonParser.PROBE_TARGET_NOT_FOUND

        if 'comet.fbweb.CometGroupAboutRoute' in route_names:
            canonical_about = any(
                len(Utils.facebook_path_segments(url)) >= 3
                and Utils.facebook_path_segments(url)[0].casefold() == 'groups'
                and Utils.facebook_path_segments(url)[-1].casefold() == 'about'
                for url in canonical_urls
            )
            if (
                any(
                    Utils.is_post_like_path(path)
                    for path in (post_path, response_url) if path
                )
                and canonical_about
                and has_private_group_surface()
                and not JsonParser.has_bounded_target_payload(
                    blocks, required_ids
                )
            ):
                return JsonParser.PROBE_PRIVATE_GROUP

        if (
            'comet.fbweb.CometVideoHomeLOEVideoPermalinkRoute' in route_names
            and not route_ids
            and (
                raw_ids
                or Utils.is_post_like_path(post_path)
            )
        ):
            return JsonParser.PROBE_TARGET_IDENTITY_MISMATCH

        if 'comet.fbweb.CometFBReelsRoute' in route_names:
            reel_id = reel_consensus_id()
            has_context, has_missing_stub = (
                reel_content_state(reel_id) if reel_id else (False, False)
            )
            has_delivery = bool(
                reel_id
                and JsonParser.has_target_video_delivery(blocks, [reel_id])
            )
            if has_context and has_delivery:
                return JsonParser.PROBE_HAS_DATA
            if auth_wall:
                return JsonParser.PROBE_LOGIN_WALL
            if has_missing_stub and not has_context and not has_delivery:
                return JsonParser.PROBE_TARGET_VIDEO_MISSING
            if (
                not reel_id
                and Utils.is_post_like_path(post_path)
                and (
                    route_ids
                    or raw_ids
                    or JsonParser.get_requested_ids(response_url)
                )
            ):
                return JsonParser.PROBE_TARGET_IDENTITY_MISMATCH

        if JsonParser.has_bounded_target_payload(blocks, required_ids):
            return JsonParser.PROBE_HAS_DATA
        if auth_wall:
            return JsonParser.PROBE_LOGIN_WALL
        return JsonParser.PROBE_UNKNOWN

    @staticmethod
    def raise_for_probe_result(page_type: str, post_path: str) -> None:
        unsupported_messages = {
            JsonParser.PROBE_UNSUPPORTED_PROFILE:
                'Facebook share target is a profile rather than a post',
            JsonParser.PROBE_UNSUPPORTED_MARKETPLACE:
                'Facebook share target is a Marketplace listing',
            JsonParser.PROBE_UNSUPPORTED_MEDIA_SET:
                'Facebook share target is a media-set album',
        }
        if page_type in unsupported_messages:
            raise UnsupportedRouteException(
                unsupported_messages[page_type], reason_code=page_type
            )
        messages = {
            JsonParser.PROBE_LOGIN_WALL:
                f'Facebook served a login wall for {post_path}',
            JsonParser.PROBE_PRIVATE_GROUP:
                f'Facebook served a private group landing without the requested post for {post_path}',
            JsonParser.PROBE_TARGET_VIDEO_MISSING:
                f'Facebook response has no target-local playable video for {post_path}',
            JsonParser.PROBE_TARGET_NOT_FOUND:
                f'Facebook reported that the requested target was not found for {post_path}',
            JsonParser.PROBE_TARGET_IDENTITY_MISMATCH:
                f'Facebook route identities disagree for {post_path}',
            JsonParser.PROBE_NO_DATA:
                f'Facebook response did not contain target data for {post_path}',
        }
        if page_type in messages:
            raise NoDataException(
                messages[page_type], reason_code=page_type, terminal=True
            )

    @staticmethod
    def check_page_or_raise(
        html_parser: BeautifulSoup,
        post_path: str,
        response_url: str | None = None,
    ) -> str:
        page_type = JsonParser.probe_page_type(
            html_parser, post_path, response_url=response_url
        )
        JsonParser.raise_for_probe_result(page_type, post_path)
        return page_type

    @staticmethod
    @contextmanager
    def fetch_page(
        post_path: str,
        http_response: CffiResponse | None = None,
    ):
        url = JsonParser.ensure_full_url(post_path)
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
            if current_url and not Utils.is_facebook_url(
                current_url, require_https=True
            ):
                raise UnsupportedRouteException(
                    'Facebook redirected outside Facebook',
                    reason_code='offsite_redirect',
                )
            current_html = current_response.text
            current_parser = BeautifulSoup(current_html, 'html.parser')
            page_type = JsonParser.probe_page_type(
                current_parser,
                post_path,
                response_url=current_url,
            )
            return current_html, current_parser, page_type

        raw_html, html_parser, page_type = prepare_page(http_response)
        if (
            owns_response
            and http_response.status_code in cffi.retry_statuses
            and page_type != JsonParser.PROBE_HAS_DATA
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
            JsonParser.PROBE_HAS_DATA,
            JsonParser.PROBE_UNSUPPORTED_PROFILE,
            JsonParser.PROBE_UNSUPPORTED_MARKETPLACE,
            JsonParser.PROBE_UNSUPPORTED_MEDIA_SET,
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
            JsonParser.PROBE_LOGIN_WALL,
            JsonParser.PROBE_PRIVATE_GROUP,
        }:
            JsonParser.raise_for_probe_result(page_type, post_path)
        if (
            http_response.status_code >= 400
            and page_type != JsonParser.PROBE_HAS_DATA
        ):
            raise UpstreamException(
                f'Facebook returned HTTP {http_response.status_code} for {url}',
                http_response,
            )
        JsonParser.raise_for_probe_result(page_type, post_path)
        try:
            yield html_parser
        except ParseException as e:
            if not e.html:
                e.html = raw_html
                e.url = url
            raise

    @staticmethod
    def get_post_json(
        html_parser: BeautifulSoup,
        post_path: str = '',
        requested_ids: list[str] | None = None,
        required_ids: list[str] | None = None,
    ) -> dict:
        blocks = JsonParser.get_json_blocks(html_parser)
        requested_ids = (
            JsonParser.get_requested_ids(post_path)
            if requested_ids is None else requested_ids
        )
        required_ids = required_ids or []
        candidate_blocks = []
        for bloc in blocks:
            try:
                root = JsonParser.get_root_node(bloc, requested_ids)
            except ParseException:
                continue
            if required_ids and not JsonParser.contains_target_id(root, required_ids):
                continue
            candidate_blocks.append(bloc)

        if not candidate_blocks:
            if required_ids:
                if JsonParser.has_bounded_target_payload(
                    blocks, required_ids
                ):
                    raise ParseException(
                        'Facebook target payload shape is unsupported'
                    )
                raise NoDataException(
                    'Facebook response did not contain the requested post',
                    reason_code='target_payload_missing',
                )
            raise ParseException('cannot find post json')

        def score_block(bloc: dict) -> int:
            score = 0
            if Jq.has(bloc, 'short_form_video_context'):
                score += 20
            if Jq.has(bloc, 'creation_story'):
                score += 10
            if Jq.has(bloc, 'comet_sections'):
                score += 5
            if Jq.has(bloc, 'group_hoisted_feed'):
                score += 8
            if Jq.has(bloc, 'video_home_www_related_videos_section') or Jq.has(bloc, 'video_home_www_loe_video_permalink_seo_info'):
                score -= 20
            node_v2 = Jq.first(bloc, 'node_v2')
            if isinstance(node_v2, dict):
                if 'actors' in node_v2 and node_v2['actors']:
                    score += 30
                if 'feedback' in node_v2 and isinstance(node_v2['feedback'], dict):
                    score += 15
                if 'comet_sections' in node_v2 or 'creation_story' in node_v2:
                    score += 10
            data_blob = Jq.first(bloc, 'data')
            if isinstance(data_blob, dict):
                if 'actors' in data_blob and data_blob['actors']:
                    score += 20
                if 'feedback' in data_blob and isinstance(data_blob['feedback'], dict):
                    score += 10
            if 'require' in bloc and not node_v2 and not data_blob:
                score -= 10
            return score

        candidate_blocks.sort(
            key=lambda block: (
                JsonParser.contains_target_id(block, required_ids),
                JsonParser.contains_exact_id(block, requested_ids),
                JsonParser.contains_target_id(block, requested_ids),
                score_block(block),
            ),
            reverse=True,
        )
        for candidate in candidate_blocks:
            try:
                root = JsonParser.get_root_node(candidate, requested_ids)
                if required_ids and not JsonParser.contains_target_id(root, required_ids):
                    continue
                return candidate
            except ParseException:
                continue
        if required_ids:
            if JsonParser.has_bounded_target_payload(blocks, required_ids):
                raise ParseException(
                    'Facebook target payload shape is unsupported'
                )
            raise NoDataException(
                'Facebook response did not contain the requested post',
                reason_code='target_payload_missing',
            )
        return candidate_blocks[0]

    @staticmethod
    def get_group_name(
        html_parser: BeautifulSoup,
        requested_ids: list[str] | None = None,
    ) -> str:
        candidates = [
            bloc for bloc in JsonParser.get_json_blocks(html_parser)
            if Jq.has(bloc, 'group_member_profiles', 'formatted_count_text')
        ]
        requested_ids = requested_ids or []
        selected = JsonParser.select_requested_candidate(candidates, requested_ids)
        if selected is None:
            return ''
        for group_object in Jq.all(selected, 'group'):
            if isinstance(group_object, dict) and 'name' in group_object:
                return group_object['name']
        return ''

    @staticmethod
    def get_interaction_counts(
        post_json: dict,
        requested_ids: list[str] | None = None,
        top_reaction_ids: list[str] | None = None,
    ) -> tuple[str, str, str]:
        assert post_json
        if top_reaction_ids is not None:
            top_reaction_ids.clear()

        def extract_counts(fb: dict) -> tuple[str, str, str]:
            if top_reaction_ids is not None:
                top_reaction_ids.extend(Utils.get_top_reaction_ids(fb))
            reaction_count = fb.get('reaction_count')
            if isinstance(reaction_count, dict):
                reaction_count = reaction_count.get('count')
            share_count = fb.get('share_count')
            if isinstance(share_count, dict):
                share_count = share_count.get('count')
            reactions = fb.get('i18n_reaction_count') or reaction_count or Jq.first(fb, 'i18n_reaction_count') or '0'
            shares = fb.get('i18n_share_count') or share_count or Jq.first(fb, 'i18n_share_count') or Jq.first(fb, 'share_count') or '0'
            comments = fb.get('total_comment_count') or Jq.first(fb, 'total_comment_count')
            if not comments:
                cri = fb.get('comment_rendering_instance')
                if isinstance(cri, dict):
                    cnode = cri.get('comments')
                    if isinstance(cnode, dict):
                        comments = cnode.get('total_count')
                if not comments:
                    ccsr = fb.get('comments_count_summary_renderer')
                    if isinstance(ccsr, dict):
                        fb_inner = ccsr.get('feedback')
                        if isinstance(fb_inner, dict):
                            cri2 = fb_inner.get('comment_rendering_instance')
                            if isinstance(cri2, dict):
                                cnode2 = cri2.get('comments')
                                if isinstance(cnode2, dict):
                                    comments = cnode2.get('total_count')
            if not comments:
                comments = '0'
            return str(reactions), str(comments), str(shares)

        def best_feedback() -> dict | None:
            best = None
            best_reactions = 0
            for fb in Jq.all(post_json, 'feedback'):
                if isinstance(fb, dict):
                    rc = fb.get('i18n_reaction_count')
                    if rc:
                        try:
                            n = int(rc)
                            if n > best_reactions:
                                best_reactions = n
                                best = fb
                        except (ValueError, TypeError):
                            pass
                    elif best is None:
                        best = fb
            return best

        requested_ids = [str(value) for value in (requested_ids or []) if value]
        id_keys = {
            'id', 'video_id', 'videoid', 'post_id', 'postid', 'story_fbid',
            'storyfbid', 'fbid', 'legacy_fbid', 'story_id', 'storyid',
            'top_level_post_id', 'mf_story_key', 'feedback_id',
        }
        direct_feedback = post_json.get('feedback')
        for identity_source in (post_json, direct_feedback):
            if not isinstance(identity_source, dict):
                continue
            for key, item in identity_source.items():
                if str(key).lower() not in id_keys:
                    continue
                if isinstance(item, (str, int)) and str(item):
                    requested_ids.append(str(item))
                elif isinstance(item, list):
                    requested_ids.extend(
                        str(part) for part in item
                        if isinstance(part, (str, int)) and str(part)
                    )
        requested_ids = list(dict.fromkeys(requested_ids))
        if requested_ids:
            contextual_feedbacks: list[dict] = []

            def walk(value, inherited_ids: set[str] | None = None) -> None:
                inherited_ids = inherited_ids or set()
                if isinstance(value, dict):
                    own_ids: set[str] = set()
                    for key, item in value.items():
                        if (
                            str(key).lower() not in id_keys
                            or not isinstance(item, (str, int))
                        ):
                            continue
                        text = str(item)
                        own_ids.add(text)
                        try:
                            decoded = base64.b64decode(
                                text + ('=' * (-len(text) % 4)),
                                validate=True,
                            ).decode('utf-8')
                        except (ValueError, UnicodeDecodeError):
                            continue
                        for requested_id in requested_ids:
                            if requested_id == decoded or (
                                requested_id.isdigit()
                                and re.search(
                                    rf'(?<![A-Za-z0-9]){re.escape(requested_id)}(?![A-Za-z0-9])',
                                    decoded,
                                )
                            ):
                                own_ids.add(requested_id)
                    context_ids = own_ids or inherited_ids
                    feedback = value.get('feedback')
                    if (
                        isinstance(feedback, dict)
                        and context_ids.intersection(requested_ids)
                    ):
                        contextual_feedbacks.append(feedback)
                    for item in value.values():
                        walk(item, context_ids)
                elif isinstance(value, list):
                    for item in value:
                        walk(item, inherited_ids)

            walk(post_json)
            if isinstance(direct_feedback, dict) and any(
                marker in direct_feedback
                for marker in (
                    'i18n_reaction_count', 'reaction_count', 'total_comment_count',
                    'i18n_share_count', 'share_count',
                    'comment_rendering_instance', 'comments_count_summary_renderer',
                )
            ):
                return extract_counts(direct_feedback)
            if contextual_feedbacks:
                def reaction_score(feedback: dict) -> float:
                    rc = feedback.get('reaction_count')
                    if isinstance(rc, dict):
                        rc = rc.get('count')
                    if rc is None:
                        rc = feedback.get('i18n_reaction_count')
                    if rc is None:
                        return -1
                    text = str(rc).strip().upper().replace(',', '')
                    multiplier = 1
                    if text.endswith(('K', 'M', 'B')):
                        multiplier = {'K': 1_000, 'M': 1_000_000, 'B': 1_000_000_000}[text[-1]]
                        text = text[:-1]
                    try:
                        return float(text) * multiplier
                    except (TypeError, ValueError):
                        return 0

                best = max(
                    contextual_feedbacks,
                    key=reaction_score,
                )
                return extract_counts(best)
            return '0', '0', '0'

        post_feedback = Jq.first(post_json, 'comet_ufi_summary_and_actions_renderer')
        if post_feedback and isinstance(post_feedback, dict) and 'feedback' in post_feedback:
            return extract_counts(post_feedback['feedback'])

        fb = Jq.first(post_json, 'feedback')
        if fb and isinstance(fb, dict):
            rc = fb.get('i18n_reaction_count')
            if rc:
                return extract_counts(fb)
            best = best_feedback()
            if best:
                return extract_counts(best)

        best = best_feedback()
        if best:
            return extract_counts(best)

        reactions = Jq.first(post_json, 'i18n_reaction_count') or '0'
        shares = Jq.first(post_json, 'i18n_share_count') or Jq.first(post_json, 'share_count') or '0'
        comments = Jq.first(post_json, 'total_comment_count')
        if not comments:
            cri = Jq.first(post_json, 'comment_rendering_instance')
            if isinstance(cri, dict):
                cnode = cri.get('comments')
                if isinstance(cnode, dict):
                    comments = cnode.get('total_count')
            if not comments:
                ccsr = Jq.first(post_json, 'comments_count_summary_renderer')
                if isinstance(ccsr, dict):
                    fb_inner = ccsr.get('feedback')
                    if isinstance(fb_inner, dict):
                        cri2 = fb_inner.get('comment_rendering_instance')
                        if isinstance(cri2, dict):
                            cnode2 = cri2.get('comments')
                            if isinstance(cnode2, dict):
                                comments = cnode2.get('total_count')
        if not comments:
            comments = '0'

        return str(reactions), str(comments), str(shares)

    @staticmethod
    def get_root_node(post_json: dict, requested_ids: list[str] | None = None) -> dict:
        requested_ids = requested_ids or []

        def selected(key: str, source: dict = post_json):
            return JsonParser.select_requested_candidate(Jq.all(source, key), requested_ids)

        def work_normal_post() -> dict:
            data_blob = selected('data')
            if not isinstance(data_blob, dict):
                short_form = selected('short_form_video_context')
                if short_form:
                    return {'creation_story': short_form}
                return {}
            if 'comet_ufi_summary_and_actions_renderer' in data_blob:
                return data_blob
            elif 'node_v2' in data_blob and isinstance(data_blob['node_v2'], dict):
                node_v2 = data_blob['node_v2']
                if 'comet_sections' in node_v2 or 'creation_story' in node_v2:
                    return node_v2
            elif 'node' in data_blob and isinstance(data_blob['node'], dict):
                node = data_blob['node']
                if 'comet_sections' in node or 'creation_story' in node:
                    return node
            short_form = selected('short_form_video_context', data_blob)
            if short_form:
                return {'creation_story': short_form}
            return {}

        def work_group_post() -> dict:
            hoisted_feed = selected('group_hoisted_feed')
            if isinstance(hoisted_feed, dict):
                if 'comet_sections' in hoisted_feed or 'creation_story' in hoisted_feed:
                    return hoisted_feed
                node_v2 = selected('node_v2', hoisted_feed)
                if isinstance(node_v2, dict):
                    return node_v2

            data_blob = selected('data')
            if isinstance(data_blob, dict):
                group = data_blob.get('group')
                if isinstance(group, dict):
                    if 'comet_sections' in group or 'creation_story' in group:
                        return group
                    node_v2 = selected('node_v2', group)
                    if isinstance(node_v2, dict):
                        return node_v2
            return {}

        methods: list[Callable[[], dict]] = [work_normal_post, work_group_post]

        for method in methods:
            try:
                ret = method()
                if ret:
                    return ret
            except (StopIteration, KeyError):
                continue

        data_blob = selected('data')
        if isinstance(data_blob, dict):
            if 'creation_story' in data_blob and 'feedback' in data_blob:
                return data_blob
            if 'node_v2' in data_blob and isinstance(data_blob['node_v2'], dict):
                return data_blob['node_v2']

        raise ParseException('Cannot process post')

    @staticmethod
    def ensure_full_url(u: str) -> str:
        value = str(u)
        parsed = urlparse(value)
        if not parsed.netloc and re.match(r'^(?:www\.|web\.|m\.|mbasic\.)?facebook\.com/', value, re.IGNORECASE):
            parsed = urlparse(f'https://{value}')
        if parsed.netloc:
            hostname = (parsed.hostname or '').lower()
            if hostname == 'facebook.com' or hostname.endswith('.facebook.com'):
                suffix = parsed.path.lstrip('/')
                if parsed.query:
                    suffix += f'?{parsed.query}'
                return f'{WWWFB}/{suffix}'
            return value
        return f'{WWWFB}/{value.removeprefix("/")}'

    @staticmethod
    def process_post(
        post_path: str,
        http_response: CffiResponse | None = None,
    ) -> ParsedPost:
        from facebed.parsers.reels import ReelsParser  # circular import

        if http_response is None:
            page = JsonParser.fetch_page(post_path)
        else:
            page = JsonParser.fetch_page(post_path, http_response=http_response)
        with page as html_parser:
            raw_ids = JsonParser.get_requested_ids(post_path)
            requested_ids, route_proven_ids = JsonParser.get_target_ids(html_parser, post_path)
            effective_post_path = post_path
            if (
                raw_ids
                and route_proven_ids
                and not set(raw_ids).intersection(route_proven_ids)
                and cffi.selected_get_response is not None
            ):
                selected_url = str(
                    getattr(cffi.selected_get_response, 'url', '') or ''
                )
                if Utils.facebook_path_segments(selected_url):
                    effective_post_path = selected_url
                    if urlparse(selected_url).path.startswith('/reel/'):
                        return ReelsParser.process_post(
                            selected_url, http_response=cffi.selected_get_response
                        )
            if Utils.is_share_path(post_path) and not raw_ids and not route_proven_ids:
                raise NoDataException(
                    'Facebook response did not identify the requested share post'
                )
            required_ids = route_proven_ids or raw_ids
            post_json = JsonParser.get_root_node(
                JsonParser.get_post_json(
                    html_parser,
                    post_path,
                    requested_ids,
                    required_ids,
                ),
                requested_ids,
            )
            if required_ids and not JsonParser.contains_target_id(post_json, required_ids):
                raise NoDataException('Facebook response selected a different post')
            top_reaction_ids: list[str] = []
            likes, cmts, shares = JsonParser.get_interaction_counts(
                post_json, requested_ids, top_reaction_ids
            )

            post_date = -1
            t = Jq.first(post_json, 'creation_time') or Jq.first(post_json, 'created_time')
            if t:
                try:
                    post_date = int(t)
                except (ValueError, TypeError):
                    pass
            if post_date == -1:
                blocks = JsonParser.get_json_blocks(html_parser)
                t = (
                    JsonParser.select_requested_field(blocks, 'creation_time', requested_ids)
                    or JsonParser.select_requested_field(blocks, 'created_time', requested_ids)
                )
                if t:
                    try:
                        post_date = int(t)
                    except (ValueError, TypeError):
                        pass

            story_dict = post_json
            if 'content' in post_json and isinstance(post_json['content'], dict) and 'story' in post_json['content']:
                story_dict = post_json['content']['story']
            elif 'creation_story' in post_json:
                story_dict = post_json['creation_story']
                if 'owner' in post_json and ('actors' not in story_dict or not story_dict['actors']):
                    story_dict['actors'] = [post_json['owner']]
            elif 'comet_sections' in post_json:
                sections = post_json['comet_sections']
                if isinstance(sections, dict) and 'content' in sections and isinstance(sections['content'], dict) and 'story' in sections['content']:
                    story_dict = sections['content']['story']
                elif 'feedback' in post_json:
                    pass

            story = Story(story_dict)
            parsed_post_path = urlparse(
                JsonParser.ensure_full_url(effective_post_path)
            ).path
            if not story.video_links and re.search(
                r'/(?:reel|watch|videos|v)(?:/|$)', parsed_post_path, re.IGNORECASE
            ):
                try:
                    story.video_links.append(
                        ReelsParser.get_video_link(
                            html_parser, requested_ids=requested_ids
                        )
                    )
                except FacebedException:
                    pass
            canonical = html_parser.find('link', attrs={'rel': 'canonical'})
            canonical_url = str(canonical.get('href', '')) if canonical else ''
            parsed_canonical = urlparse(canonical_url)
            canonical_host = (parsed_canonical.hostname or '').lower()
            validated_identity_ids = set(route_proven_ids or raw_ids)
            identity_fields = {
                'id', 'video_id', 'videoid', 'post_id', 'postid', 'story_fbid',
                'storyfbid', 'fbid', 'legacy_fbid', 'story_id', 'storyid',
                'top_level_post_id', 'mf_story_key', 'feedback_id',
            }
            def collect_identity_ids(value) -> None:
                if not isinstance(value, dict):
                    return
                for key, item in value.items():
                    key_name = str(key).lower()
                    if key_name in identity_fields and isinstance(item, (str, int)):
                        validated_identity_ids.add(str(item))
                    elif key_name in identity_fields and isinstance(item, list):
                        validated_identity_ids.update(
                            str(part) for part in item
                            if isinstance(part, (str, int))
                        )

            collect_identity_ids(post_json)
            collect_identity_ids(story_dict)
            story_url_ids = set(JsonParser.get_requested_ids(story.url))
            canonical_ids = set(JsonParser.get_requested_ids(canonical_url))
            if (
                canonical_host == 'facebook.com'
                or canonical_host.endswith('.facebook.com')
            ) and canonical_ids.intersection(validated_identity_ids):
                parts = [part for part in parsed_canonical.path.split('/') if part]
                if 'posts' in parts:
                    post_index = parts.index('posts')
                    numeric_ids = [part for part in parts[post_index + 1:] if part.isdigit()]
                    if numeric_ids:
                        parts = [*parts[:post_index + 1], numeric_ids[-1]]
                        parsed_canonical = parsed_canonical._replace(
                            path='/' + '/'.join(parts),
                            query='',
                            fragment='',
                        )
                post_url = parsed_canonical.geturl()
            elif story.url and story_url_ids.intersection(validated_identity_ids):
                post_url = story.url or JsonParser.ensure_full_url(post_path)
            else:
                post_url = JsonParser.ensure_full_url(effective_post_path)
            post_content = story.get_text()
            post_group_name = JsonParser.get_group_name(html_parser, requested_ids)
            post_author_name = story.author_name
            link_header = f'{post_author_name}' + (f' • {post_group_name}' if post_group_name else '')

            if story.author_id in get_config().banned_users:
                return banned(post_url)

            # TODO: support normal /watch here
            parsed = ParsedPost(link_header, post_content.strip(), story.image_links, post_url, post_date,
                                likes, cmts, shares, story.video_links,
                                top_reaction_ids=tuple(top_reaction_ids))
            try:
                EmbedExtract.fill_post(parsed, post_json, story_dict)
            except Exception:
                logging.warning(f'component embed extraction failed for {post_path}:\n{traceback.format_exc()}')
            return parsed


def _authentication_wall_reason(
    final_url: str,
    html_parser: BeautifulSoup,
) -> str | None:
    if Utils.is_control_path(final_url, 'login'):
        return 'login redirect'
    if Utils.is_control_path(final_url, 'checkpoint'):
        return 'checkpoint redirect'
    if Utils.is_control_path(final_url, 'recover'):
        return 'account recovery redirect'
    route_names = {
        str(route.get('canonicalRouteName', ''))
        for route in JsonParser.get_initial_routes(
            JsonParser.get_json_blocks(html_parser)
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
        if not Utils.is_control_path(str(form.get('action', '')), 'login'):
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
