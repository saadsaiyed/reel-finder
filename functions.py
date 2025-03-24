import asyncio
import os
import time
import requests
from google import genai
from concurrent.futures import ThreadPoolExecutor

async def gemini(url):
    if url == "":
        return ""

    print(f"Downloading file from: {url}")
    filename = "temp_video.mp4"

    # Download the video
    response = requests.get(url)
    if response.status_code == 200:
        with open(filename, 'wb') as f:
            f.write(response.content)
        print(f"File downloaded: {filename}")
    else:
        print(f"Failed to download file: {response.status_code}")
        return ""

    # Create a new event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

    print("Uploading file...")
    video_file = client.files.upload(file=filename)
    print(f"Completed upload: {video_file.uri}")

    # Wait until the file is processed
    while video_file.state.name == "PROCESSING":
        print('.', end='', flush=True)
        await asyncio.sleep(1)
        video_file = client.files.get(name=video_file.name)

    if video_file.state.name == "FAILED":
        raise ValueError(video_file.state.name)

    print('Done')

    # Generate content from the file
    response = client.models.generate_content(
        model="gemini-1.5-pro",
        contents=[
            video_file,
            "with simple texts only, for each scene in this video, generate captions that describe the scene along with any spoken text placed in quotation marks wihtout timestamp. Provide your explanation. Only respond with what is asked. \nExample: A guy tasting something spicy and can't control his emotions and tears up."
        ]
    )

    try:
        os.remove(filename)
        print(f"File deleted: {filename}")
    except Exception as e:
        print(f"Failed to delete file: {e}")

    return response.text