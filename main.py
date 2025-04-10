import os
import base64
import logging
import tempfile
import struct
import time
from io import BytesIO
from datetime import datetime

from flask import Flask, request, Response
from google.cloud import storage
from google.oauth2 import service_account

app = Flask(__name__)

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Constants matching the Go implementation
NUM_CHANNELS = 1  # Mono audio
SAMPLE_RATE = 16000
BITS_PER_SAMPLE = 16  # 16 bits per sample

def create_wav_header(data_length):
    """Generate a WAV header for the given data length"""
    logger.info(f"Creating WAV header for data length: {data_length} bytes")
    byte_rate = SAMPLE_RATE * NUM_CHANNELS * BITS_PER_SAMPLE // 8
    block_align = NUM_CHANNELS * BITS_PER_SAMPLE // 8
    header = BytesIO()
    
    # RIFF header
    header.write(b"RIFF")
    header.write(struct.pack("<I", 36 + data_length))  # File size
    header.write(b"WAVE")
    
    # fmt chunk
    header.write(b"fmt ")
    header.write(struct.pack("<I", 16))  # Chunk size
    header.write(struct.pack("<H", 1))   # Format code (PCM)
    header.write(struct.pack("<H", NUM_CHANNELS))
    header.write(struct.pack("<I", SAMPLE_RATE))
    header.write(struct.pack("<I", byte_rate))
    header.write(struct.pack("<H", block_align))
    header.write(struct.pack("<H", BITS_PER_SAMPLE))
    
    # data chunk
    header.write(b"data")
    header.write(struct.pack("<I", data_length))
    
    logger.debug(f"WAV header created successfully. Header size: {len(header.getvalue())} bytes")
    return header.getvalue()

def upload_file_to_gcs(bucket_name, file_name, file_path):
    """Upload a file to Google Cloud Storage"""
    logger.info(f"Starting upload to GCS bucket: {bucket_name}, file: {file_name}")
    
    try:
        # Get credentials from environment variable
        logger.debug("Reading GCS credentials from environment variable")
        creds_env = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
        if not creds_env:
            logger.error("GOOGLE_APPLICATION_CREDENTIALS_JSON environment variable is not set")
            raise ValueError("GOOGLE_APPLICATION_CREDENTIALS_JSON environment variable is not set")
        
        # Decode the base64 encoded credentials
        logger.debug("Decoding base64 credentials")
        try:
            creds_json = base64.b64decode(creds_env)
            logger.debug("Credentials decoded successfully")
        except Exception as e:
            logger.error(f"Failed to decode credentials: {e}")
            raise
        
        # Create a temporary file for the credentials
        logger.debug("Creating temporary file for credentials")
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as creds_file:
            creds_file.write(creds_json)
            creds_path = creds_file.name
            logger.debug(f"Credentials written to temporary file: {creds_path}")
        
        try:
            # Create credentials object and storage client
            logger.debug("Creating storage client with credentials")
            credentials = service_account.Credentials.from_service_account_file(creds_path)
            client = storage.Client(credentials=credentials)
            
            # Upload file to bucket
            logger.info(f"Uploading file {file_path} to bucket {bucket_name}")
            file_size = os.path.getsize(file_path)
            logger.debug(f"File size: {file_size} bytes")
            
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(file_name)
            
            # Start upload with logging
            start_time = time.time()
            blob.upload_from_filename(file_path)
            end_time = time.time()
            
            blob.content_type = "audio/wav"
            upload_duration = end_time - start_time
            logger.info(f"File {file_name} uploaded to GCS bucket {bucket_name} successfully in {upload_duration:.2f} seconds")
            return True
        finally:
            # Clean up temporary credentials file
            logger.debug(f"Removing temporary credentials file: {creds_path}")
            os.remove(creds_path)
            
    except Exception as e:
        logger.error(f"Failed to upload to GCS: {e}", exc_info=True)
        raise

@app.route('/audio', methods=['POST'])
def handle_post_audio():
    """Handle audio POST requests"""
    start_time = time.time()
    logger.info("Received /audio POST request")
    
    sample_rate = request.args.get('sample_rate')
    uid = request.args.get('uid')
    
    logger.info(f"Request details - UID: {uid}, Sample rate: {sample_rate}")
    
    # Read audio data from request body
    audio_data = request.get_data()
    logger.info(f"Received {len(audio_data)} bytes of audio data")
    
    # Generate filename with current timestamp
    current_time = datetime.now()
    filename = f"{current_time.day:02d}_{current_time.month:02d}_{current_time.year:04d}_{current_time.hour:02d}_{current_time.minute:02d}_{current_time.second:02d}.wav"
    logger.info(f"Generated filename: {filename}")
    
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
        return Response(f"Failed to write temporary file: {str(e)}", status=500)
    
    # Get bucket name from environment variable
    bucket_name = os.environ.get("GCS_BUCKET_NAME")
    if not bucket_name:
        error_msg = "GCS_BUCKET_NAME environment variable is not set"
        logger.error(error_msg)
        return Response(error_msg, status=500)
    
    try:
        # Upload the file to Google Cloud Storage
        upload_file_to_gcs(bucket_name, filename, temp_file_path)
        end_time = time.time()
        total_processing_time = end_time - start_time
        logger.info(f"Request processed successfully in {total_processing_time:.2f} seconds")
        return Response(f"Audio bytes received and uploaded as {filename}", status=200)
    except Exception as e:
        logger.error(f"Failed to upload to Google Cloud Storage: {str(e)}", exc_info=True)
        return Response(f"Failed to upload to Google Cloud Storage: {str(e)}", status=500)
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
    logger.info("Health check request received")
    return Response("OK", status=200)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    logger.info(f"Starting server on port {port}...")
    logger.info(f"Environment details - Python version: {os.sys.version}")
    logger.info(f"Configured for: NUM_CHANNELS={NUM_CHANNELS}, SAMPLE_RATE={SAMPLE_RATE}, BITS_PER_SAMPLE={BITS_PER_SAMPLE}")
    
    # Check if required environment variables are set
    if os.environ.get("GCS_BUCKET_NAME"):
        logger.info(f"GCS_BUCKET_NAME is set to: {os.environ.get('GCS_BUCKET_NAME')}")
    else:
        logger.warning("GCS_BUCKET_NAME environment variable is not set!")
        
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON"):
        logger.info("GOOGLE_APPLICATION_CREDENTIALS_JSON is set")
    else:
        logger.warning("GOOGLE_APPLICATION_CREDENTIALS_JSON environment variable is not set!")
    
    app.run(host='0.0.0.0', port=port) 