# facebed
A Facebook embed provider for Discord and other messaging apps.

[![2IrN8Ux.png](https://iili.io/2IrN8Ux.png)](https://freeimage.host/)


## For users:
Replace `www.facebook.com` in your Facebook URL to `facebed.com`.

Public post and group comment permalinks are supported: copy the comment's
timestamp link, keeping `comment_id` and (for a reply) `reply_comment_id` in the
URL. The embed shows the comment author, body, available attachment, timestamp,
reactions and reply count. Facebook must expose the comment to logged-out
requests; unavailable comments use the existing Facebook fallback card.
<details>
<summary>Vencord settings</summary>

Using regex:
- Find: `https://(www.)?facebook.com/(.*)`
- Replace: `https://facebed.com/$2`

</details>

# For developers and maintainers

## Running

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```sh
uv sync
uv run python -m facebed -c config.yaml
```

Without `-c`, every option uses its default.

## Configuration

`config.yaml` is optional, and every key in it is optional:

```yaml
host: 0.0.0.0
port: 9812
threads: 8                  # requests served concurrently
timezone: 7                 # UTC offset in hours (5.5 works), or an IANA name like Asia/Bangkok to follow DST
banned_users: []            # Facebook user/page ids whose posts are not embedded
notifier_webhook: ''        # Discord webhook for login walls, dumps and cookie expiry
proxy: ''                   # logged-out requests only, e.g. http://user:pass@host:port
user_agent: ''              # browser UA that issued cookies.json, sent with logged-in requests
cookies_file: cookies.json  # exported browser cookies for posts that need a login
log_level: INFO
```

Unknown keys and wrong types are rejected at startup.

`cookies.json` is a cookie export (a list of objects with `name`, `value` and
`expirationDate`) from a logged-in browser. Requests try logged out first and
only use the cookies when Facebook withholds the post.

## Docker

```sh
docker build -t facebed .
docker run -p 9812:9812 -v ./config:/facebed/config:ro facebed
```

The image reads `/facebed/config/config.yaml`. Mount `cookies.json` at
`/facebed/cookies.json`, or point `cookies_file` into the config directory.

## Development

```sh
uv run ruff check .        # lint
uv run ruff check --fix .  # apply safe autofixes
```
