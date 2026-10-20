import json

from bs4 import BeautifulSoup

from facebed import jq


def get_json_blocks(html_parser: BeautifulSoup) -> list[dict]:
    script_elements = html_parser.find_all('script', attrs={'type': 'application/json'})

    data_blocks = []
    for e in script_elements:
        try:
            if e.text:
                data = json.loads(e.text)
                data_blocks.append(data)
        except json.JSONDecodeError:
            continue
    return data_blocks


def get_initial_routes(blocks: list[dict]) -> list[dict]:
    routes = []
    for block in blocks:
        route_info = jq.find_first(block, 'initialRouteInfo')
        route = route_info.get('route') if isinstance(route_info, dict) else None
        if isinstance(route, dict) and route not in routes:
            routes.append(route)
    return routes


def get_declared_urls(
    html_parser: BeautifulSoup,
    *,
    property_name: str | None = None,
) -> list[str]:
    urls: list[str] = []
    if property_name in (None, 'canonical'):
        for link in html_parser.find_all('link'):
            rel = link.get('rel') or []
            if 'canonical' in rel and link.get('href'):
                urls.append(str(link['href']))
    if property_name in (None, 'og:url'):
        for meta in html_parser.find_all('meta', attrs={'property': 'og:url'}):
            if meta.get('content'):
                urls.append(str(meta['content']))
    return list(dict.fromkeys(urls))
