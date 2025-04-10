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

# Constants matching the Go implementation
NUM_CHANNELS = 1  # Mono audio
SAMPLE_RATE = 16000
BITS_PER_SAMPLE = 16  # 16 bits per sample

def create_wav_header(data_length):
    """Generate a WAV header for the given data length"""
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
    
    return header.getvalue()

def upload_file_to_gcs(bucket_name, file_name, file_path):
    """Upload a file to Google Cloud Storage"""
    try:
        # Get credentials from environment variable
        creds_env = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
        if not creds_env:
            raise ValueError("GOOGLE_APPLICATION_CREDENTIALS_JSON environment variable is not set")
        
        # Decode the base64 encoded credentials
        creds_json = base64.b64decode(creds_env)
        
        # Create a temporary file for the credentials
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as creds_file:
            creds_file.write(creds_json)
            creds_path = creds_file.name
        
        try:
            # Create credentials object and storage client
            credentials = service_account.Credentials.from_service_account_file(creds_path)
            client = storage.Client(credentials=credentials)
            
            # Upload file to bucket
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(file_name)
            blob.upload_from_filename(file_path)
            blob.content_type = "audio/wav"
            
            logging.info(f"File {file_name} uploaded to GCS bucket {bucket_name} successfully.")
            return True
        finally:
            # Clean up temporary credentials file
            os.remove(creds_path)
            
    except Exception as e:
        logging.error(f"Failed to upload to GCS: {e}")
        raise

@app.route('/audio', methods=['POST'])
def handle_post_audio():
    """Handle audio POST requests"""
    sample_rate = request.args.get('sample_rate')
    uid = request.args.get('uid')
    
    logging.info(f"Received request from uid: {uid}")
    logging.info(f"Requested sample rate: {sample_rate}")
    
    # Read audio data from request body
    audio_data = request.get_data()
    
    # Generate filename with current timestamp
    current_time = datetime.now()
    filename = f"{current_time.day:02d}_{current_time.month:02d}_{current_time.year:04d}_{current_time.hour:02d}_{current_time.minute:02d}_{current_time.second:02d}.wav"
    
    # Create temporary file
    temp_file_path = os.path.join(tempfile.gettempdir(), filename)
    
    # Generate WAV header
    header = create_wav_header(len(audio_data))
    
    # Write to temporary file
    with open(temp_file_path, 'wb') as temp_file:
        temp_file.write(header)
        temp_file.write(audio_data)
    
    # Get bucket name from environment variable
    bucket_name = os.environ.get("GCS_BUCKET_NAME")
    if not bucket_name:
        error_msg = "GCS_BUCKET_NAME environment variable is not set"
        logging.error(error_msg)
        return Response(error_msg, status=500)
    
    try:
        # Upload the file to Google Cloud Storage
        upload_file_to_gcs(bucket_name, filename, temp_file_path)
        return Response(f"Audio bytes received and uploaded as {filename}", status=200)
    except Exception as e:
        return Response(f"Failed to upload to Google Cloud Storage: {str(e)}", status=500)

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get('PORT', 8080))
    logging.info(f"Server starting on port {port}...")
    app.run(host='0.0.0.0', port=port) 