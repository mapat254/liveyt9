import streamlit as st
import os
import subprocess
import json
import sqlite3
import pandas as pd
from datetime import datetime
import time
import threading
import queue
import re
from pathlib import Path
import tempfile
import shutil
import logging
import sys

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('streaming_app.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Database setup
def init_database():
    """Initialize SQLite database with required tables"""
    try:
        conn = sqlite3.connect('streaming_app.db')
        cursor = conn.cursor()
        
        # Stream configurations table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stream_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                stream_key TEXT NOT NULL,
                video_file TEXT NOT NULL,
                resolution TEXT NOT NULL,
                bitrate INTEGER NOT NULL,
                shorts_mode BOOLEAN DEFAULT FALSE,
                audio_bitrate INTEGER DEFAULT 128,
                encoding_preset TEXT DEFAULT 'medium',
                auto_restart BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Stream history table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stream_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                config_name TEXT,
                video_file TEXT NOT NULL,
                resolution TEXT NOT NULL,
                bitrate INTEGER NOT NULL,
                duration INTEGER,
                status TEXT NOT NULL,
                start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                end_time TIMESTAMP,
                error_message TEXT
            )
        ''')
        
        # App settings table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Merge history table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS merge_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                output_file TEXT NOT NULL,
                input_files TEXT NOT NULL,
                merge_method TEXT NOT NULL,
                duration REAL,
                file_size INTEGER,
                status TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                error_message TEXT
            )
        ''')
        
        # Streaming logs table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS streaming_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                log_level TEXT NOT NULL,
                message TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        conn.commit()
        logger.info("Database initialized successfully")
        
    except Exception as e:
        logger.error(f"Error initializing database: {str(e)}")
        st.error(f"Database initialization error: {str(e)}")
    finally:
        conn.close()

# Initialize database
init_database()

# Global variables for streaming
if 'streaming_process' not in st.session_state:
    st.session_state.streaming_process = None
if 'streaming_active' not in st.session_state:
    st.session_state.streaming_active = False
if 'stream_stats' not in st.session_state:
    st.session_state.stream_stats = {}
if 'log_queue' not in st.session_state:
    st.session_state.log_queue = queue.Queue()
if 'uploaded_files' not in st.session_state:
    st.session_state.uploaded_files = []
if 'stream_logs' not in st.session_state:
    st.session_state.stream_logs = []
if 'session_id' not in st.session_state:
    st.session_state.session_id = f"stream_{int(time.time())}"

def log_to_database(session_id, level, message):
    """Log message to database"""
    try:
        conn = sqlite3.connect('streaming_app.db')
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO streaming_logs (session_id, log_level, message)
            VALUES (?, ?, ?)
        ''', (session_id, level, message))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error logging to database: {str(e)}")

def add_stream_log(level, message):
    """Add log message to session state and database"""
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {level}: {message}"
    st.session_state.stream_logs.append(log_entry)
    
    # Keep only last 100 logs
    if len(st.session_state.stream_logs) > 100:
        st.session_state.stream_logs = st.session_state.stream_logs[-100:]
    
    # Log to database
    log_to_database(st.session_state.session_id, level, message)
    
    # Log to file
    if level == "ERROR":
        logger.error(message)
    elif level == "WARNING":
        logger.warning(message)
    else:
        logger.info(message)

def get_video_info(video_path):
    """Get video information using ffprobe"""
    try:
        add_stream_log("INFO", f"Getting video info for: {video_path}")
        
        if not os.path.exists(video_path):
            add_stream_log("ERROR", f"Video file does not exist: {video_path}")
            return None
        
        cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_format', '-show_streams', video_path
        ]
        
        add_stream_log("INFO", f"Running ffprobe command: {' '.join(cmd)}")
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        
        if result.returncode != 0:
            add_stream_log("ERROR", f"ffprobe failed with return code {result.returncode}")
            add_stream_log("ERROR", f"ffprobe stderr: {result.stderr}")
            return None
            
        data = json.loads(result.stdout)
        
        # Find video stream
        video_stream = None
        for stream in data.get('streams', []):
            if stream.get('codec_type') == 'video':
                video_stream = stream
                break
        
        if not video_stream:
            add_stream_log("ERROR", "No video stream found in file")
            return None
        
        # Extract information with safe defaults
        info = {
            'duration': float(data.get('format', {}).get('duration', 0)),
            'size': int(data.get('format', {}).get('size', 0)),
            'bitrate': int(data.get('format', {}).get('bit_rate', 0)),
            'video_width': int(video_stream.get('width', 0)),
            'video_height': int(video_stream.get('height', 0)),
            'video_codec': video_stream.get('codec_name', 'unknown'),
            'fps': eval(video_stream.get('r_frame_rate', '0/1')) if '/' in str(video_stream.get('r_frame_rate', '0/1')) else 0
        }
        
        add_stream_log("INFO", f"Video info extracted successfully: {info}")
        return info
        
    except subprocess.TimeoutExpired:
        add_stream_log("ERROR", "ffprobe command timed out")
        return None
    except json.JSONDecodeError as e:
        add_stream_log("ERROR", f"Failed to parse ffprobe JSON output: {str(e)}")
        return None
    except Exception as e:
        add_stream_log("ERROR", f"Error getting video info: {str(e)}")
        return None

def format_duration(seconds):
    """Format duration in seconds to HH:MM:SS"""
    if seconds <= 0:
        return "00:00:00"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    seconds = int(seconds % 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

def format_file_size(bytes_size):
    """Format file size in bytes to human readable format"""
    if bytes_size == 0:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.1f} PB"

def save_stream_config(name, config):
    """Save streaming configuration to database"""
    try:
        conn = sqlite3.connect('streaming_app.db')
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT OR REPLACE INTO stream_configs 
            (name, stream_key, video_file, resolution, bitrate, shorts_mode, 
             audio_bitrate, encoding_preset, auto_restart, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (
            name, config['stream_key'], config['video_file'], 
            config['resolution'], config['bitrate'], config['shorts_mode'],
            config.get('audio_bitrate', 128), config.get('encoding_preset', 'medium'),
            config.get('auto_restart', False)
        ))
        conn.commit()
        add_stream_log("INFO", f"Configuration '{name}' saved successfully")
        return True
    except Exception as e:
        add_stream_log("ERROR", f"Error saving configuration: {str(e)}")
        return False
    finally:
        conn.close()

def load_stream_configs():
    """Load all streaming configurations from database"""
    try:
        conn = sqlite3.connect('streaming_app.db')
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM stream_configs ORDER BY updated_at DESC')
        configs = cursor.fetchall()
        
        config_dict = {}
        for config in configs:
            config_dict[config[1]] = {  # config[1] is name
                'stream_key': config[2],
                'video_file': config[3],
                'resolution': config[4],
                'bitrate': config[5],
                'shorts_mode': bool(config[6]),
                'audio_bitrate': config[7],
                'encoding_preset': config[8],
                'auto_restart': bool(config[9])
            }
        
        add_stream_log("INFO", f"Loaded {len(config_dict)} configurations")
        return config_dict
    except Exception as e:
        add_stream_log("ERROR", f"Error loading configurations: {str(e)}")
        return {}
    finally:
        conn.close()

def delete_stream_config(name):
    """Delete streaming configuration from database"""
    try:
        conn = sqlite3.connect('streaming_app.db')
        cursor = conn.cursor()
        
        cursor.execute('DELETE FROM stream_configs WHERE name = ?', (name,))
        conn.commit()
        add_stream_log("INFO", f"Configuration '{name}' deleted successfully")
        return True
    except Exception as e:
        add_stream_log("ERROR", f"Error deleting configuration: {str(e)}")
        return False
    finally:
        conn.close()

def save_stream_history(config_name, video_file, resolution, bitrate, duration, status, error_message=None):
    """Save streaming session to history"""
    try:
        conn = sqlite3.connect('streaming_app.db')
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO stream_history 
            (config_name, video_file, resolution, bitrate, duration, status, end_time, error_message)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
        ''', (config_name, video_file, resolution, bitrate, duration, status, error_message))
        conn.commit()
        add_stream_log("INFO", f"Stream history saved: {status}")
    except Exception as e:
        add_stream_log("ERROR", f"Error saving stream history: {str(e)}")
    finally:
        conn.close()

