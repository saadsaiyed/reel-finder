# reel-finder
Making it easy for you to find correct reel when you need

## Creating an Instagram Meta App

1. Go to the [Meta for Developers](https://developers.facebook.com/) website.
2. Log in with your Facebook account.
3. Click on "My Apps" and then "Create App".
4. Select the type of app you want to create and click "Next".
5. Fill in the required details and click "Create App ID".
6. In the app dashboard, go to "Settings" > "Basic" to find your App ID and App Secret.

## Fetching App ID and Secret

1. Navigate to your app's dashboard on the [Meta for Developers](https://developers.facebook.com/) website.
2. Go to "Settings" > "Basic".
3. Your App ID and App Secret will be displayed here. Copy these values for later use.

## Connecting an Instagram Account

1. In your app's dashboard, go to "Instagram" > "Basic Display".
2. Click "Create New App" and follow the instructions to set up Instagram Basic Display.
3. Add an Instagram Test User to your app.
4. Log in to Instagram with the test user account and authorize your app.

## Managing Instagram Inbox

1. Use the Instagram Graph API to fetch and manage messages.
2. Refer to the [Instagram Graph API documentation](https://developers.facebook.com/docs/instagram-api) for detailed instructions on how to use the API to manage your Instagram inbox.


## Docker

```bash
docker build -t saadsaiyed7/reel-finder:1.4.0 .
docker run -p 5000:5000 --env-file .env saadsaiyed7/reel-finder:1.4.0
```

## Vector store

A local Qdrant instance is the deployment default:

```bash
docker run -d --name qdrant --restart unless-stopped \
  -p 172.17.0.1:6333:6333 -p 172.17.0.1:6334:6334 \
  -v qdrant_storage:/qdrant/storage qdrant/qdrant:latest
```

Then set `QDRANT_URL=http://172.17.0.1:6333` and leave `QDRANT_API_KEY` empty.
Bound to the Docker bridge gateway, so containers can reach it and the internet
cannot. Data lives in the `qdrant_storage` volume.

## Operations notes

- Full Meta app configuration and the token strategy: [docs/META_APP_SETUP.md](docs/META_APP_SETUP.md).
- `GET /token-status` (admin) reports how many days the Instagram token has left.
  A background loop refreshes it automatically from day 30 of its 60-day life
  and logs `CRITICAL` once it has expired.
- `processed_mids.mid` is unique: each webhook `mid` is claimed once, so
  Instagram retries cannot reprocess a reel.
- Stored reel links expire. Search falls back to the saved description when
  the original media URL no longer resolves.
- `POST /webhook` rejects requests whose `X-Hub-Signature-256` does not match
  `INSTA_APP_SECRET`. `/refresh-token`, `/token-status` and `/conversations`
  require `ADMIN_TOKEN`; `/health` is the only unauthenticated status route.
