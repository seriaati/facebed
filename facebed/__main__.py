import argparse
import logging
import sys

from facebed.app import app, log_to_logger
from facebed.config import Config, ConfigError, set_config
from facebed.cookies import acc


def main():
    parser = argparse.ArgumentParser(description='Facebook embed server')
    parser.add_argument('-c', '--config', type=str, help='config yaml file path')
    args = parser.parse_args()

    try:
        config = Config.load(args.config) if args.config else Config()
    except ConfigError as e:
        logging.error(e)
        sys.exit(1)
    set_config(config)
    logging.getLogger().setLevel(config.log_level.upper())

    acc.load(config.cookies_file)
    # checked here rather than on load so warn sees the config
    if acc.get_cookies() and acc.expired():
        logging.info(f'ignoring expired non-session cookies: {", ".join(acc.expired())}')

    logging.info(f'listening on {config.host}:{config.port}')
    app.install(log_to_logger)
    app.run(host=config.host, port=config.port, quiet=True)


if __name__ == '__main__':
    main()
