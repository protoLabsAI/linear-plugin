# linear-plugin

Full **Linear** integration for a [protoAgent](https://github.com/protoLabsAI/protoAgent) agent — tools **and** "agent" behaviour in one plugin: the agent reads/files/comments on Linear, posts **as itself** (OAuth actor=app), and responds to Linear **agent sessions**.

## What's inside
- **6 tools** (`client.py` / `tools.py`): `linear_list_teams`, `linear_list_issues`, `linear_search_issues`, `linear_get_issue`, `linear_create_issue` (deduped), `linear_add_comment`.
- **OAuth agent identity** (`identity.py`): comments + session activities post **as the agent**, not the API-key owner.
- **Session poller** (`poller.py`): the primary inbound path — polls Linear for agent sessions awaiting a reply and handles each. **No public URL required** (works behind a LAN/tailnet).
- **Webhook + OAuth routes** (`webhook.py`): an optional HMAC-verified fast-path, plus `/plugins/linear/oauth/start` to authorize the identity.
- **Bridge** (`bridge.py`): drives one agent turn on an inbound event and posts the reply back (with a thought-ack so sessions don't time out).

## Config
- `linear.api_key` (`LINEAR_API_KEY`) — gates the 6 tools.
- For the agent identity: `ava_client_id` / `ava_client_secret` / `ava_redirect_uri` (`LINEAR_AVA_CLIENT_ID` / `…_SECRET` / `…_REDIRECT_URI`); authorize once at `/plugins/linear/oauth/start`.
- `webhook_secret` (`LINEAR_WEBHOOK_SECRET`) — only for the optional webhook fast-path.

> Inbound webhooks require a public, auth-exempt ingress. Behind a LAN, use the **poller** instead — it needs no public URL.

## Install
```bash
python -m server plugin install https://github.com/protoLabsAI/linear-plugin
# then add `linear` to plugins.enabled, set credentials, restart
```

## Test
```bash
cd /path/to/protoAgent && uv run --frozen python -m pytest /path/to/linear-plugin/tests -q
```

Ported from the Linear surface of protoWorkstacean's Ava agent (tools + agent identity + agent-session handling).
