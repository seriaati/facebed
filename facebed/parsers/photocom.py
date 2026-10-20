
from bs4 import BeautifulSoup
from curl_cffi.requests import Response as CffiResponse

from facebed import jq
from facebed.errors import ParseException
from facebed.formatting import get_top_reaction_ids, human_format
from facebed.models.post import ParsedPost
from facebed.parsers import photo
from facebed.parsers.blocks import get_json_blocks
from facebed.parsers.fetch import fetch_page
from facebed.parsers.identity import get_target_ids


def get_content_node(
    html_parser: BeautifulSoup,
    requested_ids: list[str] | None = None,
) -> dict:
    candidates = []
    for bloc in get_json_blocks(html_parser):
        if jq.has(bloc, 'attached_comment') and not jq.has(bloc, 'unified_reactors'):
            candidates.extend(
                node for node in jq.find_all(bloc, 'result') if isinstance(node, dict)
            )
    return photo.select_node(
        candidates,
        requested_ids or [],
        'Cannot process photocom (cn)',
    )


def get_media_node(
    html_parser: BeautifulSoup,
    requested_ids: list[str] | None = None,
) -> dict:
    candidates = [
        bloc for bloc in get_json_blocks(html_parser)
        if jq.has(bloc, 'attached_comment', 'unified_reactors')
    ]
    return photo.select_node(
        candidates,
        requested_ids or [],
        'Cannot process photocom (media)',
    )


def get_reaction_count(media_node: dict) -> int:
    feedback = get_reaction_feedback(media_node)
    reactors = feedback.get('unified_reactors')
    if not isinstance(reactors, dict) or 'count' not in reactors:
        reactors = jq.find_first(media_node, 'unified_reactors')
    if isinstance(reactors, dict) and 'count' in reactors:
        return reactors['count']
    raise ParseException('Cannot process photocom (rc)')


def get_reaction_feedback(media_node: dict) -> dict:
    cur = jq.find_first(media_node, 'currMedia')
    if isinstance(cur, dict):
        attached_comment = cur.get('attached_comment')
        if isinstance(attached_comment, dict):
            feedback = attached_comment.get('feedback')
            if isinstance(feedback, dict):
                return feedback
    raise ParseException('Cannot process photocom (rf)')


def get_attached_image_and_url(media_node: dict) -> tuple[str, str]:
    cur = jq.find_first(media_node, 'currMedia')
    if isinstance(cur, dict):
        return str(cur['image']['uri']), str(cur['attached_comment']['feedback']['url'])
    raise ParseException('Cannot process photocom (iau)')


def process_post(
    post_path: str,
    http_response: CffiResponse | None = None,
) -> ParsedPost:
    if http_response is None:
        page = fetch_page(post_path)
    else:
        page = fetch_page(post_path, http_response=http_response)
    with page as html_parser:
        requested_ids, _route_proven_ids = get_target_ids(
            html_parser, post_path
        )
        content_node = get_content_node(html_parser, requested_ids)
        media_node = get_media_node(html_parser, requested_ids)
        body = content_node['data']['attached_comment']['preferred_body']

        op_name = content_node['data']['owner']['name'] + ' (💬)'
        post_text = '' if body is None else body['text']
        post_time = content_node['data']['created_time']
        post_image, post_url = get_attached_image_and_url(media_node)
        reaction_count = get_reaction_count(media_node)
        top_reaction_ids = get_top_reaction_ids(
            get_reaction_feedback(media_node)
        )

        return ParsedPost(op_name, post_text, [post_image], post_url, post_time,
                          human_format(reaction_count), 'null', 'null', [],
                          top_reaction_ids=top_reaction_ids)
