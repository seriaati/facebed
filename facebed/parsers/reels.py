import logging

from bs4 import BeautifulSoup
from curl_cffi.requests import Response as CffiResponse

from facebed import jq
from facebed.client import cffi
from facebed.config import get_config
from facebed.constants import DISCORD_VIDEO_MAX_BYTES
from facebed.errors import NoDataException, ParseException, UpstreamException, shape_errors
from facebed.formatting import human_format
from facebed.models.post import ParsedPost, banned
from facebed.parsers.blocks import get_json_blocks
from facebed.parsers.fetch import fetch_page
from facebed.parsers.identity import (
    contains_exact_id,
    contains_target_id,
    direct_video_identity_ids,
    get_target_ids,
    get_video_delivery_nodes,
    select_requested_field,
)
from facebed.urls import ensure_full_url, is_safe_media_url

logger = logging.getLogger(__name__)


# narrower than identity.IDENTITY_FIELDS; widening it changes which feedback nodes count as the reel's
REEL_FEEDBACK_ID_FIELDS = frozenset({'fbid', 'id', 'legacy_fbid', 'post_id', 'postid', 'story_fbid', 'storyfbid', 'top_level_post_id', 'video_id', 'videoid'})


def too_big_for_discord(link: str) -> bool:
    # discord downloads the whole video and drops the embed if that takes ~10s
    try:
        head = cffi.head(link, timeout=5, _check_status=False, _retry_status_responses=False)
        size = int(head.headers.get('content-length', 0))
    except (UpstreamException, ValueError):
        return False
    return size > DISCORD_VIDEO_MAX_BYTES


def get_video_link(
    html_parser: BeautifulSoup | None,
    user_node: dict | None = None,
    requested_ids: list[str] | None = None,
) -> str:
    def work_node(node: dict) -> str:
        video_node = node
        if not (jq.find_first(video_node, 'browser_native_hd_url') or jq.find_first(video_node, 'browser_native_sd_url')):
            video_node = jq.find_first(node, 'videoDeliveryLegacyFields')
        hd_link, sd_link = (
            str(link) if link and is_safe_media_url(str(link)) else ''
            for link in (jq.find_first(video_node, key) for key in ['browser_native_hd_url', 'browser_native_sd_url'])
        )
        if hd_link and sd_link and too_big_for_discord(hd_link):
            return sd_link
        if hd_link or sd_link:
            return hd_link or sd_link
        raise ParseException('Invalid reels link (vn)')

    if user_node:
        return work_node(user_node)

    candidates = get_video_delivery_nodes(
        get_json_blocks(html_parser)
    )

    requested_ids = requested_ids or []
    requested_id_set = set(map(str, requested_ids))
    matching = [
        node for node in candidates
        if requested_id_set.intersection(
            direct_video_identity_ids(node)
        )
    ]
    for node in matching:
        try:
            return work_node(node)
        except (ParseException, KeyError, TypeError, IndexError):
            continue
    if requested_ids:
        raise NoDataException('Facebook response did not contain the requested video')
    for node in candidates:
        try:
            return work_node(node)
        except (ParseException, KeyError, TypeError, IndexError):
            continue

    raise ParseException('Invalid reels link (vn)')


def get_content_node(html_parser: BeautifulSoup, requested_ids: list[str] | None = None) -> dict:
    candidates = []
    for bloc in get_json_blocks(html_parser):
        candidates.extend(jq.find_all(bloc, 'creation_story'))
        candidates.extend(jq.find_all(bloc, 'short_form_video_context'))
    candidates = [node for node in candidates if isinstance(node, dict)]

    requested_ids = requested_ids or []
    for node in candidates:
        if contains_exact_id(node, requested_ids):
            return node
    for node in candidates:
        if contains_target_id(node, requested_ids):
            return node
    if requested_ids:
        raise NoDataException('Facebook response did not contain the requested video')
    if candidates:
        return candidates[0]
    raise ParseException('Invalid reels link (cn)')


def get_reaction_counts(
    html_parser: BeautifulSoup,
    is_ig: bool,
    video_id: str,
    related_ids: list[str] | None = None,
) -> tuple[str, str, str]:
    likes, cmts, shares = get_raw_reaction_counts(html_parser, is_ig, video_id, related_ids)
    return human_format(likes), human_format(cmts), human_format(shares)


