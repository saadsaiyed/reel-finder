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


## Docker Docs
`docker build -t saadsaiyed7/reel-finder:latest .`
`docker run -p 5000:5000 saadsaiyed7/reel-finder:latest`
`docker run -p 5000:5000 --env-file .env saadsaiyed7/reel-finder:latest`
`docker tag reel-finder-app:latest saadsaiyed7/reel-finder:latest`
