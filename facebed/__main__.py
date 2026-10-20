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
        exit(1)
    set_config(config)

    if sys.version_info.minor < 12:
        logging.error('python 3.12+ required, see https://docs.python.org/3.12/whatsnew/3.12.html#pep-701-syntactic-formalization-of-f-strings')
        exit(1)

    # checked here rather than on load so Utils.warn sees the config
    if acc.get_cookies() and acc.expired():
        logging.info(f'ignoring expired non-session cookies: {", ".join(acc.expired())}')

    logging.info(f'listening on {config.host}:{config.port}')
    app.install(log_to_logger)
    app.run(host=config.host, port=config.port, quiet=True)


if __name__ == '__main__':
    main()
