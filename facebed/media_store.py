"""slot -> real media URL for the short /m/<key>/<slot> redirects in component embeds."""

from collections import OrderedDict

from facebed.client import cffi
from facebed.models.post import ParsedPost
from facebed.parsers import generic, photo, reels, video_watch

MAX_POSTS = 1000
_slots: OrderedDict = OrderedDict()


def register(post: ParsedPost):
    if not post.key:
        return
    slots = {'a': post.avatar}
    slots.update({str(i): url for i, (_, url) in enumerate(post.media[:10])})
    if isinstance(post.shared, dict):
        slots['qa'] = post.shared['avatar']
        slots.update({f'q{i}': url for i, (_, url) in enumerate(post.shared['media'][:10])})
    _slots[post.key] = {k: v for k, v in slots.items() if v}
    _slots.move_to_end(post.key)
    while len(_slots) > MAX_POSTS:
        _slots.popitem(last=False)


def refetch(key: str) -> ParsedPost:
    if key.isdigit():
        return generic.process_post(key)
    ident = key[1:]
    if key[0] == 'r':
        return reels.process_post(f'reel/{ident}')
    if key[0] == 'w':
        return video_watch.process_post(f'watch/?v={ident}')
    return photo.process_post(f'photo/?fbid={ident}')


def resolve(key: str, slot: str) -> str:
    if key not in _slots:
        with cffi.request_scope():
            register(refetch(key))
    return _slots.get(key, {}).get(slot, '')
