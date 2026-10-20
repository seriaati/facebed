from facebed.errors import FacebedException
from facebed.jq import Jq
from facebed.models.post import ParsedPost


class EmbedExtract:
    """Extra post fields for the component embed, read beside the existing parsing."""
    VERIFIED = 'CometFeedUserVerifiedBadgeStrategy'

    @staticmethod
    def image_uri(media: dict) -> str:
        for k in ('viewer_image', 'photo_image', 'image'):
            if isinstance(media.get(k), dict) and media[k].get('uri'):
                return media[k]['uri']
        return ''

    @staticmethod
    def media(story: dict) -> list[tuple[str, str]]:
        from facebed.parsers.reels import ReelsParser  # circular import

        out = []
        for a in story.get('attachments') or []:
            at = ((a or {}).get('styles') or {}).get('attachment') or {}
            nodes = [(n or {}).get('media') or {} for n in (at.get('all_subattachments') or {}).get('nodes') or []]
            for m in nodes or [at.get('media') or {}]:
                if m.get('__typename') == 'Video':
                    try:
                        out.append(('video', ReelsParser.get_video_link(None, user_node=m)))
                    except FacebedException:
                        pass
                elif m.get('__typename') == 'Photo' and EmbedExtract.image_uri(m):
                    out.append(('image', EmbedExtract.image_uri(m)))
        return out

    @staticmethod
    def badge(title_section) -> bool:
        if not isinstance(title_section, dict):
            return False
        sections = (title_section.get('story') or {}).get('comet_sections') or {}
        return (sections.get('badge') or {}).get('__typename') == EmbedExtract.VERIFIED

    @staticmethod
    def raw_counts(node: dict) -> tuple[int | None, int | None, int | None]:
        renderer = Jq.first(node, 'comet_ufi_summary_and_actions_renderer')
        fb = renderer.get('feedback') if isinstance(renderer, dict) else None
        if not isinstance(fb, dict):
            return None, None, None
        likes = (fb.get('reaction_count') or {}).get('count')
        shares = (fb.get('share_count') or {}).get('count')
        cmts = Jq.first(fb, 'total_count')
        return likes, cmts if isinstance(cmts, int) else None, shares

    @staticmethod
    def creation_time(node) -> int:
        t = Jq.first(node, 'creation_time')
        return int(t) if isinstance(t, (int, str)) and str(t).isdigit() else -1

    @staticmethod
    def fill_post(post: ParsedPost, root: dict, story: dict):
        actor = (story.get('actors') or [{}])[0] or {}
        title = (((root.get('context_layout') or {}).get('story') or {}).get('comet_sections') or {}).get('title') or {}
        to = (title.get('story') or {}).get('to') or {}
        post.key = str(story.get('post_id') or '')
        post.author = actor.get('name') or ''
        post.author_url = actor.get('url') or actor.get('profile_url') or ''
        post.avatar = (actor.get('profile_picture') or {}).get('uri') or ''
        post.verified = EmbedExtract.badge(title)
        post.own_text = (story.get('message') or {}).get('text') or ''
        post.media = EmbedExtract.media(story)
        post.counts = EmbedExtract.raw_counts(root)
        if to.get('__typename') == 'Group':
            post.group_name, post.group_url = to.get('name') or '', to.get('url') or ''

        shared = story.get('attached_story')
        if isinstance(shared, dict) and shared.get('actors'):
            shared_actor = shared['actors'][0] or {}
            layout = Jq.first(story.get('comet_sections') or {}, 'attached_story_layout')
            layout = ((layout or {}).get('story') or {}).get('comet_sections') or {} if isinstance(layout, dict) else {}
            post.shared = {
                'author': shared_actor.get('name') or '',
                'avatar': (shared_actor.get('profile_picture') or {}).get('uri') or '',
                'verified': EmbedExtract.badge(layout.get('title')),
                'text': (shared.get('message') or {}).get('text') or '',
                'url': shared.get('wwwURL') or '',
                'date': EmbedExtract.creation_time(layout.get('metadata') or {}),
                'media': EmbedExtract.media(shared),
            }
        elif isinstance(shared, dict):
            post.shared = 'unavailable'
