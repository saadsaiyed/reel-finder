import os, logging, time, asyncio, requests
from google import genai

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def detect_file_type(response):
    """Detect file type from response headers or magic bytes."""
    # Check Content-Type header first
    content_type = response.headers.get('content-type', '').lower()
    
    if 'video' in content_type:
        return 'video', 'mp4'
    elif 'image' in content_type:
        return 'image', 'jpg'
    
    # Fall back to magic bytes detection
    content = response.content[:12]
    
    # Video magic bytes
    if content.startswith(b'\x00\x00\x00\x18ftypisom'):  # MP4
        return 'video', 'mp4'
    elif content.startswith(b'\x00\x00\x00\x20ftyp'):  # MP4 variant
        return 'video', 'mp4'
    elif content.startswith(b'ftypmp42'):  # MP4 variant
        return 'video', 'mp4'
    
    # Image magic bytes
    elif content.startswith(b'\xFF\xD8\xFF'):  # JPEG
        return 'image', 'jpg'
    elif content.startswith(b'\x89PNG\r\n\x1a\n'):  # PNG
        return 'image', 'png'
    elif content.startswith(b'GIF87a') or content.startswith(b'GIF89a'):  # GIF
        return 'image', 'gif'
    elif content.startswith(b'WEBP'):  # WebP
        return 'image', 'webp'
    
    # Default to mp4 if unknown
    logger.warning(f"Could not determine file type from Content-Type: {content_type}, magic bytes: {content.hex()[:20]}... - defaulting to mp4")
    return 'video', 'mp4'

async def gemini(url, is_reel=True):
    if url == "":
        return ""

    logger.debug(f"Downloading file from: {url}")

    # Download the file
    response = requests.get(url)
    if response.status_code != 200:
        logger.error(f"Failed to download file: {response.status_code}")
        return ""
    
    # Detect file type
    file_type, extension = detect_file_type(response)
    filename = f"temp_video.{extension}" if file_type == 'video' else f"temp_image.{extension}"
    
    logger.info(f"Detected {file_type} file ({extension}): {filename}")
    
    # Save the file
    try:
        with open(filename, 'wb') as f:
            f.write(response.content)
        logger.debug(f"File downloaded and saved: {filename} ({len(response.content)} bytes)")
    except Exception as e:
        logger.error(f"Failed to save file: {e}")
        return ""

    # Create a new event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

    logger.debug("Uploading file to Gemini...")
    try:
        video_file = client.files.upload(file=filename)
        logger.debug(f"Completed upload: {video_file.uri}")
    except Exception as e:
        logger.error(f"Failed to upload file: {e}")
        try:
            os.remove(filename)
        except:
            pass
        return ""

    # Wait until the file is processed
    while video_file.state.name == "PROCESSING":
        print('.', end='', flush=True)
        await asyncio.sleep(1)
        video_file = client.files.get(name=video_file.name)

    if video_file.state.name == "FAILED":
        logger.error(f"File processing failed: {video_file.state.name}")
        try:
            os.remove(filename)
        except:
            pass
        raise ValueError(video_file.state.name)

    logger.debug('File processed successfully')

    # Generate content from the file
    prompt = os.environ.get("GEMINI_PROMPT", "With simple texts only and no `here you go...` or `following is:...` types of statements, for each scene in this video, generate captions that describe the scene along with any spoken text placed in quotation marks without timestamp. Provide your explanation. Only respond with what is asked. \nExample: A guy tasting something spicy and can't control his emotions and tears up.")
    
    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=[
                video_file,
                prompt
            ]
        )
    except Exception as e:
        logger.error(f"Failed to generate content: {e}")
        try:
            os.remove(filename)
        except:
            pass
        return ""

    # Clean up temp file
    try:
        os.remove(filename)
        logger.debug(f"Temp file deleted: {filename}")
    except Exception as e:
        logger.warning(f"Failed to delete temp file: {e}")

    return response.text