def get_raw_reaction_counts(
    html_parser: BeautifulSoup,
    is_ig: bool,
    video_id: str,
    related_ids: list[str] | None = None,
) -> tuple:
    target_ids = list(dict.fromkeys(
        str(value) for value in [video_id, *(related_ids or [])] if value
    ))
    direct_blocks: list[dict] = []
    url_blocks: list[dict] = []
    for bloc in get_json_blocks(html_parser):
        if not jq.has(bloc, 'unified_reactors'):
            continue
        if contains_exact_id(bloc, target_ids):
            direct_blocks.append(bloc)
        elif contains_target_id(bloc, target_ids):
            url_blocks.append(bloc)

    blocks = direct_blocks or url_blocks

    if len(blocks) == 0:
        raise ParseException('Cannot process post (cn)')

    contextual_feedbacks: list[tuple[set[str], dict]] = []

    def walk(value, inherited_ids: set[str] | None = None):
        inherited_ids = inherited_ids or set()
        if isinstance(value, dict):
            own_ids = set()
            for key, item in value.items():
                if str(key).lower() not in REEL_FEEDBACK_ID_FIELDS:
                    continue
                if isinstance(item, (str, int)):
                    own_ids.add(str(item))
                elif isinstance(item, list):
                    own_ids.update(str(part) for part in item if isinstance(part, (str, int)))
            context_ids = own_ids or inherited_ids
            feedback = value.get('feedback')
            if isinstance(feedback, dict):
                contextual_feedbacks.append((set(context_ids), feedback))
            elif isinstance(feedback, list):
                contextual_feedbacks.extend(
                    (set(context_ids), item) for item in feedback if isinstance(item, dict)
                )
            for item in value.values():
                walk(item, context_ids)
        elif isinstance(value, list):
            for item in value:
                walk(item, inherited_ids)

    for block in blocks:
        walk(block)

    target_id_set = set(target_ids)
    feedbacks: list[tuple[set[str], dict]] = []
    seen = set()
    for context_ids, feedback in contextual_feedbacks:
        if not context_ids.intersection(target_id_set) and not contains_target_id(feedback, target_ids):
            continue
        marker = id(feedback)
        if marker not in seen:
            seen.add(marker)
            feedbacks.append((context_ids, feedback))

    if feedbacks:
        def unified_count(item: tuple[set[str], dict]) -> int:
            count = item[1].get('unified_reactors', {}).get('count', 0)
            try:
                return int(count)
            except (TypeError, ValueError):
                return 0

        first_context, first_fb = max(feedbacks, key=unified_count)
        context_companions = [
            feedback for context_ids, feedback in feedbacks
            if feedback is not first_fb
            and (not first_context or context_ids.intersection(first_context))
        ]
        first_feedback_id = first_fb.get('id')
        companions = [
            feedback for feedback in context_companions
            if first_feedback_id and feedback.get('id') == first_feedback_id
        ] or context_companions
        last_fb = next((
            fb for fb in reversed(companions)
            if 'total_comment_count' in fb
            or 'share_count_reduced' in fb
            or 'share_count' in fb
        ), None)
        if last_fb is None:
            last_fb = next((
                fb for fb in reversed(companions)
                if 'cross_universe_feedback_info' in fb
            ), first_fb)
    else:
        raise ParseException('Cannot associate reactions with requested video')

    cross_info = last_fb.get('cross_universe_feedback_info', {})
    ig_cmts = cross_info.get('ig_comment_count') or last_fb.get('total_comment_count', 0)
    likes = first_fb.get('unified_reactors', {}).get('count', 0)
    cmts = ig_cmts if is_ig else last_fb.get('total_comment_count', 0)
    shares = last_fb.get('share_count_reduced', last_fb.get('share_count', 0))

    return likes, cmts, shares


@shape_errors()
def process_post(
    post_path: str,
    http_response: CffiResponse | None = None,
) -> ParsedPost:
    with fetch_page(post_path, http_response=http_response) as html_parser:
        requested_ids, _strong_ids = get_target_ids(html_parser, post_path)
        content_node = get_content_node(html_parser, requested_ids)

        video_link = get_video_link(html_parser, requested_ids=requested_ids)
        video_id = next(
            (
                requested_id for requested_id in requested_ids
                if contains_target_id(content_node, [requested_id])
            ),
            None,
        )
        video_id = video_id or content_node.get('video', {}).get('id') or content_node.get('id')
        if not video_id:
            video_id = jq.find_first(content_node, 'id')

        owner_info = content_node.get('short_form_video_context', {}).get('video_owner') or content_node.get('video_owner')
        if not owner_info:
            owner_info = jq.find_first(content_node, 'video_owner')

        is_ig = owner_info['__typename'].startswith('InstagramUser')
        op_name = ('📷 @' if is_ig else '') + owner_info['username' if is_ig else 'name']
        post_url = content_node.get('short_form_video_context', {}).get('shareable_url') or ensure_full_url(post_path)

        post_date = content_node.get('creation_time')
        if not post_date:
            blocks = get_json_blocks(html_parser)
            post_date = (
                select_requested_field(blocks, 'creation_time', requested_ids)
                or select_requested_field(blocks, 'created_time', requested_ids)
            )
            try:
                post_date = int(post_date) if post_date else None
            except (ValueError, TypeError):
                post_date = None
        if not post_date:
            post_date = -1

        post_text = '' if content_node.get('message') is None else content_node['message']['text']

        reaction_ids = [video_id, content_node.get('id'), content_node.get('post_id')]
        content_feedback = content_node.get('feedback')
        if isinstance(content_feedback, dict):
            reaction_ids.append(content_feedback.get('id'))
        raw_counts = get_raw_reaction_counts(
            html_parser, is_ig, video_id, reaction_ids
        )
        likes, cmts, shares = (human_format(x) for x in raw_counts)

        if str(owner_info['id']) in get_config().banned_users:
            return banned(post_url)

        parsed = ParsedPost(op_name, post_text, [], post_url, post_date, likes, cmts, shares, [video_link])
        try:
            reel_id = str((content_node.get('video') or {}).get('id') or video_id)
            parsed.key = f'r{reel_id}' if reel_id.isdigit() else ''
            parsed.author = owner_info.get('username' if is_ig else 'name') or ''
            parsed.author_url = owner_info.get('url') or ''
            parsed.avatar = (owner_info.get('displayPicture') or {}).get('uri') or ''
            parsed.verified = bool(owner_info.get('is_verified'))
            parsed.media = [('video', video_link)]
            r_likes, r_cmts, r_shares = (int(x) if str(x).isdigit() else None for x in raw_counts)
            parsed.counts = (r_likes, r_cmts, r_shares)
        except Exception:
            logger.warning('component embed extraction failed for %s', post_path, exc_info=True)
        return parsed
