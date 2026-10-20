import base64
import re
from urllib.parse import parse_qs, urlparse

from curl_cffi.requests import Response as CffiResponse

from facebed.config import get_config
from facebed.errors import NoDataException, ParseException, UnsupportedRouteException
from facebed.jq import Jq
from facebed.models.post import ParsedPost, banned
from facebed.parsers.json_parser import JsonParser
from facebed.utils import Utils


class CommentParser:
    """Select one comment without borrowing content from its parent story."""

    @staticmethod
    def get_target(post_path: str) -> tuple[str, str | None] | None:
        query = parse_qs(urlparse(post_path).query, keep_blank_values=True)
        if not {'comment_id', 'reply_comment_id'}.intersection(query):
            return None
        comment = query.get('comment_id', [])
        reply = query.get('reply_comment_id')
        if (
            len(comment) != 1
            or not re.fullmatch(r'[0-9]+', comment[0])
            or (reply is not None and (
                len(reply) != 1 or not re.fullmatch(r'[0-9]+', reply[0])
                or reply[0] == comment[0]
            ))
        ):
            raise NoDataException(
                'Invalid or ambiguous Facebook comment identity',
                reason_code='invalid_comment_target', terminal=True,
            )
        return comment[0], reply[0] if reply is not None else None

    @staticmethod
    def comment_id(node: dict) -> str | None:
        if not isinstance(node, dict):
            return None
        legacy_id = str(node.get('legacy_fbid') or '')
        encoded = str(node.get('id') or '')
        try:
            decoded = base64.b64decode(
                encoded + '=' * (-len(encoded) % 4), validate=True
            ).decode('utf-8')
        except (ValueError, UnicodeDecodeError):
            decoded = ''
        match = re.fullmatch(r'comment:[0-9]+_([0-9]+)', decoded)
        opaque_id = match[1] if match else None
        if legacy_id and opaque_id and legacy_id != opaque_id:
            return None
        if legacy_id.isascii() and legacy_id.isdigit():
            return legacy_id
        return opaque_id

    @staticmethod
    def select_comment(
        blocks: list[dict], target: tuple[str, str | None], post_ids: list[str]
    ) -> dict:
        comment_id, reply_id = target
        wanted_id = reply_id or comment_id
        candidates = []
        for block in blocks:
            for node in Jq.enumerate(block):
                if node.get('__typename') != 'Comment':
                    continue
                if CommentParser.comment_id(node) != wanted_id:
                    continue
                if not isinstance(node.get('author'), dict) or not (
                    'body' in node or 'preferred_body' in node
                ):
                    continue
                parent = node.get('comment_direct_parent')
                if reply_id and isinstance(parent, dict):
                    if CommentParser.comment_id(parent) != comment_id:
                        continue
                feedback = node.get('feedback')
                urls = [node.get('url')]
                if isinstance(feedback, dict):
                    urls.append(feedback.get('url'))
                for action in node.get('comment_action_links') or []:
                    stamp = action.get('comment') if isinstance(action, dict) else None
                    if isinstance(stamp, dict) and CommentParser.comment_id(stamp) == wanted_id:
                        urls.append(stamp.get('url'))
                matched_url = False
                for url in urls:
                    if not isinstance(url, str) or not Utils.is_facebook_url(url, require_https=True):
                        continue
                    try:
                        matches = CommentParser.get_target(url) == target
                    except NoDataException:
                        matches = False
                    if matches and set(JsonParser.get_requested_ids(url)).intersection(post_ids):
                        matched_url = True
                        break
                if matched_url:
                    candidates.append(node)
        if not candidates:
            raise NoDataException(
                'Facebook response did not contain the requested comment',
                reason_code='comment_payload_missing', terminal=True,
            )
        # Full comment renderers include attachments and feedback; timestamp and
        # parent-reference fragments with the same ID must not shadow them.
        return max(candidates, key=lambda node: (
            'attachments' in node, 'preferred_body' in node,
            isinstance(node.get('feedback'), dict), len(node),
        ))

    @staticmethod
    def get_media(comment: dict) -> tuple[list[str], list[str]]:
        images: list[str] = []
        videos: list[str] = []
        for wrapper in comment.get('attachments') or []:
            if not isinstance(wrapper, dict):
                continue
            renderer = wrapper.get('style_type_renderer')
            attachment = renderer.get('attachment') if isinstance(renderer, dict) else wrapper
            if not isinstance(attachment, dict):
                continue
            media = attachment.get('media')
            if not isinstance(media, dict):
                continue
            video = JsonParser.direct_video_url(media)
            if video:
                if video not in videos:
                    videos.append(video)
                continue
            # These are direct fields of the selected attachment, never an
            # image discovered in a reply, author avatar or parent-post subtree.
            for key in ('animated_image', 'sticker_image', 'image', 'photo_image', 'fallback_image'):
                image = media.get(key)
                link = image.get('uri') if isinstance(image, dict) else None
                if isinstance(link, str) and Utils.is_safe_media_url(link):
                    if link not in images:
                        images.append(link)
                    break
        return images, videos

    @staticmethod
    def get_counts(comment: dict) -> tuple[str, str, tuple[str, ...]]:
        feedback = comment.get('feedback') or {}
        reactions = feedback.get('reaction_count')
        if isinstance(reactions, dict):
            reactions = reactions.get('count')
        if reactions is None:
            reactions = (feedback.get('reactors') or {}).get('count_reduced')
        if reactions is None:
            reactions = (feedback.get('unified_reactors') or {}).get('count')
        replies = feedback.get('replies_fields') or feedback.get('replies') or {}
        reply_count = replies.get('total_count', replies.get('count'))
        return (
            Utils.human_format(reactions) if reactions is not None else 'null',
            Utils.human_format(reply_count) if reply_count is not None else 'null',
            Utils.get_top_reaction_ids(feedback),
        )

    @staticmethod
    def process_post(post_path: str, http_response: CffiResponse | None = None) -> ParsedPost:
        target = CommentParser.get_target(post_path)
        if target is None:
            raise UnsupportedRouteException('A comment permalink is required')
        with JsonParser.fetch_page(post_path, http_response=http_response) as html_parser:
            post_ids, _ = JsonParser.get_target_ids(html_parser, post_path)
            comment = CommentParser.select_comment(
                JsonParser.get_json_blocks(html_parser), target, post_ids
            )
            author = comment['author']
            if not isinstance(author.get('name'), str) or not author['name']:
                raise ParseException('Facebook comment has no author name')
            url = JsonParser.ensure_full_url(post_path)
            if author.get('id') in get_config().banned_users:
                return banned(url)
            body = comment.get('body')
            if body is None:
                body = comment.get('preferred_body')
            text = body.get('text', '') if isinstance(body, dict) else ''
            images, videos = CommentParser.get_media(comment)
            if not isinstance(text, str) or not (text.strip() or images or videos):
                raise NoDataException(
                    'Requested comment has no available text or media',
                    reason_code='comment_content_unavailable', terminal=True,
                )
            reactions, replies, reaction_ids = CommentParser.get_counts(comment)
            timestamp = comment.get('created_time')
            if not isinstance(timestamp, int):
                raise ParseException('Facebook comment has no creation timestamp')
            return ParsedPost(
                author['name'] + ' (💬)', text.strip(), images, url,
                timestamp, reactions, replies, 'null', videos,
                top_reaction_ids=reaction_ids,
            )
