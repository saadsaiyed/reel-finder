# Meta app setup and token strategy

The old Meta app is out of use. This is the configuration for the new app, and
the reasoning behind the code that keeps the access token alive.

## 1. Create the app

1. <https://developers.facebook.com/apps> → **Create app**.
2. Use case: **Instagram** → "Manage everything on your Instagram account".
   App type: **Business**.
3. **App settings → Basic** gives you the **App ID** and **App Secret**.
   The secret goes into `INSTA_APP_SECRET`. It is a credential: never commit it,
   never paste it into a chat, and rotate it (App settings → App secret → Reset)
   if it has ever been exposed.

## 2. Add the Instagram product

1. App dashboard → **Instagram** → **API setup with Instagram login**.
2. Add the Instagram account the bot runs as (`@reel_sync_ai`) and accept the
   tester invite from that account.
3. Scopes for this bot:
   - `instagram_business_basic` — read the account and media.
   - `instagram_business_manage_messages` — receive and send DMs, reactions.
   - `instagram_business_manage_comments` only if you add comment handling.
4. **Business login settings → OAuth redirect URI**: `https://<your-host>/callback`.
   That exact string must also be in `INSTA_REDIRECT_URI`.

`LOGIN_URL` is the authorize URL:

```
https://www.instagram.com/oauth/authorize
  ?client_id=<APP_ID>
  &redirect_uri=https://<your-host>/callback
  &response_type=code
  &scope=instagram_business_basic,instagram_business_manage_messages
```

Opening `/` shows that link; after login Meta redirects to `/callback`, which
exchanges the code for a short-lived token and immediately for a 60-day token.

## 3. Webhook

1. App dashboard → **Webhooks** → **Instagram** → Edit subscription.
2. Callback URL: `https://<your-host>/webhook`.
3. Verify token: a random string, also set as `WEBHOOK_VERIFY_TOKEN`.
   Generate one with `openssl rand -hex 32`.
4. Subscribe to the **messages** field (add `message_reactions` only if you
   want reaction events).

Meta performs a `GET /webhook?hub.verify_token=...&hub.challenge=...`
immediately. Every later `POST` carries `X-Hub-Signature-256`, an HMAC-SHA256 of
the raw body keyed by the app secret. The app now rejects unsigned or
mismatched payloads with 403, so nobody can forge a message event even if they
find the URL.

The host must serve HTTPS with a valid certificate; Meta will not verify a
plain HTTP or self-signed callback.

## 4. App mode

- **Development** with the Instagram account added as a tester is enough for a
  single-account bot. No App Review, no Business Verification.
- **Live** is only needed once other people use it. That requires App Review
  for each permission, Business Verification, and a data-deletion callback URL.
- Keep the app in Development unless you actually need other users. Fewer moving
  parts, no review queue, same behaviour for your own account.

## 5. Facebook Login for Business / system-user token

If the app was created as **Instagram API with Facebook Login** (Facebook Login
for Business configuration, system-user access token), three things differ:

- **Base URL:** `graph.facebook.com`, not `graph.instagram.com`. Set
  `GRAPH_BASE=https://graph.facebook.com`.
- **Messaging token:** a **Page access token**, not the system-user token. The
  Instagram professional account must be linked to a Facebook Page, and the
  system user must have both the Page and the Instagram account assigned in
  Business Manager.
- **Webhooks:** the app must be Live for Meta to deliver events, and Business
  Verification is required for this API variant.

System-user tokens never expire, which is the appeal — but the token that
actually sends DMs is the Page token derived from it:

```bash
# 1. find the Page linked to the Instagram account
curl -s "https://graph.facebook.com/v22.0/me/accounts?access_token=<SUAT>"

# 2. exchange it for the Page access token
curl -s "https://graph.facebook.com/v22.0/<PAGE_ID>?fields=access_token&access_token=<SUAT>"
```

Set the result as `INSTA_MESSAGING_TOKEN`, the Page id as `INSTA_PAGE_ID`, and
the system-user token as `INSTA_ACCESS_TOKEN` (used only for bootstrap). A Page
token derived from a system user does not expire, so the refresh loop detects it
and stops trying to refresh — there is nothing to refresh.

