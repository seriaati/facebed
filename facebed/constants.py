from pathlib import Path

DEFAULT_FACEBOOK_USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36'
SHARE_HEAD_USER_AGENT = 'python-requests/2.32.3'
SHARE_BODY_USER_AGENT = 'Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)'
BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / 'assets'
WWWFB = 'https://www.facebook.com'
DISCORD_VIDEO_MAX_BYTES = 35_000_000  # larger hd videos fall back to sd
FACEBOOK_REACTION_EMOJIS = {
    '1635855486666999': '👍',
    '1678524932434102': '❤️',
    '613557422527858': '🤗',
    '115940658764963': '😂',
    '478547315650144': '😮',
    '908563459236466': '😢',
    '444813342392137': '😡',
}
FACEBOOK_REACTION_NAME_IDS = {
    'like': '1635855486666999',
    'love': '1678524932434102',
    'care': '613557422527858',
    'haha': '115940658764963',
    'wow': '478547315650144',
    'sad': '908563459236466',
    'angry': '444813342392137',
}
