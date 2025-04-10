import os
import base64
import logging
import tempfile
import struct
import time
import json # Added for Pub/Sub message
from io import BytesIO
from datetime import datetime, timezone # Added timezone

from flask import Flask, request, Response
from google.cloud import storage
from google.cloud import pubsub_v1 # Added Pub/Sub client
from google.oauth2 import service_account

app = Flask(__name__)

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- Configuration & Constants ---
# Constants matching the Go implementation
NUM_CHANNELS = 1  # Mono audio
SAMPLE_RATE = 16000
BITS_PER_SAMPLE = 16  # 16 bits per sample

# Environment Variables (ensure these are set in Cloud Run)
GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME")
GOOGLE_APPLICATION_CREDENTIALS_JSON = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON") # For GCS upload as implemented
PUB_SUB_TOPIC_ID = os.environ.get("PUB_SUB_TOPIC_ID") # ADD THIS ENV VAR
PROJECT_ID = os.environ.get("GCP_PROJECT") # Needed for Pub/Sub Topic Path

# --- Initialize Clients ---
# Storage Client (using explicit credentials as provided in original code)
# Note: Standard practice in Cloud Run is usually to rely on the attached service account (ADC)
#       instead of explicit key files, but we retain the original method here for GCS.
storage_client = None
if GOOGLE_APPLICATION_CREDENTIALS_JSON:
    try:
        creds_json_bytes = base64.b64decode(GOOGLE_APPLICATION_CREDENTIALS_JSON)
        # Use temporary file for credentials - consider security implications
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as creds_file:
            creds_file.write(creds_json_bytes)
            creds_path = creds_file.name
        credentials = service_account.Credentials.from_service_account_file(creds_path)
        storage_client = storage.Client(credentials=credentials, project=PROJECT_ID) # Specify project for client
        os.remove(creds_path) # Clean up temp file immediately after client creation
        logger.info("Storage Client initialized using provided credentials JSON.")
    except Exception as e:
        logger.error(f"Failed to initialize Storage Client from JSON credentials: {e}", exc_info=True)
        # Decide if the app should fail to start or continue without storage client
        storage_client = None # Ensure it's None if init fails
else:
    logger.warning("GOOGLE_APPLICATION_CREDENTIALS_JSON not set. Storage Client not initialized with explicit credentials.")
    # Attempt to initialize using ADC (will use Cloud Run service account)
    try:
        storage_client = storage.Client(project=PROJECT_ID)
        logger.info("Storage Client initialized using Application Default Credentials.")
    except Exception as e:
        logger.error(f"Failed to initialize Storage Client using Application Default Credentials: {e}", exc_info=True)
        storage_client = None

# Pub/Sub Publisher Client (using standard ADC via Cloud Run service account)
publisher = None
topic_path = None
if PROJECT_ID and PUB_SUB_TOPIC_ID:
    try:
        publisher = pubsub_v1.PublisherClient()
        topic_path = publisher.topic_path(PROJECT_ID, PUB_SUB_TOPIC_ID)
        logger.info(f"Pub/Sub Publisher Client initialized for topic: {topic_path}")
    except Exception as e:
        logger.error(f"Failed to initialize Pub/Sub Publisher Client: {e}", exc_info=True)
else:
    logger.warning("GCP_PROJECT or PUBSUB_TOPIC_ID not set. Pub/Sub Publisher not initialized.")


def create_wav_header(data_length):
    """Generate a WAV header for the given data length"""
    logger.debug(f"Creating WAV header for data length: {data_length} bytes") # Changed to debug
    byte_rate = SAMPLE_RATE * NUM_CHANNELS * BITS_PER_SAMPLE // 8
    block_align = NUM_CHANNELS * BITS_PER_SAMPLE // 8
    header = BytesIO()
    header.write(b"RIFF")
    header.write(struct.pack("<I", 36 + data_length))
    header.write(b"WAVE")
    header.write(b"fmt ")
    header.write(struct.pack("<I", 16))
    header.write(struct.pack("<H", 1))
    header.write(struct.pack("<H", NUM_CHANNELS))
    header.write(struct.pack("<I", SAMPLE_RATE))
    header.write(struct.pack("<I", byte_rate))
    header.write(struct.pack("<H", block_align))
    header.write(struct.pack("<H", BITS_PER_SAMPLE))
    header.write(b"data")
    header.write(struct.pack("<I", data_length))
    logger.debug(f"WAV header created. Header size: {len(header.getvalue())} bytes") # Changed to debug
    return header.getvalue()

def upload_file_to_gcs_internal(bucket_name, file_name, file_path):
    """Internal GCS upload function using the initialized client"""
    logger.info(f"Starting upload to GCS bucket: {bucket_name}, file: {file_name}")
    if not storage_client:
        logger.error("Storage client is not initialized. Cannot upload.")
        raise ConnectionError("Storage client not initialized") # More specific error

    try:
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(file_name) # Define GCS path within bucket

        logger.info(f"Uploading file {file_path} to gs://{bucket_name}/{file_name}")
        file_size = os.path.getsize(file_path)
        logger.debug(f"File size: {file_size} bytes")

        start_time = time.time()
        blob.upload_from_filename(file_path)
        # Setting content type after upload is generally okay
        blob.content_type = "audio/wav"
        blob.patch() # Make sure content type is saved
        end_time = time.time()

        upload_duration = end_time - start_time
        logger.info(f"File {file_name} uploaded to GCS successfully in {upload_duration:.2f} seconds")
        return f"gs://{bucket_name}/{file_name}" # Return the GCS URI
    except Exception as e:
        logger.error(f"Failed to upload {file_name} to GCS: {e}", exc_info=True)
        raise # Re-raise the exception


