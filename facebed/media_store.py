from collections import OrderedDict

from facebed.client import cffi
from facebed.models.post import ParsedPost
from facebed.parsers.json_parser import JsonParser
from facebed.parsers.photo import SinglePhotoParser
from facebed.parsers.reels import ReelsParser
from facebed.parsers.video_watch import VideoWatchParser


class MediaStore:
    """slot -> real media URL for the short /m/<key>/<slot> redirects in component embeds."""
    MAX_POSTS = 1000
    _slots: OrderedDict = OrderedDict()

    @staticmethod
    def register(post: ParsedPost):
        if not post.key:
            return
        slots = {'a': post.avatar}
        slots.update({str(i): url for i, (_, url) in enumerate(post.media[:10])})
        if isinstance(post.shared, dict):
            slots['qa'] = post.shared['avatar']
            slots.update({f'q{i}': url for i, (_, url) in enumerate(post.shared['media'][:10])})
        MediaStore._slots[post.key] = {k: v for k, v in slots.items() if v}
        MediaStore._slots.move_to_end(post.key)
        while len(MediaStore._slots) > MediaStore.MAX_POSTS:
            MediaStore._slots.popitem(last=False)

    @staticmethod
    def refetch(key: str) -> ParsedPost:
        if key.isdigit():
            return JsonParser.process_post(key)
        ident = key[1:]
        if key[0] == 'r':
            return ReelsParser.process_post(f'reel/{ident}')
        if key[0] == 'w':
            return VideoWatchParser.process_post(f'watch/?v={ident}')
        return SinglePhotoParser.process_post(f'photo/?fbid={ident}')

    @staticmethod
    def resolve(key: str, slot: str) -> str:
        if key not in MediaStore._slots:
            with cffi.request_scope():
                MediaStore.register(MediaStore.refetch(key))
        return MediaStore._slots.get(key, {}).get(slot, '')
