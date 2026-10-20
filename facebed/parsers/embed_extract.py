"""Extra post fields for the component embed, read beside the existing parsing."""



import contextlib

from facebed import jq
from facebed.errors import FacebedException
from facebed.models.post import ParsedPost
from facebed.parsers import reels

VERIFIED = 'CometFeedUserVerifiedBadgeStrategy'


def image_uri(media: dict) -> str:
    for k in ('viewer_image', 'photo_image', 'image'):
        if isinstance(media.get(k), dict) and media[k].get('uri'):
            return media[k]['uri']
    return ''


def media(story: dict) -> list[tuple[str, str]]:
    out = []
    for a in story.get('attachments') or []:
        at = ((a or {}).get('styles') or {}).get('attachment') or {}
        nodes = [(n or {}).get('media') or {} for n in (at.get('all_subattachments') or {}).get('nodes') or []]
        for m in nodes or [at.get('media') or {}]:
            if m.get('__typename') == 'Video':
                with contextlib.suppress(FacebedException):
                    out.append(('video', reels.get_video_link(None, user_node=m)))
            elif m.get('__typename') == 'Photo' and image_uri(m):
                out.append(('image', image_uri(m)))
    return out


def badge(title_section) -> bool:
    if not isinstance(title_section, dict):
        return False
    sections = (title_section.get('story') or {}).get('comet_sections') or {}
    return (sections.get('badge') or {}).get('__typename') == VERIFIED


def raw_counts(node: dict) -> tuple[int | None, int | None, int | None]:
    renderer = jq.find_first(node, 'comet_ufi_summary_and_actions_renderer')
    fb = renderer.get('feedback') if isinstance(renderer, dict) else None
    if not isinstance(fb, dict):
        return None, None, None
    likes = (fb.get('reaction_count') or {}).get('count')
    shares = (fb.get('share_count') or {}).get('count')
    cmts = jq.find_first(fb, 'total_count')
    return likes, cmts if isinstance(cmts, int) else None, shares


def creation_time(node) -> int:
    t = jq.find_first(node, 'creation_time')
    return int(t) if isinstance(t, (int, str)) and str(t).isdigit() else -1


def fill_post(post: ParsedPost, root: dict, story: dict):
    actor = (story.get('actors') or [{}])[0] or {}
    title = (((root.get('context_layout') or {}).get('story') or {}).get('comet_sections') or {}).get('title') or {}
    to = (title.get('story') or {}).get('to') or {}
    post.key = str(story.get('post_id') or '')
    post.author = actor.get('name') or ''
    post.author_url = actor.get('url') or actor.get('profile_url') or ''
    post.avatar = (actor.get('profile_picture') or {}).get('uri') or ''
    post.verified = badge(title)
    post.own_text = (story.get('message') or {}).get('text') or ''
    post.media = media(story)
    post.counts = raw_counts(root)
    if to.get('__typename') == 'Group':
        post.group_name, post.group_url = to.get('name') or '', to.get('url') or ''

    shared = story.get('attached_story')
    if isinstance(shared, dict) and shared.get('actors'):
        shared_actor = shared['actors'][0] or {}
        layout = jq.find_first(story.get('comet_sections') or {}, 'attached_story_layout')
        layout = ((layout or {}).get('story') or {}).get('comet_sections') or {} if isinstance(layout, dict) else {}
        post.shared = {
            'author': shared_actor.get('name') or '',
            'avatar': (shared_actor.get('profile_picture') or {}).get('uri') or '',
            'verified': badge(layout.get('title')),
            'text': (shared.get('message') or {}).get('text') or '',
            'url': shared.get('wwwURL') or '',
            'date': creation_time(layout.get('metadata') or {}),
            'media': media(shared),
        }
    elif isinstance(shared, dict):
        post.shared = 'unavailable'
