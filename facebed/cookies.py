import json
import logging
import os
import time

from facebed.notifier import warn

logger = logging.getLogger(__name__)


class Cookies:
    # facebook's login session; the rest are cosmetic and expire much sooner
    SESSION_COOKIES = ('c_user', 'xs')

    def __init__(self) -> None:
        self.cookies: list = []

    def load(self, fn: str) -> None:
        if not os.path.isfile(fn):
            logger.warning('%s not found, non incognito-viewable posts will NOT work', fn)
            return

        with open(fn) as f:
            self.cookies = json.load(f)
            logger.info('loaded %s cookies from %s', len(self.cookies), fn)

    # noinspection PyMethodMayBeStatic
    def is_valid_cookie(self, entry: dict) -> bool:
        return int(entry.get('expirationDate', 2**31)) > time.time()

    def expired(self) -> list[str]:
        return [k['name'] for k in self.cookies if not self.is_valid_cookie(k)]

    def get_cookies(self) -> dict[str, str]:
        expired = self.expired()
        if any(name in self.SESSION_COOKIES for name in expired):
            logger.warning('session cookies expired (%s), requests will be logged out', ', '.join(expired))
            warn('@everyone cookies expired')
            return {}

        return {k['name']: k['value'] for k in self.cookies if k['name'] not in expired}


acc = Cookies()  # loaded from config.cookies_file at startup
