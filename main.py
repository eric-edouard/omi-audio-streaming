from flask import Flask, request, Response
import os
import base64
import logging
import tempfile
import struct
import time
import numpy as np
import wave
from datetime import datetime
from opuslib import Decoder

app = Flask(__name__)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants
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

@app.route('/audio', methods=['POST'])
def handle_post_audio():
    """Handle audio POST requests"""
    start_time = time.time()
    logger.info("Received /audio POST request")
    
    sample_rate = request.args.get('sample_rate', type=int, default=SAMPLE_RATE)
    uid = request.args.get('uid')
    codec = request.args.get('codec', default='pcm').lower()
    
    logger.info(f"Request details - UID: {uid}, Sample rate: {sample_rate}, Codec: {codec}")
    
    # Read audio data from request body
    audio_data = request.get_data()
    logger.info(f"Received {len(audio_data)} bytes of audio data")
    
    # Generate filename with current timestamp
    current_time = datetime.now()
    filename = f"{current_time.strftime('%d_%m_%Y_%H_%M_%S')}.wav"
    logger.info(f"Generated filename: {filename}")
    
    # Create temporary file path
    temp_file_path = os.path.join(tempfile.gettempdir(), filename)
    logger.debug(f"Temporary file path: {temp_file_path}")
    
    try:
        if codec == 'opus':
            logger.info("Processing Opus encoded audio data")
            # Initialize Opus decoder
            decoder = Decoder(sample_rate, NUM_CHANNELS)
            
            # Decode Opus data to PCM
            frame_size = 960  # Number of samples per frame; adjust as needed
            pcm_data = decoder.decode(audio_data, frame_size)
            logger.debug(f"Opus data decoded successfully, obtained {len(pcm_data)} bytes of PCM data")
            
            # Convert PCM data to numpy array
            pcm_array = np.frombuffer(pcm_data, dtype=np.int16)
            
            # Write PCM data to WAV file
            with wave.open(temp_file_path, 'wb') as wav_file:
                wav_file.setnchannels(NUM_CHANNELS)
                wav_file.setsampwidth(BITS_PER_SAMPLE // 8)  # Convert bits to bytes
                wav_file.setframerate(sample_rate)
                wav_file.writeframes(pcm_array.tobytes())
            logger.info(f"Decoded Opus data written to WAV file: {temp_file_path}")
        else:
            logger.info("Processing raw PCM audio data")
            # Generate WAV header for raw PCM data
            header = create_wav_header(len(audio_data))
            
            # Write WAV header and raw PCM data to temporary file
            with open(temp_file_path, 'wb') as temp_file:
                temp_file.write(header)
                temp_file.write(audio_data)
            logger.info(f"Raw PCM data written to WAV file: {temp_file_path}")
        
        # Proceed with uploading the file to Google Cloud Storage or further processing
        # ...
        
        return Response(f"Audio data processed and saved as {filename}", status=200)
    except Exception as e:
        logger.error(f"Error processing audio data: {e}", exc_info=True)
        return Response(f"Failed to process audio data: {str(e)}", status=500)
    finally:
        # Clean up the temporary file
        if os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
                logger.debug(f"Temporary file {temp_file_path} removed")
            except Exception as e:
                logger.warning(f"Failed to remove temporary file {temp_file_path}: {e}")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    logger.info(f"Starting server on port {port}...")
    app.run(host='0.0.0.0', port=port)