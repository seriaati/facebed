from dataclasses import dataclass, field

from facebed.utils import Utils


@dataclass
class ParsedPost:
    author_name: str
    text: str
    image_links: list[str]
    url: str
    date: int

    likes: str
    comments: str
    shares: str
    video_links: list[str]
    top_reaction_ids: tuple[str, ...] = ()

    # optional extras for the Discord component embed
    key: str = ''  # short id used by the /m/<key>/<slot> media redirects
    author: str = ''
    author_url: str = ''
    avatar: str = ''
    verified: bool = False
    own_text: str | None = None  # caption without the shared post appended
    media: list[tuple[str, str]] = field(default_factory=list)  # ordered ('image'|'video', url)
    counts: tuple[int | None, int | None, int | None] = (None, None, None)  # exact likes, comments, shares
    group_name: str = ''
    group_url: str = ''
    shared: dict | str | None = None  # shared post fields, or 'unavailable'


def banned(url: str) -> ParsedPost:
    Utils.warn(f'banned embed attempted "{url}"')
    return ParsedPost('Banned', 'This user is banned by the operators of this embed server',
                      [], 'https://banned.facebook.com', -1,
                      'null', 'null', 'null', [])
