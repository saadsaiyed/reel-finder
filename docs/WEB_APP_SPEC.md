# Reelmark Web App — Spec

Status: approved 2026-11-04
Brand: **Reelmark** (`reelmark.io`)

## 1. Purpose

A public site where people can learn what the bot does, create an account, buy
credits, and see the reels they have saved — plus a small admin surface for the
operator. The bot keeps running as it does today; the site adds identity,
billing, and a read-only view of stored data.

## 2. Non-goals for v1

- No mobile app.
- No team or shared accounts.
- No automatic refunds; refunds are a manual admin action in v1.
- No multi-language UI.
- No change to how reels are fetched or described.

## 3. Identity

### 3.1 Login

Two providers, same account:

- Email magic link (primary). No passwords.
- Google sign-in (OAuth), matched on verified email.

A user may attach both to the same account. `users.email` is unique.

### 3.2 Binding an Instagram account

Webhooks deliver an **Instagram-scoped ID (IGSID)**, which is not the same value
as an Instagram account ID and cannot be obtained any other way. So the site must
be told which IGSID belongs to which user, by proof of control:

1. Signed-in user opens "Connect Instagram".
2. Site issues a code, e.g. `LINK-7F3K` — 6 characters from an unambiguous
   alphabet, 10 minute expiry, one active code per user.
3. User DMs that code to `@reel_finder_nooh`.
4. Bot recognises `LINK-` in the message text and calls
   `POST /internal/link` on the site with `{igsid, code}` and the service token.
5. Site validates the code, writes `ig_links`, and the bot replies
   "Linked. You have N credits."

Rules:

- One IGSID maps to at most one user.
- One user may link at most one IGSID in v1.
- Re-linking replaces the previous link and is logged.
- Codes are single-use, rate-limited to 5 attempts per hour per user.

Instagram Login was rejected as a provider: it returns the professional account
ID, not the IGSID, and only works for professional accounts.

## 4. Credits

- One credit is consumed per reel successfully saved. Search and questions are
  free forever and unlimited — that is the product's selling point.
- **The bot is the source of truth** for balance and ledger. The site reads and
  grants through the internal API. This avoids a mirrored counter drifting.

### 4.1 Free allowance

- 10 reels on first contact from an unknown IGSID, no account needed.
- 10 more when an Instagram account is linked via `LINK-` code (total 20).
- Granting is idempotent per sender and reason, so a repeat webhook cannot
  double-grant.

### 4.2 Plans

| Plan | Price | Reels |
|---|---|---|
| Free (anonymous) | $0 | 10, one time |
| Free (linked) | $0 | +10, one time |
| Starter, first month | $3.99 | 20 this cycle |
| Starter | $9.99 / month | 40 / cycle |
| Pro | $19.99 / month | 100 / cycle |
| Top-up | $2.99 one-off | +10 |

- Cycles reset monthly. Unused reels roll over for one cycle, then expire.
- The intro price is shown on the button itself: "$3.99 first month, then
  $9.99/month", never only in fine print.
- Search and questions never consume credits.

### 4.3 Debit

Before doing any model work the bot checks balance > 0 and refuses early, so a
broke account cannot burn a vision call. The debit itself is atomic and happens
after a successful store:

```js
find_one_and_update({sender_id, credits: {$gte: 1}}, {$inc: {credits: -1}})
```

`ref` is the message id; a unique partial index makes a replay a no-op rather
than a second charge.

## 5. Stripe

- Stripe Checkout, hosted page. No card data touches our servers.
- Packs defined in config, default: 50 credits, 150 credits, 500 credits.
- Webhook endpoint `POST /stripe/webhook`, signature verified with
  `STRIPE_WEBHOOK_SECRET`.
- Events handled: `checkout.session.completed`, `charge.refunded` (manual review
  queue only in v1).

## 6. Data model (site database, Postgres)

- `users(id, email, email_verified_at, google_sub, name, created_at, is_admin)`
- `sessions(id, user_id, created_at, expires_at, ip, user_agent)`
- `login_tokens(token_hash, user_id, expires_at, used_at)`
- `link_codes(code, user_id, igsid, expires_at, used_at, attempts)`
- `ig_links(id, user_id, igsid, ig_username, linked_at, replaces_id)`
- `credit_ledger(id, user_id, delta, reason, ref, balance_after, created_at)`
  with `unique(reason='consume', ref)` enforced by a partial unique index
- `stripe_events(id, type, payload, processed_at)` — `id` is the Stripe event id
- `admin_actions(id, actor_user_id, action, target, detail, created_at)`

## 7. Site ↔ bot API

Internal only, guarded by `SERVICE_TOKEN` (separate from `ADMIN_TOKEN`), bound to
the private network, never exposed to browsers.

- `POST /internal/link` — `{igsid, code}` → `{ok, user_id, credits}`
- `GET  /internal/account/<igsid>` → `{credits, plan, status}`
- `POST /internal/credit` — grant from Stripe, `{igsid, units, ref, reason}`

Site → bot, read only:

- `GET /internal/reels/<igsid>?limit=50` — from the Qdrant collection for that
  sender: description, note, caption, link, saved_by, created_at
- `GET /internal/usage/<igsid>` — counts from `stats` and `accounts`

## 8. Security

Must ship before the site is public:

1. `/callback` gains a signed `state` cookie and refuses to persist a token whose
   returned `user_id` differs from `IG_ID`. Today any valid code can overwrite
   the bot token.
2. Webhook HMAC already verified; keep it mandatory in production.
3. Admin routes stay behind `ADMIN_TOKEN`; the site never proxies them.
4. Session cookies: `HttpOnly`, `Secure`, `SameSite=Lax`, signed, 30 day expiry.
5. CORS: site origin only; internal endpoints reject anything but the service
   token and the private network.
6. No endpoint accepts free text and forwards it as the bot.
7. Rate limits on login, link, and checkout endpoints.
8. Structured audit log for admin actions and credit grants.

## 9. Hosting

- Site: `reelfinder.saadsaiyed.com`, its own container, its own Postgres.
- Bot API: `reel.saadsaiyed.com` unchanged; webhook and callback only.
- TLS via the existing reverse proxy; HSTS on.

## 10. Phases

0. Bot security: `/callback` state + IG_ID check. Independent of the site.
1. Bot credits: balance check, atomic debit, zero-credit reply, internal
   endpoints, outbox.
2. Site: landing page, magic-link and Google auth, sessions.
3. Stripe: packs, checkout, webhook, ledger.
4. Dashboard: linked account, balance, saved reels, usage, buy credits.
5. Admin: grant credits, look up a user, see recent errors.
6. Deploy, seed, end-to-end test with a real purchase in test mode.

## 11. Open questions

- Brand name for the site header: "Reel Finder" or something else?
- Which credit packs and prices?
- Should a question or search ever cost a credit later? (Currently free.)
