import argparse
import logging
import sys

from facebed.app import app, log_to_logger
from facebed.config import Config, ConfigError, set_config
from facebed.cookies import acc
from facebed.log import set_log_level, setup_logging

logger = logging.getLogger('facebed.main')


def main():
    parser = argparse.ArgumentParser(description='Facebook embed server')
    parser.add_argument('-c', '--config', type=str, help='config yaml file path')
    args = parser.parse_args()
    setup_logging()

    try:
        config = Config.load(args.config) if args.config else Config()
    except ConfigError as e:
        logger.error(e)  # noqa: TRY400 -- a config mistake needs the message, not a traceback
        sys.exit(1)
    set_config(config)
    set_log_level(config.log_level)

    acc.load(config.cookies_file)
    # checked here rather than on load so warn sees the config
    if acc.get_cookies() and acc.expired():
        logger.info('ignoring expired non-session cookies: %s', ', '.join(acc.expired()))

    logger.info('listening on %s:%s', config.host, config.port)
    app.install(log_to_logger)
    app.run(host=config.host, port=config.port, quiet=True)


if __name__ == '__main__':
    main()