`/callback` and `LOGIN_URL` are unused in this mode: there is no per-user OAuth
handshake, the system user already has the asset.

## 6. How the token stops being a problem

Facts that drive the design:

- The short-lived token from the login flow lasts **1 hour**.
- Exchanging it yields a long-lived token that lasts **60 days**.
- A long-lived token can be refreshed with
  `GET /refresh_access_token?grant_type=ig_refresh_token&access_token=...`
  **only while it is still valid and at least 24 hours old**. Each refresh
  returns a fresh 60-day token.
- Once the token is expired, refresh fails permanently. The only recovery is
  repeating the login flow — which is exactly what happened on 2026-09-07.

What the app does now:

- **Boot**: if the `creds` collection is empty and `INSTA_ACCESS_TOKEN` is set,
  the token is validated against `/me?fields=id,username` and persisted with a
  60-day expiry. The database becomes the source of truth; the env var is only
  a bootstrap.
- **Every 6 hours**: if the token has **30 days or fewer** left, it is refreshed
  and the expiry moves back out to 60 days. This leaves about 30 daily attempts
  before expiry, so a short outage or one failed refresh cannot kill the bot.
- **On failure**: the error is logged and posted to `ALERT_WEBHOOK_URL` if set.
- **On expiry**: `CRITICAL` log plus an alert.
- **On demand**: `POST /refresh-token` and `GET /token-status` (both admin-only).

Net effect: as long as the container runs at least once every ~30 days, the
token never expires. If it does run dry, the alert arrives weeks before the
first failed DM rather than after.

## 7. Environment variables

Required:

- `DB_CONNECTION_STRING` — MongoDB connection string.
- `QDRANT_URL`, `QDRANT_API_KEY` — vector store.
- `GEMINI_API_KEY` — reel description generation.
- `INSTA_CLIENT_ID`, `INSTA_CLIENT_SECRET` — from App settings → Basic.
- `INSTA_REDIRECT_URI` — must match the Business login redirect URI exactly.
- `IG_ID` — the bot account's Instagram user id; used to ignore its own messages.
- `WEBHOOK_VERIFY_TOKEN` — random string, also entered in the webhook config.
- `LOGIN_URL` — the authorize URL above (Instagram Login only).

Facebook Login deployments add:

- `GRAPH_BASE=https://graph.facebook.com` (default is `graph.instagram.com`).
- `INSTA_MESSAGING_TOKEN` — the Page access token used to send DMs.
- `INSTA_PAGE_ID` — the Facebook Page linked to the Instagram account.

Optional: `GRAPH_VERSION` (default `v22.0`).

Security:

- `INSTA_APP_SECRET` — enables webhook signature verification and
  `appsecret_proof` on every Graph call. Without it the webhook accepts
  unsigned POSTs and logs a warning.
- `ADMIN_TOKEN` — required for `/refresh-token`, `/token-status` and
  `/conversations`. Without it those routes return 503 instead of running open.
- `ALERT_WEBHOOK_URL` — optional; any endpoint that accepts a plain-text POST
  (an ntfy topic URL works).
- `INSTA_ACCESS_TOKEN` — optional bootstrap only. Once `creds` has a row, the
  database token wins.

Optional: `GEMINI_PROMPT`, `DEFAULT_REACTION_TYPE`, `FLASK_SECRET_KEY`.

## 8. Operational checklist

- [ ] `INSTA_APP_SECRET` and `ADMIN_TOKEN` set in the deployment.
- [ ] `GET /health` returns `{"ok": true, "version": "1.3.1"}`.
- [ ] `GET /webhook?hub.verify_token=...&hub.challenge=123` echoes `123`.
- [ ] A `messages` event reaches the app; the log shows one claim per `mid`.
- [ ] `GET /token-status` (with `X-Admin-Token`) reports `valid` and ~60 days.
- [ ] `ALERT_WEBHOOK_URL` delivers a test message.
- [ ] The app secret is not in git: `git log -p | grep -i app_secret` is empty.
- [ ] Cloudflare/Nginx TLS is valid; `http://` redirects to `https://`.
