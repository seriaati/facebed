import json
import logging
import os
import time

from facebed.notifier import warn


class Cookies:
    # facebook's login session; the rest are cosmetic and expire much sooner
    SESSION_COOKIES = ('c_user', 'xs')

    def __init__(self, fn: str):
        self.cookies: list = []

        if not os.path.isfile(fn):
            logging.warning('cookies.json not found, non incognito-viewable posts will NOT work')
            return

        with open(fn) as f:
            self.cookies = json.load(f)
            logging.info(f'loaded {len(self.cookies)} cookies from {fn}')

    # noinspection PyMethodMayBeStatic
    def is_valid_cookie(self, entry: dict) -> bool:
        return int(entry.get('expirationDate', 2**31)) > time.time()

    def expired(self) -> list[str]:
        return [k['name'] for k in self.cookies if not self.is_valid_cookie(k)]

    def get_cookies(self) -> dict[str, str]:
        expired = self.expired()
        if any(name in self.SESSION_COOKIES for name in expired):
            logging.warning(f'session cookies expired ({", ".join(expired)}), requests will be logged out')
            warn('@everyone cookies expired')
            return {}

        return {k['name']: k['value'] for k in self.cookies if k['name'] not in expired}


acc = Cookies('cookies.json')