@app.route('/audio', methods=['POST'])
def handle_post_audio():
    """Handle audio POST requests"""
    start_request_time = time.time()
    logger.info("Received /audio POST request")

    if not GCS_BUCKET_NAME:
        error_msg = "GCS_BUCKET_NAME environment variable is not set"
        logger.error(error_msg)
        return Response(error_msg, status=500)
    if not publisher or not topic_path:
        error_msg = "Pub/Sub publisher not initialized (check GCP_PROJECT, PUBSUB_TOPIC_ID env vars)"
        logger.error(error_msg)
        return Response(error_msg, status=500)

    # Read audio data from request body
    audio_data = request.get_data()
    if not audio_data:
         logger.warning("Received empty audio data in request.")
         return Response("No audio data received.", status=400)
    logger.info(f"Received {len(audio_data)} bytes of audio data")

    # Generate filename with current timestamp (add microseconds for better uniqueness)
    # Using UTC is recommended for consistency
    current_time_utc = datetime.now(timezone.utc)
    # Format: YYYY-MM-DD_HH-MM-SS-ffffff.wav (ISO-like, sortable, unique)
    filename = f"{current_time_utc.strftime('%Y-%m-%d_%H-%M-%S-%f')}.wav"
    gcs_blob_name = f"raw_audio/{filename}" # Assume a prefix for raw files
    logger.info(f"Generated GCS blob name: {gcs_blob_name}")

    # Create temporary file
    temp_file_path = os.path.join(tempfile.gettempdir(), filename)
    logger.debug(f"Temporary file path: {temp_file_path}")

    # Generate WAV header
    header = create_wav_header(len(audio_data))

    # Write to temporary file
    logger.debug(f"Writing WAV header and audio data to temporary file")
    try:
        with open(temp_file_path, 'wb') as temp_file:
            temp_file.write(header)
            temp_file.write(audio_data)
        logger.debug(f"Successfully wrote data to temporary file: {temp_file_path}")
    except Exception as e:
        logger.error(f"Failed to write to temporary file: {e}", exc_info=True)
        # Clean up if file exists before returning error
        if os.path.exists(temp_file_path): os.remove(temp_file_path)
        return Response(f"Failed to write temporary file: {str(e)}", status=500)

    gcs_uri = None
    try:
        # Upload the file to Google Cloud Storage
        gcs_uri = upload_file_to_gcs_internal(GCS_BUCKET_NAME, gcs_blob_name, temp_file_path)

        # ---- ADDED: Publish to Pub/Sub ----
        message_data = {
            "gcsUri": gcs_uri,
            "filename": filename, # Keep original generated filename maybe? Or blob name?
            "timestamp": current_time_utc.isoformat() # Use precise ISO 8601 timestamp
        }
        message_json = json.dumps(message_data)
        message_bytes = message_json.encode('utf-8')

        try:
            publish_future = publisher.publish(topic_path, data=message_bytes)
            # Let publish happen asynchronously for lower latency, but log errors
            publish_future.add_done_callback(
                lambda future: logger.info(f"Pub/Sub message for {filename} published successfully.")
                if not future.exception() else
                logger.error(f"Failed to publish Pub/Sub message for {filename}: {future.exception()}")
            )
            # For critical paths you might use publish_future.result(timeout=...)
        except Exception as e:
            logger.error(f"Error initiating publish to Pub/Sub topic {topic_path}: {e}", exc_info=True)
            # Decide how to handle: GCS upload succeeded but Pub/Sub failed.
            # Maybe log prominently, or attempt retry? For now, log and continue.
            # The request still succeeded in saving the file.

        # ------------------------------------

        end_request_time = time.time()
        total_processing_time = end_request_time - start_request_time
        logger.info(f"Request processed successfully (GCS + Pub/Sub triggered) in {total_processing_time:.2f} seconds")
        return Response(f"Audio bytes received, uploaded as {gcs_blob_name}, notification sent.", status=200)

    except Exception as e:
        # This catches GCS upload errors mostly
        logger.error(f"Failed during GCS upload: {str(e)}", exc_info=True)
        return Response(f"Failed during GCS upload: {str(e)}", status=500)
    finally:
        # Clean up the temporary file
        if os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
                logger.debug(f"Temporary file {temp_file_path} removed")
            except Exception as e:
                logger.warning(f"Failed to remove temporary file {temp_file_path}: {e}")


@app.route('/health', methods=['GET'])
def health_check():
    """Simple health check endpoint"""
    # Could add checks for GCS/PubSub client initialization here
    logger.debug("Health check request received") # Changed to debug
    return Response("OK", status=200)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    logger.info(f"Starting server on port {port}...")
    # Log critical env vars on startup
    logger.info(f"GCS_BUCKET_NAME: {GCS_BUCKET_NAME or 'Not Set!'}")
    logger.info(f"GCP_PROJECT: {PROJECT_ID or 'Not Set!'}")
    logger.info(f"PUBSUB_TOPIC_ID: {PUB_SUB_TOPIC_ID or 'Not Set!'}")
    logger.info(f"GOOGLE_APPLICATION_CREDENTIALS_JSON: {'Set' if GOOGLE_APPLICATION_CREDENTIALS_JSON else 'Not Set'}")

    # Basic check for client initialization
    if not storage_client:
         logger.error("Storage client failed to initialize on startup!")
    if not publisher:
         logger.error("Pub/Sub publisher failed to initialize on startup!")

    app.run(host='0.0.0.0', port=port)