def get_stream_history():
    """Get streaming history from database"""
    try:
        conn = sqlite3.connect('streaming_app.db')
        df = pd.read_sql_query('''
            SELECT * FROM stream_history 
            ORDER BY start_time DESC
        ''', conn)
        return df
    except Exception as e:
        add_stream_log("ERROR", f"Error loading stream history: {str(e)}")
        return pd.DataFrame()
    finally:
        conn.close()

def get_app_setting(key, default_value):
    """Get application setting from database"""
    try:
        conn = sqlite3.connect('streaming_app.db')
        cursor = conn.cursor()
        
        cursor.execute('SELECT value FROM app_settings WHERE key = ?', (key,))
        result = cursor.fetchone()
        return result[0] if result else default_value
    except Exception:
        return default_value
    finally:
        conn.close()

def set_app_setting(key, value):
    """Set application setting in database"""
    try:
        conn = sqlite3.connect('streaming_app.db')
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT OR REPLACE INTO app_settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
        ''', (key, str(value)))
        conn.commit()
    except Exception as e:
        add_stream_log("ERROR", f"Error saving setting: {str(e)}")
    finally:
        conn.close()

def check_ffmpeg():
    """Check if FFmpeg is available"""
    try:
        add_stream_log("INFO", "Checking FFmpeg availability...")
        result = subprocess.run(['ffmpeg', '-version'], 
                              capture_output=True, text=True, timeout=10)
        
        if result.returncode == 0:
            add_stream_log("INFO", "FFmpeg is available")
            # Extract version info
            version_line = result.stdout.split('\n')[0]
            add_stream_log("INFO", f"FFmpeg version: {version_line}")
            return True
        else:
            add_stream_log("ERROR", f"FFmpeg check failed with return code: {result.returncode}")
            return False
            
    except subprocess.TimeoutExpired:
        add_stream_log("ERROR", "FFmpeg version check timed out")
        return False
    except FileNotFoundError:
        add_stream_log("ERROR", "FFmpeg not found in PATH")
        return False
    except Exception as e:
        add_stream_log("ERROR", f"Error checking FFmpeg: {str(e)}")
        return False

def get_video_files(directory="."):
    """Get list of video files in directory"""
    video_extensions = {'.mp4', '.avi', '.mov', '.mkv', '.flv', '.webm', '.m4v'}
    video_files = []
    
    try:
        add_stream_log("INFO", f"Scanning directory for video files: {directory}")
        for file_path in Path(directory).rglob('*'):
            if file_path.is_file() and file_path.suffix.lower() in video_extensions:
                video_files.append(str(file_path))
        
        add_stream_log("INFO", f"Found {len(video_files)} video files")
        return sorted(video_files)
        
    except Exception as e:
        add_stream_log("ERROR", f"Error scanning directory: {str(e)}")
        return []

def monitor_stream_output(process, log_queue, session_id):
    """Monitor FFmpeg output in separate thread"""
    try:
        add_stream_log("INFO", "Starting stream output monitoring thread")
        
        while process.poll() is None:
            try:
                # Read from stderr (FFmpeg outputs to stderr)
                line = process.stderr.readline()
                if line:
                    decoded_line = line.decode('utf-8', errors='ignore').strip()
                    if decoded_line:
                        log_queue.put(decoded_line)
                        
                        # Log important messages
                        if 'error' in decoded_line.lower():
                            add_stream_log("ERROR", f"FFmpeg error: {decoded_line}")
                        elif 'warning' in decoded_line.lower():
                            add_stream_log("WARNING", f"FFmpeg warning: {decoded_line}")
                        elif 'frame=' in decoded_line:
                            # This is a progress line, don't spam logs
                            pass
                        else:
                            add_stream_log("DEBUG", f"FFmpeg: {decoded_line}")
                            
            except Exception as e:
                add_stream_log("ERROR", f"Error reading FFmpeg output: {str(e)}")
                break
                
        add_stream_log("INFO", "Stream output monitoring thread ended")
        
    except Exception as e:
        add_stream_log("ERROR", f"Monitor thread error: {str(e)}")

def parse_ffmpeg_stats(line):
    """Parse FFmpeg statistics from output line"""
    stats = {}
    
    try:
        # Parse frame number
        frame_match = re.search(r'frame=\s*(\d+)', line)
        if frame_match:
            stats['frame'] = int(frame_match.group(1))
        
        # Parse FPS
        fps_match = re.search(r'fps=\s*([\d.]+)', line)
        if fps_match:
            stats['fps'] = float(fps_match.group(1))
        
        # Parse bitrate
        bitrate_match = re.search(r'bitrate=\s*([\d.]+)kbits/s', line)
        if bitrate_match:
            stats['bitrate'] = float(bitrate_match.group(1))
        
        # Parse time
        time_match = re.search(r'time=(\d{2}):(\d{2}):(\d{2})', line)
        if time_match:
            hours, minutes, seconds = map(int, time_match.groups())
            stats['time'] = hours * 3600 + minutes * 60 + seconds
        
        # Parse speed
        speed_match = re.search(r'speed=\s*([\d.]+)x', line)
        if speed_match:
            stats['speed'] = float(speed_match.group(1))
            
    except Exception as e:
        add_stream_log("ERROR", f"Error parsing FFmpeg stats: {str(e)}")
    
    return stats

def validate_stream_key(stream_key):
    """Validate YouTube stream key format"""
    if not stream_key:
        return False, "Stream key is empty"
    
    if len(stream_key) < 10:
        return False, "Stream key is too short"
    
    # YouTube stream keys are typically alphanumeric with dashes
    if not re.match(r'^[a-zA-Z0-9\-_]+$', stream_key):
        return False, "Stream key contains invalid characters"
    
    return True, "Stream key is valid"

def test_rtmp_connection(stream_key):
    """Test RTMP connection to YouTube"""
    try:
        add_stream_log("INFO", "Testing RTMP connection to YouTube...")
        
        rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{stream_key}"
        
        # Create a simple test command
        cmd = [
            'ffmpeg', '-f', 'lavfi', '-i', 'testsrc=duration=1:size=320x240:rate=1',
            '-f', 'flv', '-t', '1', rtmp_url
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        
        if result.returncode == 0:
            add_stream_log("INFO", "RTMP connection test successful")
            return True, "Connection successful"
        else:
            add_stream_log("ERROR", f"RTMP connection test failed: {result.stderr}")
            return False, f"Connection failed: {result.stderr}"
            
    except subprocess.TimeoutExpired:
        add_stream_log("ERROR", "RTMP connection test timed out")
        return False, "Connection test timed out"
    except Exception as e:
        add_stream_log("ERROR", f"Error testing RTMP connection: {str(e)}")
        return False, f"Connection test error: {str(e)}"

def start_streaming(stream_key, video_file, resolution, bitrate, shorts_mode=False, 
                   audio_bitrate=128, encoding_preset='medium'):
    """Start streaming process with comprehensive error handling"""
    try:
        add_stream_log("INFO", "=== STARTING STREAMING SESSION ===")
        add_stream_log("INFO", f"Stream key: {stream_key[:10]}...")
        add_stream_log("INFO", f"Video file: {video_file}")
        add_stream_log("INFO", f"Resolution: {resolution}")
        add_stream_log("INFO", f"Bitrate: {bitrate}k")
        add_stream_log("INFO", f"Shorts mode: {shorts_mode}")
        add_stream_log("INFO", f"Audio bitrate: {audio_bitrate}k")
        add_stream_log("INFO", f"Encoding preset: {encoding_preset}")
        
        # Check FFmpeg
        if not check_ffmpeg():
            add_stream_log("ERROR", "FFmpeg not found. Please install FFmpeg and add it to your PATH.")
            return False
        
        # Validate stream key
        is_valid, message = validate_stream_key(stream_key)
        if not is_valid:
            add_stream_log("ERROR", f"Invalid stream key: {message}")
            return False
        
        # Check video file
        if not os.path.exists(video_file):
            add_stream_log("ERROR", f"Video file not found: {video_file}")
            return False
        
        # Get video info
        video_info = get_video_info(video_file)
        if not video_info:
            add_stream_log("ERROR", "Could not read video file information")
            return False
        
        add_stream_log("INFO", f"Video duration: {format_duration(video_info['duration'])}")
        add_stream_log("INFO", f"Video resolution: {video_info['video_width']}x{video_info['video_height']}")
        
        # Build FFmpeg command
        rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{stream_key}"
        add_stream_log("INFO", f"RTMP URL: rtmp://a.rtmp.youtube.com/live2/{stream_key[:10]}...")
        
        cmd = ['ffmpeg', '-y', '-re', '-i', video_file]
        
        # Video encoding settings
        if resolution == "Original":
            add_stream_log("INFO", "Using original resolution")
            cmd.extend(['-c:v', 'libx264'])
        else:
            width, height = resolution.split('x')
            if shorts_mode:
                add_stream_log("INFO", "Applying YouTube Shorts format (9:16)")
                cmd.extend(['-vf', f'scale=720:1280,pad=720:1280:(ow-iw)/2:(oh-ih)/2'])
            else:
                add_stream_log("INFO", f"Scaling to {width}x{height}")
                cmd.extend(['-vf', f'scale={width}:{height}'])
            cmd.extend(['-c:v', 'libx264'])
        
        # Encoding settings
        cmd.extend([
            '-preset', encoding_preset,
            '-b:v', f'{bitrate}k',
            '-maxrate', f'{int(bitrate * 1.2)}k',
            '-bufsize', f'{int(bitrate * 2)}k',
            '-g', '60',  # GOP size
            '-keyint_min', '60',
            '-sc_threshold', '0',
            '-pix_fmt', 'yuv420p'
        ])
        
        # Audio settings
        cmd.extend([
            '-c:a', 'aac',
            '-b:a', f'{audio_bitrate}k',
            '-ar', '44100',
            '-ac', '2'
        ])
        
        # Output settings
        cmd.extend([
            '-f', 'flv',
            '-flvflags', 'no_duration_filesize',
            rtmp_url
        ])
        
        add_stream_log("INFO", f"FFmpeg command: {' '.join(cmd[:10])}... [truncated for security]")
        
        # Start FFmpeg process
        add_stream_log("INFO", "Starting FFmpeg process...")
        
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=False,
            bufsize=1
        )
        
        add_stream_log("INFO", f"FFmpeg process started with PID: {process.pid}")
        
        # Start monitoring thread
        monitor_thread = threading.Thread(
            target=monitor_stream_output,
            args=(process, st.session_state.log_queue, st.session_state.session_id),
            daemon=True
        )
        monitor_thread.start()
        add_stream_log("INFO", "Monitoring thread started")
        
        # Store process info
        st.session_state.streaming_process = process
        st.session_state.streaming_active = True
        st.session_state.stream_start_time = time.time()
        
        add_stream_log("INFO", "=== STREAMING SESSION STARTED SUCCESSFULLY ===")
        return True
        
    except Exception as e:
        add_stream_log("ERROR", f"Error starting stream: {str(e)}")
        return False

def stop_streaming():
    """Stop streaming process"""
    try:
        add_stream_log("INFO", "=== STOPPING STREAMING SESSION ===")
        
        if st.session_state.streaming_process:
            process = st.session_state.streaming_process
            add_stream_log("INFO", f"Terminating FFmpeg process (PID: {process.pid})")
            
            try:
                process.terminate()
                add_stream_log("INFO", "Sent SIGTERM to FFmpeg process")
                
                # Wait for graceful shutdown
                try:
                    process.wait(timeout=10)
                    add_stream_log("INFO", "FFmpeg process terminated gracefully")
                except subprocess.TimeoutExpired:
                    add_stream_log("WARNING", "FFmpeg process did not terminate gracefully, forcing kill")
                    process.kill()
                    process.wait()
                    add_stream_log("INFO", "FFmpeg process killed")
                    
            except Exception as e:
                add_stream_log("ERROR", f"Error stopping FFmpeg process: {str(e)}")
        
        # Calculate session duration
        if hasattr(st.session_state, 'stream_start_time'):
            duration = time.time() - st.session_state.stream_start_time
            add_stream_log("INFO", f"Stream duration: {format_duration(duration)}")
        
        # Reset state
        st.session_state.streaming_process = None
        st.session_state.streaming_active = False
        st.session_state.stream_stats = {}
        
        add_stream_log("INFO", "=== STREAMING SESSION STOPPED ===")
        
    except Exception as e:
        add_stream_log("ERROR", f"Error stopping stream: {str(e)}")

def emergency_stop():
    """Emergency stop - force kill streaming process"""
    try:
        add_stream_log("WARNING", "=== EMERGENCY STOP INITIATED ===")
        
        if st.session_state.streaming_process:
            process = st.session_state.streaming_process
            add_stream_log("WARNING", f"Force killing FFmpeg process (PID: {process.pid})")
            
            try:
                process.kill()
                process.wait()
                add_stream_log("WARNING", "FFmpeg process force killed")
            except Exception as e:
                add_stream_log("ERROR", f"Error during emergency stop: {str(e)}")
        
        # Reset state
        st.session_state.streaming_process = None
        st.session_state.streaming_active = False
        st.session_state.stream_stats = {}
        
        add_stream_log("WARNING", "=== EMERGENCY STOP COMPLETED ===")
        
    except Exception as e:
        add_stream_log("ERROR", f"Error during emergency stop: {str(e)}")

def merge_videos(input_files, output_file, method='auto'):
    """Merge multiple video files"""
    try:
        add_stream_log("INFO", f"Starting video merge: {len(input_files)} files")
        add_stream_log("INFO", f"Output file: {output_file}")
        add_stream_log("INFO", f"Merge method: {method}")
        
        if len(input_files) < 2:
            add_stream_log("ERROR", "Need at least 2 files to merge")
            return False, "Need at least 2 files to merge"
        
        # Check if all input files exist
        for file_path in input_files:
            if not os.path.exists(file_path):
                add_stream_log("ERROR", f"Input file not found: {file_path}")
                return False, f"Input file not found: {file_path}"
        
        if method == 'fast' or method == 'auto':
            # Try fast copy method first
            add_stream_log("INFO", "Attempting fast copy merge...")
            
            # Create file list for concat
            list_file = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
            try:
                for file_path in input_files:
                    list_file.write(f"file '{os.path.abspath(file_path)}'\n")
                list_file.close()
                
                cmd = [
                    'ffmpeg', '-y', '-f', 'concat', '-safe', '0',
                    '-i', list_file.name, '-c', 'copy', output_file
                ]
                
                add_stream_log("INFO", f"Running fast merge command: {' '.join(cmd)}")
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                
                if result.returncode == 0:
                    add_stream_log("INFO", "Fast copy merge successful")
                    return True, "Merge completed successfully (fast copy)"
                else:
                    add_stream_log("WARNING", f"Fast copy failed: {result.stderr}")
                    if method == 'fast':
                        return False, f"Fast copy merge failed: {result.stderr}"
                    # Fall back to re-encode for auto method
                    
            finally:
                try:
                    os.unlink(list_file.name)
                except:
                    pass
        
        if method == 're-encode' or method == 'auto':
            # Re-encode method
            add_stream_log("INFO", "Using re-encode merge method...")
            
            # Build complex filter
            inputs = []
            filter_complex = ""
            
            for i, file_path in enumerate(input_files):
                inputs.extend(['-i', file_path])
                filter_complex += f"[{i}:v][{i}:a]"
            
            filter_complex += f"concat=n={len(input_files)}:v=1:a=1[outv][outa]"
            
            cmd = ['ffmpeg', '-y'] + inputs + [
                '-filter_complex', filter_complex,
                '-map', '[outv]', '-map', '[outa]',
                '-c:v', 'libx264', '-c:a', 'aac',
                '-preset', 'medium', '-crf', '23',
                output_file
            ]
            
            add_stream_log("INFO", f"Running re-encode merge command")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            
            if result.returncode == 0:
                add_stream_log("INFO", "Re-encode merge successful")
                return True, "Merge completed successfully (re-encoded)"
            else:
                add_stream_log("ERROR", f"Re-encode merge failed: {result.stderr}")
                return False, f"Re-encode merge failed: {result.stderr}"
        
        return False, "Unknown merge method"
        
    except subprocess.TimeoutExpired:
        add_stream_log("ERROR", "Video merge timed out")
        return False, "Merge operation timed out"
    except Exception as e:
        add_stream_log("ERROR", f"Error during video merge: {str(e)}")
        return False, f"Merge error: {str(e)}"

def save_merge_history(output_file, input_files, method, duration, file_size, status, error_message=None):
    """Save merge operation to history"""
    try:
        conn = sqlite3.connect('streaming_app.db')
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO merge_history 
            (output_file, input_files, merge_method, duration, file_size, status, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (output_file, json.dumps(input_files), method, duration, file_size, status, error_message))
        conn.commit()
        add_stream_log("INFO", f"Merge history saved: {status}")
    except Exception as e:
        add_stream_log("ERROR", f"Error saving merge history: {str(e)}")
    finally:
        conn.close()

def show_control_panel():
    """Main streaming control panel"""
    st.header("🎥 Stream Control Center")
    
    # Check FFmpeg availability
    if not check_ffmpeg():
        st.error("⚠️ FFmpeg not detected! Please install FFmpeg to use this application.")
        st.info("Download FFmpeg from: https://ffmpeg.org/download.html")
        add_stream_log("ERROR", "FFmpeg not available")
        return
    
    # Configuration section
    with st.expander("📋 Stream Configuration", expanded=True):
        col1, col2 = st.columns(2)
        
        with col1:
            # Load saved configurations
            saved_configs = load_stream_configs()
            config_names = [""] + list(saved_configs.keys())
            
            selected_config = st.selectbox(
                "Load Saved Configuration",
                config_names,
                help="Select a saved configuration to load"
            )
            
            # Stream key input
            stream_key = st.text_input(
                "YouTube Stream Key",
                type="password",
                value=saved_configs.get(selected_config, {}).get('stream_key', ''),
                help="Get this from YouTube Studio > Go Live"
            )
            
            # Test stream key button
            if stream_key and st.button("🔍 Test Stream Key"):
                is_valid, message = validate_stream_key(stream_key)
                if is_valid:
                    st.success("✅ Stream key format is valid")
                    add_stream_log("INFO", "Stream key validation passed")
                else:
                    st.error(f"❌ {message}")
                    add_stream_log("ERROR", f"Stream key validation failed: {message}")
            
            # Video file selection
            video_files = get_video_files()
            if st.session_state.uploaded_files:
                video_files.extend(st.session_state.uploaded_files)
            
            if video_files:
                default_video = saved_configs.get(selected_config, {}).get('video_file', video_files[0])
                if default_video not in video_files:
                    default_video = video_files[0]
                
                video_file = st.selectbox(
                    "Select Video File",
                    video_files,
                    index=video_files.index(default_video) if default_video in video_files else 0
                )
            else:
                st.warning("No video files found. Please upload a video file.")
                video_file = None
        
        with col2:
            # Resolution settings
            resolutions = ["Original", "1920x1080", "1280x720", "854x480"]
            default_resolution = saved_configs.get(selected_config, {}).get('resolution', 'Original')
            
            resolution = st.selectbox(
                "Resolution",
                resolutions,
                index=resolutions.index(default_resolution) if default_resolution in resolutions else 0
            )
            
            # Bitrate settings
            default_bitrate = saved_configs.get(selected_config, {}).get('bitrate', 2500)
            bitrate = st.slider(
                "Video Bitrate (kbps)",
                min_value=500,
                max_value=8000,
                value=default_bitrate,
                step=100,
                help="Higher bitrate = better quality but requires more bandwidth"
            )
            
            # YouTube Shorts mode
            default_shorts = saved_configs.get(selected_config, {}).get('shorts_mode', False)
            shorts_mode = st.checkbox(
                "YouTube Shorts Mode (9:16)",
                value=default_shorts,
                help="Optimize for vertical video format"
            )
    
    # Advanced settings
    with st.expander("⚙️ Advanced Settings"):
        col1, col2, col3 = st.columns(3)
        
        with col1:
            default_audio_bitrate = saved_configs.get(selected_config, {}).get('audio_bitrate', 128)
            audio_bitrate = st.selectbox(
                "Audio Bitrate (kbps)",
                [96, 128, 192, 256],
                index=[96, 128, 192, 256].index(default_audio_bitrate) if default_audio_bitrate in [96, 128, 192, 256] else 1
            )
        
        with col2:
            default_preset = saved_configs.get(selected_config, {}).get('encoding_preset', 'medium')
            encoding_preset = st.selectbox(
                "Encoding Preset",
                ['ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower'],
                index=['ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower'].index(default_preset) if default_preset in ['ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower'] else 5
            )
        
        with col3:
            default_auto_restart = saved_configs.get(selected_config, {}).get('auto_restart', False)
            auto_restart = st.checkbox(
                "Auto Restart on Error",
                value=default_auto_restart
            )
    
    # Save configuration
    st.subheader("💾 Save Configuration")
    col1, col2, col3 = st.columns([2, 1, 1])
    
    with col1:
        config_name = st.text_input("Configuration Name", placeholder="Enter name to save current settings")
    
    with col2:
        if st.button("💾 Save Config", disabled=not config_name or not stream_key):
            config = {
                'stream_key': stream_key,
                'video_file': video_file,
                'resolution': resolution,
                'bitrate': bitrate,
                'shorts_mode': shorts_mode,
                'audio_bitrate': audio_bitrate,
                'encoding_preset': encoding_preset,
                'auto_restart': auto_restart
            }
            if save_stream_config(config_name, config):
                st.success(f"Configuration '{config_name}' saved!")
                st.rerun()
    
    with col3:
        if selected_config and st.button("🗑️ Delete Config"):
            if delete_stream_config(selected_config):
                st.success(f"Configuration '{selected_config}' deleted!")
                st.rerun()
    
    # Streaming controls
    st.subheader("🎮 Stream Controls")
    
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        start_disabled = (not stream_key or not video_file or 
                         st.session_state.streaming_active)
        
        if st.button("▶️ Start Streaming", disabled=start_disabled, type="primary"):
            if start_streaming(stream_key, video_file, resolution, bitrate, 
                             shorts_mode, audio_bitrate, encoding_preset):
                st.success("🚀 Streaming started!")
                st.rerun()
            else:
                st.error("❌ Failed to start streaming. Check logs for details.")
    
    with col2:
        if st.button("⏹️ Stop Streaming", 
                    disabled=not st.session_state.streaming_active):
            stop_streaming()
            st.success("⏹️ Streaming stopped!")
            st.rerun()
    
    with col3:
        if st.button("🚨 Emergency Stop", 
                    disabled=not st.session_state.streaming_active):
            emergency_stop()
            st.warning("🚨 Emergency stop executed!")
            st.rerun()
    
    with col4:
        # Status indicator
        if st.session_state.streaming_active:
            st.success("🔴 LIVE")
        else:
            st.info("⚫ OFFLINE")
    
    # Live statistics
    if st.session_state.streaming_active:
        show_live_statistics()
    
    # Live logs
    show_live_logs()

def show_live_statistics():
    """Show live streaming statistics"""
    st.subheader("📊 Live Statistics")
    
    # Process log queue to update statistics
    while not st.session_state.log_queue.empty():
        try:
            line = st.session_state.log_queue.get_nowait()
            stats = parse_ffmpeg_stats(line)
            if stats:
                st.session_state.stream_stats.update(stats)
        except queue.Empty:
            break
    
    if st.session_state.stream_stats:
        col1, col2, col3, col4, col5 = st.columns(5)
        
        with col1:
            st.metric("Frame", st.session_state.stream_stats.get('frame', 0))
        
        with col2:
            st.metric("FPS", f"{st.session_state.stream_stats.get('fps', 0):.1f}")
        
        with col3:
            st.metric("Bitrate", f"{st.session_state.stream_stats.get('bitrate', 0):.1f} kbps")
        
        with col4:
            elapsed = st.session_state.stream_stats.get('time', 0)
            st.metric("Time", format_duration(elapsed))
        
        with col5:
            speed = st.session_state.stream_stats.get('speed', 0)
            st.metric("Speed", f"{speed:.2f}x")
    
    # Process status
    if st.session_state.streaming_process:
        process = st.session_state.streaming_process
        if process.poll() is None:
            st.success("✅ FFmpeg process is running")
        else:
            st.error(f"❌ FFmpeg process ended with code: {process.returncode}")
            add_stream_log("ERROR", f"FFmpeg process ended unexpectedly with code: {process.returncode}")

def show_live_logs():
    """Show live streaming logs"""
    with st.expander("📋 Live Logs", expanded=False):
        
        # Log level filter
        col1, col2, col3 = st.columns([1, 1, 2])
        with col1:
            log_level_filter = st.selectbox(
                "Filter Level",
                ["ALL", "ERROR", "WARNING", "INFO", "DEBUG"],
                index=0
            )
        
        with col2:
            if st.button("🗑️ Clear Logs"):
                st.session_state.stream_logs = []
                add_stream_log("INFO", "Logs cleared by user")
                st.rerun()
        
        with col3:
            auto_scroll = st.checkbox("Auto-scroll to bottom", value=True)
        
        # Display logs
        if st.session_state.stream_logs:
            # Filter logs by level
            filtered_logs = st.session_state.stream_logs
            if log_level_filter != "ALL":
                filtered_logs = [log for log in st.session_state.stream_logs if log_level_filter in log]
            
            # Create scrollable log container
            log_container = st.container()
            
            with log_container:
                # Show logs in a text area for better formatting
                log_text = "\n".join(filtered_logs[-50:])  # Show last 50 logs
                st.text_area(
                    "Logs",
                    value=log_text,
                    height=300,
                    disabled=True,
                    key="log_display"
                )
        else:
            st.info("No logs available yet. Start streaming to see logs.")
        
        # Export logs
        if st.session_state.stream_logs:
            log_export = "\n".join(st.session_state.stream_logs)
            st.download_button(
                "📥 Download Logs",
                data=log_export,
                file_name=f"streaming_logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
                mime="text/plain"
            )

def show_file_manager():
    """File manager interface with video merge functionality"""
    st.header("📁 File Manager")
    
    # Current directory info
    current_dir = os.getcwd()
    st.info(f"📂 Current Directory: {current_dir}")
    
    # File upload
    st.subheader("📤 Upload Video Files")
    uploaded_files = st.file_uploader(
        "Choose video files",
        type=['mp4', 'avi', 'mov', 'mkv', 'flv', 'webm', 'm4v'],
        accept_multiple_files=True
    )
    
    if uploaded_files:
        for uploaded_file in uploaded_files:
            # Save uploaded file
            file_path = os.path.join(current_dir, uploaded_file.name)
            with open(file_path, "wb") as f:
                f.write(uploaded_file.getbuffer())
            
            if file_path not in st.session_state.uploaded_files:
                st.session_state.uploaded_files.append(file_path)
            
            add_stream_log("INFO", f"File uploaded: {uploaded_file.name}")
        
        st.success(f"✅ Uploaded {len(uploaded_files)} file(s)")
    
    # Video merge tool
    st.subheader("🔗 Video Merge Tool")
    
    video_files = get_video_files()
    if st.session_state.uploaded_files:
        video_files.extend(st.session_state.uploaded_files)
    
    if len(video_files) >= 2:
        with st.expander("🔗 Merge Videos", expanded=True):
            col1, col2 = st.columns([2, 1])
            
            with col1:
                # Multi-select for videos to merge
                selected_videos = st.multiselect(
                    "Select videos to merge (in order)",
                    video_files,
                    help="Select videos in the order you want them merged"
                )
                
                if len(selected_videos) >= 2:
                    # Show preview of selected videos
                    st.write("**Selected videos:**")
                    total_duration = 0
                    total_size = 0
                    
                    for i, video in enumerate(selected_videos):
                        info = get_video_info(video)
                        if info:
                            duration = info['duration']
                            size = info['size']
                            total_duration += duration
                            total_size += size
                            
                            st.write(f"{i+1}. {os.path.basename(video)} - "
                                   f"{format_duration(duration)} - "
                                   f"{format_file_size(size)}")
                    
                    st.write(f"**Total duration:** {format_duration(total_duration)}")
                    st.write(f"**Total size:** {format_file_size(total_size)}")
            
            with col2:
                # Merge settings
                merge_method = st.selectbox(
                    "Merge Method",
                    ["auto", "fast", "re-encode"],
                    help="Auto: Try fast first, fallback to re-encode\n"
                         "Fast: Quick copy (same format only)\n"
                         "Re-encode: Slower but works with any formats"
                )
                
                # Output filename
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                default_output = f"merged_video_{timestamp}.mp4"
                output_filename = st.text_input(
                    "Output filename",
                    value=default_output
                )
                
                # Merge button
                if st.button("🔗 Start Merge", 
                           disabled=len(selected_videos) < 2,
                           type="primary"):
                    
                    if len(selected_videos) < 2:
                        st.error("Please select at least 2 videos to merge")
                    else:
                        with st.spinner("Merging videos..."):
                            start_time = time.time()
                            
                            success, message = merge_videos(
                                selected_videos, 
                                output_filename, 
                                merge_method
                            )
                            
                            end_time = time.time()
                            merge_duration = end_time - start_time
                            
                            if success:
                                st.success(f"✅ {message}")
                                
                                # Get output file info
                                if os.path.exists(output_filename):
                                    file_size = os.path.getsize(output_filename)
                                    st.session_state.uploaded_files.append(output_filename)
                                    
                                    # Save to history
                                    save_merge_history(
                                        output_filename, 
                                        selected_videos, 
                                        merge_method,
                                        merge_duration,
                                        file_size,
                                        "success"
                                    )
                                    
                                    st.info(f"📁 Merged file saved as: {output_filename}")
                                    st.rerun()
                                
                            else:
                                st.error(f"❌ {message}")
                                
                                # Save failed attempt to history
                                save_merge_history(
                                    output_filename,
                                    selected_videos,
                                    merge_method,
                                    merge_duration,
                                    0,
                                    "failed",
                                    message
                                )
    else:
        st.info("Upload at least 2 video files to use the merge tool.")
    
    # Video files list
    st.subheader("🎬 Video Files")
    
    if video_files:
        for video_file in video_files:
            with st.expander(f"📹 {os.path.basename(video_file)}"):
                col1, col2 = st.columns([2, 1])
                
                with col1:
                    st.write(f"**Path:** {video_file}")
                    
                    # Get video information
                    info = get_video_info(video_file)
                    if info:
                        st.write(f"**Duration:** {format_duration(info['duration'])}")
                        st.write(f"**Size:** {format_file_size(info['size'])}")
                        st.write(f"**Resolution:** {info['video_width']}x{info['video_height']}")
                        st.write(f"**Codec:** {info['video_codec']}")
                        st.write(f"**FPS:** {info['fps']:.2f}")
                        st.write(f"**Bitrate:** {info['bitrate']} bps")
                    else:
                        st.warning("Could not read video information")
                
                with col2:
                    if st.button(f"🗑️ Delete", key=f"delete_{video_file}"):
                        try:
                            os.remove(video_file)
                            if video_file in st.session_state.uploaded_files:
                                st.session_state.uploaded_files.remove(video_file)
                            st.success(f"Deleted {os.path.basename(video_file)}")
                            add_stream_log("INFO", f"File deleted: {os.path.basename(video_file)}")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error deleting file: {str(e)}")
                            add_stream_log("ERROR", f"Error deleting file: {str(e)}")
    else:
        st.info("No video files found in the current directory.")
    
    # Directory cleanup
    st.subheader("🧹 Cleanup")
    if st.button("🗑️ Clear All Uploaded Files"):
        for file_path in st.session_state.uploaded_files[:]:
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                st.session_state.uploaded_files.remove(file_path)
                add_stream_log("INFO", f"Cleaned up file: {os.path.basename(file_path)}")
            except Exception as e:
                st.error(f"Error removing {file_path}: {str(e)}")
                add_stream_log("ERROR", f"Error removing {file_path}: {str(e)}")
        
        st.success("✅ Cleared all uploaded files")
        st.rerun()

def show_analytics():
    """Analytics and history dashboard"""
    st.header("📈 Analytics Dashboard")
    
    # Get streaming history
    history_df = get_stream_history()
    
    if history_df.empty:
        st.info("No streaming history available yet.")
        return
    
    # Summary statistics
    st.subheader("📊 Summary Statistics")
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        total_streams = len(history_df)
        st.metric("Total Streams", total_streams)
    
    with col2:
        successful_streams = len(history_df[history_df['status'] == 'completed'])
        success_rate = (successful_streams / total_streams * 100) if total_streams > 0 else 0
        st.metric("Success Rate", f"{success_rate:.1f}%")
    
    with col3:
        total_duration = history_df['duration'].fillna(0).sum()
        st.metric("Total Duration", format_duration(total_duration))
    
    with col4:
        avg_bitrate = history_df['bitrate'].mean()
        st.metric("Avg Bitrate", f"{avg_bitrate:.0f} kbps")
    
    # Charts
    st.subheader("📈 Charts")
    
    col1, col2 = st.columns(2)
    
    with col1:
        # Streams by status
        status_counts = history_df['status'].value_counts()
        st.bar_chart(status_counts)
        st.caption("Streams by Status")
    
    with col2:
        # Streams by resolution
        resolution_counts = history_df['resolution'].value_counts()
        st.bar_chart(resolution_counts)
        st.caption("Streams by Resolution")
    
    # Recent streams table
    st.subheader("📋 Recent Streams")
    
    # Format the dataframe for display
    display_df = history_df.copy()
    display_df['duration'] = display_df['duration'].fillna(0).apply(format_duration)
    display_df['start_time'] = pd.to_datetime(display_df['start_time']).dt.strftime('%Y-%m-%d %H:%M:%S')
    
    # Select columns to display
    columns_to_show = ['start_time', 'video_file', 'resolution', 'bitrate', 'duration', 'status']
    display_df = display_df[columns_to_show]
    
    st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True
    )
    
    # Export functionality
    st.subheader("📤 Export Data")
    col1, col2 = st.columns(2)
    
    with col1:
        if st.button("📊 Export to CSV"):
            csv = history_df.to_csv(index=False)
            st.download_button(
                label="💾 Download CSV",
                data=csv,
                file_name=f"streaming_history_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv"
            )
    
    with col2:
        if st.button("📋 Export to JSON"):
            json_data = history_df.to_json(orient='records', date_format='iso')
            st.download_button(
                label="💾 Download JSON",
                data=json_data,
                file_name=f"streaming_history_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                mime="application/json"
            )

def show_settings():
    """Application settings"""
    st.header("⚙️ Settings")
    
    # System information
    st.subheader("💻 System Information")
    col1, col2 = st.columns(2)
    
    with col1:
        st.info(f"**FFmpeg Status:** {'✅ Available' if check_ffmpeg() else '❌ Not Found'}")
        st.info(f"**Current Directory:** {os.getcwd()}")
        st.info(f"**Database:** streaming_app.db")
    
    with col2:
        # Database statistics
        conn = sqlite3.connect('streaming_app.db')
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM stream_configs")
        config_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM stream_history")
        history_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM streaming_logs")
        log_count = cursor.fetchone()[0]
        
        conn.close()
        
        st.info(f"**Saved Configurations:** {config_count}")
        st.info(f"**Stream History Records:** {history_count}")
        st.info(f"**Log Records:** {log_count}")
    
    # Application settings
    st.subheader("🔧 Application Settings")
    
    # Auto-refresh settings
    auto_refresh = st.checkbox(
        "Auto-refresh statistics",
        value=get_app_setting('auto_refresh', True),
        help="Automatically refresh live statistics during streaming"
    )
    set_app_setting('auto_refresh', auto_refresh)
    
    # Default settings
    st.subheader("📋 Default Settings")
    
    col1, col2 = st.columns(2)
    
    with col1:
        default_bitrate = st.slider(
            "Default Bitrate (kbps)",
            min_value=500,
            max_value=8000,
            value=int(get_app_setting('default_bitrate', 2500)),
            step=100
        )
        set_app_setting('default_bitrate', default_bitrate)
        
        default_resolution = st.selectbox(
            "Default Resolution",
            ["Original", "1920x1080", "1280x720", "854x480"],
            index=["Original", "1920x1080", "1280x720", "854x480"].index(
                get_app_setting('default_resolution', 'Original')
            )
        )
        set_app_setting('default_resolution', default_resolution)
    
    with col2:
        default_audio_bitrate = st.selectbox(
            "Default Audio Bitrate (kbps)",
            [96, 128, 192, 256],
            index=[96, 128, 192, 256].index(
                int(get_app_setting('default_audio_bitrate', 128))
            )
        )
        set_app_setting('default_audio_bitrate', default_audio_bitrate)
        
        default_preset = st.selectbox(
            "Default Encoding Preset",
            ['ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower'],
            index=['ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower'].index(
                get_app_setting('default_preset', 'medium')
            )
        )
        set_app_setting('default_preset', default_preset)
    
    # Database management
    st.subheader("🗄️ Database Management")
    
    col1, col2, col3 = st.columns(3)
    
    with col1:
        if st.button("🔄 Reset Database", type="secondary"):
            if st.button("⚠️ Confirm Reset", type="secondary"):
                try:
                    conn = sqlite3.connect('streaming_app.db')
                    cursor = conn.cursor()
                    
                    cursor.execute("DELETE FROM stream_configs")
                    cursor.execute("DELETE FROM stream_history")
                    cursor.execute("DELETE FROM app_settings")
                    cursor.execute("DELETE FROM merge_history")
                    cursor.execute("DELETE FROM streaming_logs")
                    
                    conn.commit()
                    conn.close()
                    
                    st.success("✅ Database reset successfully!")
                    add_stream_log("INFO", "Database reset by user")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error resetting database: {str(e)}")
                    add_stream_log("ERROR", f"Error resetting database: {str(e)}")
    
    with col2:
        if st.button("📤 Export Database"):
            try:
                # Create backup
                backup_path = f"streaming_app_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
                shutil.copy2('streaming_app.db', backup_path)
                
                with open(backup_path, 'rb') as f:
                    st.download_button(
                        label="💾 Download Backup",
                        data=f.read(),
                        file_name=backup_path,
                        mime="application/octet-stream"
                    )
                
                # Clean up temporary file
                os.remove(backup_path)
                add_stream_log("INFO", "Database exported successfully")
                
            except Exception as e:
                st.error(f"Error creating backup: {str(e)}")
                add_stream_log("ERROR", f"Error creating backup: {str(e)}")
    
    with col3:
        uploaded_db = st.file_uploader("📥 Import Database", type=['db'])
        if uploaded_db:
            try:
                with open('streaming_app_temp.db', 'wb') as f:
                    f.write(uploaded_db.getbuffer())
                
                # Verify it's a valid database
                conn = sqlite3.connect('streaming_app_temp.db')
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = cursor.fetchall()
                conn.close()
                
                if any('stream_configs' in str(table) for table in tables):
                    shutil.move('streaming_app_temp.db', 'streaming_app.db')
                    st.success("✅ Database imported successfully!")
                    add_stream_log("INFO", "Database imported successfully")
                    st.rerun()
                else:
                    os.remove('streaming_app_temp.db')
                    st.error("Invalid database file format")
                    add_stream_log("ERROR", "Invalid database file format")
                    
            except Exception as e:
                st.error(f"Error importing database: {str(e)}")
                add_stream_log("ERROR", f"Error importing database: {str(e)}")
                if os.path.exists('streaming_app_temp.db'):
                    os.remove('streaming_app_temp.db')

def main():
    """Main application"""
    st.set_page_config(
        page_title="YouTube Live Streamer Pro",
        page_icon="🚀",
        layout="wide",
        initial_sidebar_state="expanded"
    )
    
    # Custom CSS
    st.markdown("""
    <style>
    .main-header {
        text-align: center;
        padding: 1rem 0;
        background: linear-gradient(90deg, #ff6b6b, #4ecdc4);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        font-size: 2.5rem;
        font-weight: bold;
        margin-bottom: 2rem;
    }
    
    .status-live {
        background-color: #ff4444;
        color: white;
        padding: 0.5rem 1rem;
        border-radius: 20px;
        font-weight: bold;
        text-align: center;
    }
    
    .status-offline {
        background-color: #666666;
        color: white;
        padding: 0.5rem 1rem;
        border-radius: 20px;
        font-weight: bold;
        text-align: center;
    }
    
    .metric-card {
        background-color: #f0f2f6;
        padding: 1rem;
        border-radius: 10px;
        border-left: 4px solid #4ecdc4;
    }
    </style>
    """, unsafe_allow_html=True)
    
    # Main header
    st.markdown('<h1 class="main-header">🚀 Advanced YouTube Live Streamer Pro</h1>', 
                unsafe_allow_html=True)
    
    # Sidebar navigation
    st.sidebar.title("🎛️ Control Panel")
    
    # Status indicator in sidebar
    if st.session_state.streaming_active:
        st.sidebar.markdown(
            '<div class="status-live">🔴 LIVE</div>', 
            unsafe_allow_html=True
        )
    else:
        st.sidebar.markdown(
            '<div class="status-offline">⚫ OFFLINE</div>', 
            unsafe_allow_html=True
        )
    
    # Auto-refresh toggle
    auto_refresh = st.sidebar.checkbox("🔄 Auto Refresh (5s)", value=False)
    
    # Navigation
    page = st.sidebar.selectbox(
        "Select Page",
        ["🎥 Stream Control", "📁 File Manager", "📈 Analytics", "⚙️ Settings"]
    )
    
    # Page routing
    if page == "🎥 Stream Control":
        show_control_panel()
    elif page == "📁 File Manager":
        show_file_manager()
    elif page == "📈 Analytics":
        show_analytics()
    elif page == "⚙️ Settings":
        show_settings()
    
    # Auto-refresh functionality
    if auto_refresh and st.session_state.streaming_active:
        time.sleep(5)
        st.rerun()
    
    # Footer
    st.sidebar.markdown("---")
    st.sidebar.markdown("Built with ❤️ using Streamlit")
    st.sidebar.markdown("Powered by FFmpeg")

if __name__ == "__main__":
    main()
