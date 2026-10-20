import base64
import logging
import re
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from curl_cffi.requests import Response as CffiResponse

from facebed import jq
from facebed.errors import NoDataException, ParseException, shape_errors
from facebed.models.post import ParsedPost
from facebed.parsers import embed_extract, generic
from facebed.parsers.blocks import get_json_blocks
from facebed.parsers.fetch import fetch_page
from facebed.parsers.identity import (
    contains_exact_id,
    get_target_ids,
    select_requested_candidate,
)
from facebed.urls import ensure_full_url

logger = logging.getLogger(__name__)


# narrower than identity.IDENTITY_FIELDS; widening it changes which interaction ids are collected
PHOTO_IDENTITY_FIELDS = frozenset({'fbid', 'feedback_id', 'id', 'legacy_fbid', 'mf_story_key', 'post_id', 'story_fbid', 'top_level_post_id', 'video_id'})


def select_node(
    candidates: list[dict],
    requested_ids: list[str],
    error_message: str,
) -> dict:
    ordered = (
        sorted(candidates, key=lambda item: len(str(item)))
        if requested_ids else candidates
    )
    selected = select_requested_candidate(ordered, requested_ids)
    if selected is not None:
        return selected
    if candidates and requested_ids:
        raise NoDataException('Facebook response did not contain the requested photo')
    raise ParseException(error_message)


def get_content_node(
    html_parser: BeautifulSoup,
    requested_ids: list[str] | None = None,
) -> dict:
    candidates = []
    for bloc in get_json_blocks(html_parser):
        if jq.has(bloc, 'message_preferred_body', 'container_story'):
            candidates.append(bloc)
    selected_block = select_node(
        candidates,
        requested_ids or [],
        'Cannot process post (cn)',
    )
    data_nodes = [
        node for node in jq.find_all(selected_block, 'data') if isinstance(node, dict)
    ]
    selected_data = select_requested_candidate(
        data_nodes, requested_ids or []
    )
    if selected_data is not None:
        return selected_data
    if len(data_nodes) == 1 and not requested_ids:
        return data_nodes[0]
    if len(data_nodes) == 1 and contains_exact_id(
        selected_block, requested_ids or []
    ):
        return data_nodes[0]
    raise NoDataException('Facebook response did not contain the requested photo')


def get_interactions_node(
    html_parser: BeautifulSoup,
    requested_ids: list[str] | None = None,
) -> dict | None:
    candidates = []
    for bloc in get_json_blocks(html_parser):
        if jq.has(bloc, 'comet_ufi_summary_and_actions_renderer'):
            candidates.append(bloc)
    try:
        return select_node(
            candidates,
            requested_ids or [],
            'Cannot process post (in)',
        )
    except NoDataException:
        if len(candidates) == 1:
            return None
        raise


def get_single_image(
    html_parser: BeautifulSoup,
    requested_ids: list[str] | None = None,
) -> str:
    candidates = []
    for bloc in get_json_blocks(html_parser):
        if jq.has(bloc, 'prefetch_uris_v2'):
            candidates.append(bloc)
    selected_block = select_node(
        candidates,
        requested_ids or [],
        'cannot find single image',
    )
    prefetch = jq.find_first(selected_block, 'prefetch_uris_v2')
    if not isinstance(prefetch, list) or not prefetch:
        raise ParseException('cannot find single image')
    return str(prefetch[0]['uri'])


@shape_errors()
def process_post(
    post_path: str,
    http_response: CffiResponse | None = None,
) -> ParsedPost:
    with fetch_page(post_path, http_response=http_response) as html_parser:
        requested_ids, _route_proven_ids = get_target_ids(
            html_parser, post_path
        )
        content_node = get_content_node(html_parser, requested_ids)
        interaction_ids = list(requested_ids)
        identity_sources = [content_node]
        for key in ('container_story', 'creation_story'):
            source = jq.find_first(content_node, key)
            if isinstance(source, dict):
                identity_sources.append(source)
        for source in identity_sources:
            for key, value in source.items():
                if str(key).lower() in PHOTO_IDENTITY_FIELDS and isinstance(value, (str, int)):
                    text = str(value)
                    interaction_ids.append(text)
                    try:
                        decoded = base64.b64decode(
                            text + ('=' * (-len(text) % 4)), validate=True
                        ).decode('utf-8')
                    except (ValueError, UnicodeDecodeError):
                        continue
                    numeric_parts = re.findall(r'\d+', decoded)
                    if numeric_parts:
                        interaction_ids.append(numeric_parts[-1])
        interaction_ids = list(dict.fromkeys(interaction_ids))
        interaction_node = get_interactions_node(
            html_parser, interaction_ids
        )

        post_text = content_node['message']['text'] if content_node['message'] and 'text' in content_node['message'] else ''
        post_author = content_node['owner']['name']
        post_date = content_node['created_time']
        if interaction_node is None:
            likes, cmts, shares = '0', '0', '0'
            top_reaction_ids: list[str] = []
        else:
            top_reaction_ids = []
            likes, cmts, shares = generic.get_interaction_counts(
                interaction_node, interaction_ids, top_reaction_ids
            )
        image_url = get_single_image(html_parser, requested_ids)

        parsed = ParsedPost(post_author, post_text.strip(), [image_url], ensure_full_url(post_path),
                            post_date, likes, cmts, shares, [],
                            top_reaction_ids=tuple(top_reaction_ids))
        try:
            fbid = parse_qs(urlparse(post_path).query).get('fbid', [''])[0]
            parsed.key = f'f{fbid}' if fbid.isdigit() else ''
            parsed.author = post_author
            parsed.avatar = (content_node['owner'].get('profile_picture') or {}).get('uri') or ''
            parsed.media = [('image', image_url)]
            if interaction_node is not None:
                parsed.counts = embed_extract.raw_counts(interaction_node)
        except Exception:
            logger.warning('component embed extraction failed for %s', post_path, exc_info=True)
        return parsed
