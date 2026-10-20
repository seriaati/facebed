import logging
import traceback
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from curl_cffi.requests import Response as CffiResponse

from facebed.errors import NoDataException, ParseException
from facebed.jq import Jq
from facebed.models.post import ParsedPost
from facebed.parsers.json_parser import JsonParser
from facebed.parsers.reels import ReelsParser
from facebed.utils import Utils


class VideoWatchParser:
    # excluding group post video since they are handled by jsonparser
    @staticmethod
    def get_op_name(
        html_parser: BeautifulSoup,
        content_node: dict | None = None,
        requested_ids: list[str] | None = None,
    ) -> str:
        if content_node:
            owner = content_node.get('owner') or Jq.first(content_node, 'owner')
            if isinstance(owner, dict) and owner.get('name'):
                return owner['name']
        blocks = JsonParser.get_json_blocks(html_parser)
        requested_ids = requested_ids or []
        owner = JsonParser.select_requested_field(blocks, 'owner', requested_ids)
        if isinstance(owner, dict) and owner.get('name'):
            return owner['name']
        raise ParseException('Invalid watch link (opn)')

    @staticmethod
    def get_content_node(html_parser: BeautifulSoup, requested_ids: list[str] | None = None) -> dict:
        candidates = []
        for bloc in JsonParser.get_json_blocks(html_parser):
            if Jq.has(bloc,'comment_rendering_instance', 'video_view_count_renderer'):
                for result in Jq.all(bloc, 'result'):
                    if isinstance(result, dict) and isinstance(result.get('data'), dict):
                        candidates.append(result['data'])

        requested_ids = requested_ids or []
        for node in candidates:
            if JsonParser.contains_exact_id(node, requested_ids):
                return node
        for node in candidates:
            if JsonParser.contains_target_id(node, requested_ids):
                return node
        if requested_ids:
            raise NoDataException('Facebook response did not contain the requested video')
        if candidates:
            return candidates[0]
        canonical = html_parser.find('link', attrs={'rel': 'canonical'})
        if canonical:
            canonical_path = urlparse(canonical.get('href', '')).path.rstrip('/')
            if canonical_path == '/watch':
                raise NoDataException('Facebook served generic watch feed instead of specific video')
        raise ParseException('Invalid watch link (cn)')

    @staticmethod
    def get_date(
        html_parser: BeautifulSoup,
        content_node: dict | None = None,
        requested_ids: list[str] | None = None,
    ) -> int:
        if content_node:
            creation_time = content_node.get('creation_time') or Jq.first(content_node, 'creation_time')
            if creation_time:
                return int(creation_time)
        blocks = JsonParser.get_json_blocks(html_parser)
        requested_ids = requested_ids or []
        creation_time = JsonParser.select_requested_field(blocks, 'creation_time', requested_ids)
        if creation_time:
            return int(creation_time)
        raise ParseException('cannot find date')

    @staticmethod
    def process_post(
        post_path: str,
        http_response: CffiResponse | None = None,
    ) -> ParsedPost:
        if http_response is None:
            page = JsonParser.fetch_page(post_path)
        else:
            page = JsonParser.fetch_page(post_path, http_response=http_response)
        with page as html_parser:
            requested_ids, _strong_ids = JsonParser.get_target_ids(html_parser, post_path)
            content_node = VideoWatchParser.get_content_node(html_parser, requested_ids)

            video_link = ReelsParser.get_video_link(html_parser, requested_ids=requested_ids)

            post_url = JsonParser.ensure_full_url(post_path)
            op_name = VideoWatchParser.get_op_name(html_parser, content_node, requested_ids)
            post_text = content_node['title']['text'] if (content_node.get('title') and isinstance(content_node['title'], dict) and content_node['title'].get('text')) else ''
            if not post_text:
                msg = Jq.first(content_node, 'message')
                if isinstance(msg, dict):
                    post_text = msg.get('text', '')

            post_feedback = content_node['feedback']
            likes = Utils.human_format(post_feedback['reaction_count']['count'])
            shares = 'null'
            cmts = Utils.human_format(post_feedback['total_comment_count'])
            post_date = VideoWatchParser.get_date(html_parser, content_node, requested_ids)
            top_reaction_ids = Utils.get_top_reaction_ids(post_feedback)

            parsed = ParsedPost(op_name, post_text, [], post_url, post_date,
                                likes, cmts, shares, [video_link],
                                top_reaction_ids=top_reaction_ids)
            try:
                v = parse_qs(urlparse(post_path).query).get('v', [''])[0]
                parsed.key = f'w{v}' if v.isdigit() else ''
                parsed.author = op_name
                parsed.media = [('video', video_link)]
                parsed.counts = (post_feedback['reaction_count']['count'],
                                 post_feedback['total_comment_count'], None)
            except Exception:
                logging.warning(f'component embed extraction failed for {post_path}:\n{traceback.format_exc()}')
            return parsed
