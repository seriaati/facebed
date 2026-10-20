import base64
import contextlib
import logging
import re
import traceback
from collections.abc import Callable
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from curl_cffi.requests import Response as CffiResponse

from facebed import jq
from facebed.client import cffi
from facebed.config import get_config
from facebed.errors import (
    FacebedException,
    NoDataException,
    ParseException,
)
from facebed.formatting import get_top_reaction_ids
from facebed.models.post import ParsedPost, banned
from facebed.models.story import Story
from facebed.parsers import embed_extract, reels
from facebed.parsers.blocks import (
    get_json_blocks,
)
from facebed.parsers.fetch import fetch_page
from facebed.parsers.identity import (
    contains_exact_id,
    contains_target_id,
    get_requested_ids,
    get_target_ids,
    has_bounded_target_payload,
    select_requested_candidate,
    select_requested_field,
)
from facebed.urls import (
    ensure_full_url,
    facebook_path_segments,
    is_share_path,
)


def get_post_json(
    html_parser: BeautifulSoup,
    post_path: str = '',
    requested_ids: list[str] | None = None,
    required_ids: list[str] | None = None,
) -> dict:
    blocks = get_json_blocks(html_parser)
    requested_ids = (
        get_requested_ids(post_path)
        if requested_ids is None else requested_ids
    )
    required_ids = required_ids or []
    candidate_blocks = []
    for bloc in blocks:
        try:
            root = get_root_node(bloc, requested_ids)
        except ParseException:
            continue
        if required_ids and not contains_target_id(root, required_ids):
            continue
        candidate_blocks.append(bloc)

    if not candidate_blocks:
        if required_ids:
            if has_bounded_target_payload(
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
        if jq.has(bloc, 'short_form_video_context'):
            score += 20
        if jq.has(bloc, 'creation_story'):
            score += 10
        if jq.has(bloc, 'comet_sections'):
            score += 5
        if jq.has(bloc, 'group_hoisted_feed'):
            score += 8
        if jq.has(bloc, 'video_home_www_related_videos_section') or jq.has(bloc, 'video_home_www_loe_video_permalink_seo_info'):
            score -= 20
        node_v2 = jq.find_first(bloc, 'node_v2')
        if isinstance(node_v2, dict):
            if node_v2.get('actors'):
                score += 30
            if 'feedback' in node_v2 and isinstance(node_v2['feedback'], dict):
                score += 15
            if 'comet_sections' in node_v2 or 'creation_story' in node_v2:
                score += 10
        data_blob = jq.find_first(bloc, 'data')
        if isinstance(data_blob, dict):
            if data_blob.get('actors'):
                score += 20
            if 'feedback' in data_blob and isinstance(data_blob['feedback'], dict):
                score += 10
        if 'require' in bloc and not node_v2 and not data_blob:
            score -= 10
        return score

    candidate_blocks.sort(
        key=lambda block: (
            contains_target_id(block, required_ids),
            contains_exact_id(block, requested_ids),
            contains_target_id(block, requested_ids),
            score_block(block),
        ),
        reverse=True,
    )
    for candidate in candidate_blocks:
        try:
            root = get_root_node(candidate, requested_ids)
            if required_ids and not contains_target_id(root, required_ids):
                continue
            return candidate
        except ParseException:
            continue
    if required_ids:
        if has_bounded_target_payload(blocks, required_ids):
            raise ParseException(
                'Facebook target payload shape is unsupported'
            )
        raise NoDataException(
            'Facebook response did not contain the requested post',
            reason_code='target_payload_missing',
        )
    return candidate_blocks[0]


def get_group_name(
    html_parser: BeautifulSoup,
    requested_ids: list[str] | None = None,
) -> str:
    candidates = [
        bloc for bloc in get_json_blocks(html_parser)
        if jq.has(bloc, 'group_member_profiles', 'formatted_count_text')
    ]
    requested_ids = requested_ids or []
    selected = select_requested_candidate(candidates, requested_ids)
    if selected is None:
        return ''
    for group_object in jq.find_all(selected, 'group'):
        if isinstance(group_object, dict) and 'name' in group_object:
            return group_object['name']
    return ''


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
            top_reaction_ids.extend(get_top_reaction_ids(fb))
        reaction_count = fb.get('reaction_count')
        if isinstance(reaction_count, dict):
            reaction_count = reaction_count.get('count')
        share_count = fb.get('share_count')
        if isinstance(share_count, dict):
            share_count = share_count.get('count')
        reactions = fb.get('i18n_reaction_count') or reaction_count or jq.find_first(fb, 'i18n_reaction_count') or '0'
        shares = fb.get('i18n_share_count') or share_count or jq.find_first(fb, 'i18n_share_count') or jq.find_first(fb, 'share_count') or '0'
        comments = fb.get('total_comment_count') or jq.find_first(fb, 'total_comment_count')
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
        for fb in jq.find_all(post_json, 'feedback'):
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

    post_feedback = jq.find_first(post_json, 'comet_ufi_summary_and_actions_renderer')
    if post_feedback and isinstance(post_feedback, dict) and 'feedback' in post_feedback:
        return extract_counts(post_feedback['feedback'])

    fb = jq.find_first(post_json, 'feedback')
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

    reactions = jq.find_first(post_json, 'i18n_reaction_count') or '0'
    shares = jq.find_first(post_json, 'i18n_share_count') or jq.find_first(post_json, 'share_count') or '0'
    comments = jq.find_first(post_json, 'total_comment_count')
    if not comments:
        cri = jq.find_first(post_json, 'comment_rendering_instance')
        if isinstance(cri, dict):
            cnode = cri.get('comments')
            if isinstance(cnode, dict):
                comments = cnode.get('total_count')
        if not comments:
            ccsr = jq.find_first(post_json, 'comments_count_summary_renderer')
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


def get_root_node(post_json: dict, requested_ids: list[str] | None = None) -> dict:
    requested_ids = requested_ids or []

    def selected(key: str, source: dict = post_json):
        return select_requested_candidate(jq.find_all(source, key), requested_ids)

    def work_normal_post() -> dict:
        data_blob = selected('data')
        if not isinstance(data_blob, dict):
            short_form = selected('short_form_video_context')
            if short_form:
                return {'creation_story': short_form}
            return {}
        if 'comet_ufi_summary_and_actions_renderer' in data_blob:
            return data_blob
        if 'node_v2' in data_blob and isinstance(data_blob['node_v2'], dict):
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


def process_post(
    post_path: str,
    http_response: CffiResponse | None = None,
) -> ParsedPost:
    with fetch_page(post_path, http_response=http_response) as html_parser:
        raw_ids = get_requested_ids(post_path)
        requested_ids, route_proven_ids = get_target_ids(html_parser, post_path)
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
            if facebook_path_segments(selected_url):
                effective_post_path = selected_url
                if urlparse(selected_url).path.startswith('/reel/'):
                    return reels.process_post(
                        selected_url, http_response=cffi.selected_get_response
                    )
        if is_share_path(post_path) and not raw_ids and not route_proven_ids:
            raise NoDataException(
                'Facebook response did not identify the requested share post'
            )
        required_ids = route_proven_ids or raw_ids
        post_json = get_root_node(
            get_post_json(
                html_parser,
                post_path,
                requested_ids,
                required_ids,
            ),
            requested_ids,
        )
        if required_ids and not contains_target_id(post_json, required_ids):
            raise NoDataException('Facebook response selected a different post')
        top_reaction_ids: list[str] = []
        likes, cmts, shares = get_interaction_counts(
            post_json, requested_ids, top_reaction_ids
        )

        post_date = -1
        t = jq.find_first(post_json, 'creation_time') or jq.find_first(post_json, 'created_time')
        if t:
            with contextlib.suppress(ValueError, TypeError):
                post_date = int(t)
        if post_date == -1:
            blocks = get_json_blocks(html_parser)
            t = (
                select_requested_field(blocks, 'creation_time', requested_ids)
                or select_requested_field(blocks, 'created_time', requested_ids)
            )
            if t:
                with contextlib.suppress(ValueError, TypeError):
                    post_date = int(t)

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
            ensure_full_url(effective_post_path)
        ).path
        if not story.video_links and re.search(
            r'/(?:reel|watch|videos|v)(?:/|$)', parsed_post_path, re.IGNORECASE
        ):
            with contextlib.suppress(FacebedException):
                story.video_links.append(
                    reels.get_video_link(
                        html_parser, requested_ids=requested_ids
                    )
                )
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
        story_url_ids = set(get_requested_ids(story.url))
        canonical_ids = set(get_requested_ids(canonical_url))
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
            post_url = story.url or ensure_full_url(post_path)
        else:
            post_url = ensure_full_url(effective_post_path)
        post_content = story.get_text()
        post_group_name = get_group_name(html_parser, requested_ids)
        post_author_name = story.author_name
        link_header = f'{post_author_name}' + (f' • {post_group_name}' if post_group_name else '')

        if story.author_id in get_config().banned_users:
            return banned(post_url)

        # TODO: support normal /watch here
        parsed = ParsedPost(link_header, post_content.strip(), story.image_links, post_url, post_date,
                            likes, cmts, shares, story.video_links,
                            top_reaction_ids=tuple(top_reaction_ids))
        try:
            embed_extract.fill_post(parsed, post_json, story_dict)
        except Exception:
            logging.warning(f'component embed extraction failed for {post_path}:\n{traceback.format_exc()}')
        return parsed
