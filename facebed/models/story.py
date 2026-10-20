from typing import Self

from facebed import jq
from facebed.errors import FacebedException
from facebed.parsers import reels


class Story:
    author_name: str
    text: str
    image_links: list[str]
    video_links: list[str]
    url: str

    author_id: int
    attached_story: Self

    def __init__(self, story_json: dict):
        if 'actors' in story_json or ('node_v2' not in story_json and ('comet_sections' in story_json or 'creation_story' in story_json or 'feedback' in story_json or 'attachments' in story_json)):
            node_v2 = story_json
        else:
            node_v2 = jq.find_first(story_json, 'node_v2')
        if not isinstance(node_v2, dict):
            node_v2 = {}

        self.author_name = ''
        if 'actors' in story_json and story_json['actors'] and isinstance(story_json['actors'], list) and len(story_json['actors']) > 0 and 'name' in story_json['actors'][0]:
            self.author_name = story_json['actors'][0]['name']
        elif node_v2.get('actors') and isinstance(node_v2['actors'], list) and len(node_v2['actors']) > 0 and 'name' in node_v2['actors'][0]:
            self.author_name = node_v2['actors'][0]['name']
        elif node_v2.get('name') and isinstance(node_v2['name'], str) and len(node_v2['name']) > 2:
            self.author_name = node_v2['name']
        elif node_v2.get('short_name'):
            self.author_name = node_v2['short_name']
        elif story_json.get('name') and isinstance(story_json['name'], str) and len(story_json['name']) > 2:
            self.author_name = story_json['name']
        else:
            self.author_name = jq.find_first(story_json, 'name') or jq.find_first(story_json, 'localized_name') or ''

        self.text = ''
        if 'message' in story_json and story_json['message'] and 'text' in story_json['message']:
            self.text = story_json['message']['text']
        elif story_json.get('text') and isinstance(story_json['text'], str):
            self.text = story_json['text']
        else:
            self.text = jq.find_first(story_json, 'text') or ''

        self.image_links = self.get_image_links_post_json(story_json)
        self.video_links = self.get_video_links(story_json)

        self.url = story_json.get('wwwURL') or node_v2.get('wwwURL') or story_json.get('url') or ''
        if not isinstance(self.url, str):
            self.url = ''

        self.author_id = ''
        if 'actors' in story_json and story_json['actors'] and isinstance(story_json['actors'], list) and len(story_json['actors']) > 0 and 'id' in story_json['actors'][0]:
            self.author_id = story_json['actors'][0]['id']
        elif node_v2.get('actors') and isinstance(node_v2['actors'], list) and len(node_v2['actors']) > 0 and 'id' in node_v2['actors'][0]:
            self.author_id = node_v2['actors'][0]['id']
        elif node_v2.get('id'):
            self.author_id = node_v2['id']
        else:
            self.author_id = jq.find_first(story_json, 'id') or ''

        if 'attached_story' in story_json and story_json['attached_story'] and 'actors' in story_json['attached_story']:
            self.attached_story = Story(story_json['attached_story'])
            self.image_links.extend([x for x in self.attached_story.image_links if x not in self.image_links])
            self.video_links.extend([x for x in self.attached_story.video_links if x not in self.video_links])
        else:
            self.attached_story = None

    # TODO: find better format for this
    def get_text(self) -> str:
        text = self.text
        if self.attached_story:
            text += f'\n╰┈➤ {self.attached_story.author_name}\n{self.attached_story.text}'
        return text

    @staticmethod
    def get_video_links(post_json: dict) -> list[str]:
        video_links = []
        for attachment_set in jq.find_all(post_json, 'attachment'):
            try:
                link = reels.get_video_link(None, user_node=attachment_set)
                if link not in video_links:
                    video_links.append(link)
            except FacebedException:
                pass

        return video_links

    @staticmethod
    def get_image_links_post_json(post_json: dict) -> list[str]:
        def is_sticker(value: dict) -> bool:
            return any(
                isinstance(node, dict) and node.get('__typename') == 'Sticker'
                for node in [value, *jq.nodes(value)]
            )

        all_attachments = jq.find_all(post_json, 'attachment')
        for attachment_set in all_attachments:
            if any(k.endswith('subattachments') for k in attachment_set):
                subsets = [v for k, v in attachment_set.items() if k.endswith('subattachments') and 'nodes' in v]
                if subsets:
                    max_imgage_count = len(max(subsets, key=lambda it: len(it['nodes']))['nodes'])
                    subsets = [subset for subset in subsets if
                               len(subset['nodes']) == max_imgage_count and jq.find_all(subset, 'viewer_image')]
                    if subsets:
                        images = [x['uri'] for x in jq.find_all(subsets[0], 'viewer_image')]
                        if images:
                            return images
            elif 'media' in attachment_set and not is_sticker(attachment_set):
                simplet_set = [x['uri'] for x in jq.find_all(attachment_set, 'photo_image')]
                if simplet_set:
                    return simplet_set
        one_img = Story.fallback_get_image_link(post_json)
        if one_img:
            return [one_img]
        return []

    # facebook broke the original selector for all single-image posts around 10/12/2024
    @staticmethod
    def fallback_get_image_link(post_json: dict) -> str:
        for aa in jq.find_all(post_json, 'comet_photo_attachment_resolution_renderer'):
            return aa['image']['uri']
        return ''
