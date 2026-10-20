import json
import logging
import traceback
from html import escape

from bottle import request

from facebed import component_embed
from facebed.media_store import MediaStore
from facebed.models.post import ParsedPost
from facebed.utils import Utils, get_credit, quote


def format_error_message_embed(original_url: str) -> str:
    return Utils.prettify(f'''<!DOCTYPE html>
<html lang="">
<head>
<meta charset="UTF-8" />
    <meta name="theme-color" content="#2c3048f" />
    <meta property="og:title" content="Log in or sign up to view"/>
    <meta property="og:description" content="See posts, photos and more on Facebook."/>
    <meta http-equiv="refresh" content="0;url={escape(quote(original_url), quote=True)}"/>
</head>
</html>''')


def with_component_embed(html: str, post: ParsedPost) -> str:
    """Add the Discord component embed next to the OG tags; the OG tags stay as the fallback."""
    try:
        origin = '{}://{}'.format(*request.urlparts[:2])
        payload = component_embed.render(post, origin)
    except Exception:
        logging.warning(f'component embed failed for {post.url}:\n{traceback.format_exc()}')
        return html
    if not payload:
        return html
    MediaStore.register(post)
    tag = f'<script id="discord:component-embed" type="application/json">{payload}</script>'
    return html.replace('</head>', f'    {tag}\n    </head>', 1)


def format_reel_post_embed(post: ParsedPost) -> str:
    def get_video_meta_tag(link: str) -> str:
        escaped_link = escape(link, quote=True)
        return '\n'.join([
            f'<meta property="twitter:player:stream" content="{escaped_link}"/>',
            f'<meta property="og:video" content="{escaped_link}"/>',
            f'<meta property="og:video:secure_url" content="{escaped_link}"/>',
        ])

    video_meta_tags = '\n'.join([get_video_meta_tag(vu) for vu in post.video_links])
    reaction_str = Utils.format_reactions_str(
        post.likes, post.comments, post.shares, post.top_reaction_ids
    )
    post_date = Utils.timestamp_to_str(post.date)
    site_name = escape(f'{get_credit()}\n{post_date}\n{reaction_str}', quote=True)
    color = '#0866ff'

    return with_component_embed(Utils.prettify(f'''<!DOCTYPE html>
        <html lang="">
        <head>
            <title>{get_credit()}</title>
            <meta charset="UTF-8"/>
            <meta property="og:title" content="{escape(post.author_name)}"/>
            <meta property="og:description" content="{escape(post.text[:1024])}"/>
            <meta property="og:site_name" content="{site_name}"/>
            <meta property="og:url" content="{escape(quote(post.url), quote=True)}"/>
            <meta property="og:video:type" content="video/mp4"/>
            <meta property="twitter:player:stream:content_type" content="video/mp4"/>

            {video_meta_tags}

            <link rel="canonical" href="{escape(quote(post.url), quote=True)}"/>
            <meta http-equiv="refresh" content="0;url={escape(quote(post.url), quote=True)}"/>
            <meta name="twitter:card" content="player"/>
            <meta name="theme-color" content="{color}"/>
        </head>
        </html>'''), post)


def format_full_post_embed(post: ParsedPost) -> str:
    if post.video_links:
        return format_reel_post_embed(post)
    image_links = post.image_links
    image_counter = f'\ncontains 4+ images' if len(image_links) > 4 else ''
    image_links = image_links[:4]
    image_meta_tags = '\n'.join([
        f'<meta property="og:image" content="{escape(iu, quote=True)}"/>'
        for iu in image_links
    ])
    post_date = Utils.timestamp_to_str(post.date)
    reaction_str = Utils.format_reactions_str(
        post.likes, post.comments, post.shares, post.top_reaction_ids
    )
    site_name = escape(
        f'{get_credit()}\n{post_date}\n{reaction_str}{image_counter}', quote=True
    )

    # TODO: organize and duplicate the neccessary tags
    return with_component_embed(Utils.prettify(f'''<!DOCTYPE html>
        <html lang="">
        <head>
            <title>{get_credit()}</title>
            <meta charset="UTF-8"/>
            <meta property="og:title" content="{escape(post.author_name)}"/>
            <meta property="og:description" content="{escape(post.text[:1024])}"/>
            <meta property="og:site_name" content="{site_name}"/>
            <meta property="og:url" content="{escape(quote(post.url), quote=True)}"/>
            {image_meta_tags}
            <link rel="canonical" href="{escape(quote(post.url), quote=True)}"/>
            <meta http-equiv="refresh" content="0;url={escape(quote(post.url), quote=True)}"/>
            <meta name="twitter:card" content="summary_large_image"/>
            <meta name="theme-color" content="#0866ff"/>
        </head>
        </html>'''), post)


def format_redirect_page(url: str) -> str:
    script_url = json.dumps(url).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    return Utils.prettify(f'''<!DOCTYPE HTML>
<html lang="en-US">
    <head>
        <meta charset="UTF-8">
        <meta http-equiv="refresh" content="0; url={escape(quote(url), quote=True)}">
        <script type="text/javascript">
            window.location.href = {script_url}
        </script>
        <title>redirecting...</title>
    </head>
    <body>
    </body>
</html>''')
