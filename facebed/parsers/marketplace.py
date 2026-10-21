import re

from curl_cffi.requests import Response as CffiResponse

from facebed import jq
from facebed.constants import WWWFB
from facebed.errors import NoDataException, ParseException, shape_errors
from facebed.models.post import ParsedPost
from facebed.parsers.blocks import get_json_blocks
from facebed.parsers.fetch import fetch_page


def get_listing_id(post_path: str) -> str:
    match = re.search(r'(?:^|/)marketplace/item/(\d+)', post_path, re.IGNORECASE)
    if not match:
        raise ParseException('cannot find marketplace listing id')
    return match.group(1)


@shape_errors()
def process_post(
    post_path: str,
    http_response: CffiResponse | None = None,
) -> ParsedPost:
    listing_id = get_listing_id(post_path)
    with fetch_page(post_path, http_response=http_response) as html_parser:
        # the listing is split over several targets (photos, details), merge the ones for this id
        listing: dict = {}
        for block in get_json_blocks(html_parser):
            for page in jq.find_all(block, 'marketplace_product_details_page'):
                target = page.get('target') if isinstance(page, dict) else None
                if isinstance(target, dict) and str(target.get('id')) == listing_id:
                    listing.update(target)
        if 'marketplace_listing_title' not in listing:
            raise NoDataException('Facebook response did not contain the requested listing')

        price = (listing.get('listing_price') or {}).get('formatted_amount_zeros_stripped') or ''
        if listing.get('is_sold'):
            price = f'Sold · {price}' if price else 'Sold'
        elif listing.get('is_pending'):
            price = f'Pending · {price}' if price else 'Pending'
        headline = ' · '.join(x for x in [price, listing['marketplace_listing_title']] if x)
        condition = next((a.get('label') for a in listing.get('attribute_data') or []
                          if a.get('attribute_name') == 'Condition'), '')
        place = (listing.get('location_text') or {}).get('text') or ''
        details = ' · '.join(x for x in [f'📍 {place}' if place else '', condition] if x)
        description = (listing.get('redacted_description') or {}).get('text') or ''
        text = '\n'.join(x for x in [headline, details] if x)
        if description:
            text += f'\n\n{description}'

        seller = listing.get('marketplace_listing_seller') or {}
        images = [(p.get('image') or {}).get('uri') for p in listing.get('listing_photos') or []]
        images = [u for u in images if u]
        created = listing.get('creation_time')

        parsed = ParsedPost(seller.get('name') or 'Marketplace', text.strip(), images,
                            f'{WWWFB}/marketplace/item/{listing_id}/',
                            int(created) if isinstance(created, int) else -1,
                            None, None, None, [])
        parsed.key = f'm{listing_id}'
        parsed.author = seller.get('name') or ''
        parsed.avatar = (seller.get('profile_picture') or {}).get('uri') or ''
        parsed.media = [('image', u) for u in images]
        return parsed
