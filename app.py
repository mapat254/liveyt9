import streamlit as st
import subprocess
import threading
import time
import os
import sqlite3
import pandas as pd
import json
from datetime import datetime
import uuid
import logging
import queue
import re
from contextlib import contextmanager

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Database connection pool and thread safety
@contextmanager
def get_db_connection():
    """Thread-safe database connection with proper error handling"""
    conn = None
    try:
        conn = sqlite3.connect('streaming_app.db', timeout=30.0, check_same_thread=False)
        conn.execute('PRAGMA journal_mode=WAL')  # Enable WAL mode for better concurrency
        conn.execute('PRAGMA synchronous=NORMAL')  # Balance between safety and performance
        conn.execute('PRAGMA temp_store=memory')  # Use memory for temporary storage
        conn.execute('PRAGMA cache_size=10000')  # Increase cache size
        yield conn
    except Exception as e:
        logger.error(f"Database connection error: {e}")
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()

# Database setup
def init_database():
    """Initialize database with proper schema and constraints"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Create tables with proper constraints
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS stream_configs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    stream_key TEXT NOT NULL,
                    video_file TEXT NOT NULL,
                    resolution TEXT NOT NULL,
                    bitrate INTEGER NOT NULL,
                    audio_bitrate INTEGER NOT NULL,
                    encoding_preset TEXT NOT NULL,
                    shorts_mode BOOLEAN NOT NULL DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS stream_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    video_file TEXT NOT NULL,
                    resolution TEXT NOT NULL,
                    bitrate INTEGER NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT,
                    duration INTEGER,
                    status TEXT NOT NULL DEFAULT 'STARTING',
                    error_message TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS stream_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL DEFAULT 'system',
                    level TEXT NOT NULL DEFAULT 'INFO',
                    message TEXT NOT NULL,
                    timestamp TEXT DEFAULT (datetime('now'))
                )
            ''')
            
            # Create indexes for better performance
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_stream_history_session ON stream_history(session_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_stream_logs_session ON stream_logs(session_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_stream_logs_timestamp ON stream_logs(timestamp)')
            
            conn.commit()
            logger.info("Database initialized successfully")
            
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise

# Initialize database
init_database()

# Global variables
if 'streaming_process' not in st.session_state:
    st.session_state.streaming_process = None
if 'streaming_active' not in st.session_state:
    st.session_state.streaming_active = False
if 'stream_stats' not in st.session_state:
    st.session_state.stream_stats = {}
if 'current_session_id' not in st.session_state:
    st.session_state.current_session_id = str(uuid.uuid4())
if 'log_queue' not in st.session_state:
    st.session_state.log_queue = queue.Queue()
if 'stream_logs' not in st.session_state:
    st.session_state.stream_logs = []

def log_message(level, message, session_id=None):
    """Add log message to queue and database with proper error handling"""
    try:
        # Ensure we have a valid session_id
        if not session_id:
            session_id = st.session_state.get('current_session_id', 'system')
        
        # Ensure session_id is not None or empty
        if not session_id or session_id.strip() == '':
            session_id = 'system'
        
        timestamp = datetime.now().isoformat()
        log_entry = {
            'timestamp': datetime.now(),
            'level': level,
            'message': message,
            'session_id': session_id
        }
        
        # Add to session state (limit to last 1000 entries)
        st.session_state.stream_logs.append(log_entry)
        if len(st.session_state.stream_logs) > 1000:
            st.session_state.stream_logs = st.session_state.stream_logs[-1000:]
        
        # Add to database with retry mechanism
        max_retries = 3
        for attempt in range(max_retries):
            try:
                with get_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute('''
                        INSERT INTO stream_logs (session_id, level, message, timestamp)
                        VALUES (?, ?, ?, ?)
                    ''', (session_id, level, message, timestamp))
                    conn.commit()
                break  # Success, exit retry loop
                
            except sqlite3.IntegrityError as e:
                logger.error(f"Database integrity error (attempt {attempt + 1}): {e}")
                if attempt == max_retries - 1:
                    # Last attempt failed, log to console only
                    print(f"[{timestamp}] {level}: {message}")
                else:
                    time.sleep(0.1)  # Brief delay before retry
                    
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e).lower():
                    logger.warning(f"Database locked (attempt {attempt + 1}), retrying...")
                    if attempt == max_retries - 1:
                        print(f"[{timestamp}] {level}: {message}")
                    else:
                        time.sleep(0.2)  # Wait longer for lock to release
                else:
                    logger.error(f"Database operational error: {e}")
                    break
                    
            except Exception as e:
                logger.error(f"Unexpected database error: {e}")
                break
                
    except Exception as e:
        # Fallback: log to console if all else fails
        print(f"[{datetime.now().isoformat()}] {level}: {message}")
        print(f"Log error: {e}")

def check_ffmpeg():
    """Check if FFmpeg is available"""
    try:
        result = subprocess.run(['ffmpeg', '-version'], 
                              capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except:
        return False

def get_video_info(video_path):
    """Get video information using FFprobe"""
    try:
        cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_format', '-show_streams', video_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return json.loads(result.stdout)
        return None
    except Exception as e:
        log_message('ERROR', f"Failed to get video info: {e}")
        return None

def validate_stream_key(stream_key):
    """Validate YouTube stream key format"""
    if not stream_key or len(stream_key.strip()) == 0:
        return False, "Stream key cannot be empty"
    
    # Basic validation - YouTube stream keys are typically 20-40 characters
    clean_key = stream_key.strip()
    if len(clean_key) < 10:
        return False, "Stream key appears too short"
    
    if len(clean_key) > 100:
        return False, "Stream key appears too long"
    
    # Check for invalid characters
    if any(char in clean_key for char in [' ', '\n', '\r', '\t']):
        return False, "Stream key contains invalid characters"
    
    return True, "Stream key format appears valid"

def build_optimized_ffmpeg_command(video_file, stream_key, resolution, bitrate, audio_bitrate, encoding_preset, shorts_mode):
    """Build optimized FFmpeg command with anti-buffering parameters"""
    
    # Base command with optimized buffering settings
    cmd = [
        'ffmpeg',
        '-y',  # Overwrite output files
        '-re',  # Read input at native frame rate (essential for live streaming)
        '-stream_loop', '-1',  # Loop the video indefinitely
        '-i', video_file,  # Input file
        '-c:v', 'libx264',  # Video codec
        '-preset', encoding_preset,  # Encoding preset
        '-tune', 'zerolatency',  # Optimize for low latency
        '-profile:v', 'high',  # H.264 profile
        '-level', '4.1',  # H.264 level
        '-pix_fmt', 'yuv420p',  # Pixel format
        '-g', '60',  # GOP size (2 seconds at 30fps)
        '-keyint_min', '30',  # Minimum keyframe interval
        '-sc_threshold', '0',  # Disable scene change detection
        '-b:v', f'{bitrate}k',  # Video bitrate
        '-maxrate', f'{int(bitrate * 1.2)}k',  # Maximum bitrate (20% buffer)
        '-bufsize', f'{int(bitrate * 2)}k',  # Buffer size (2x bitrate)
        '-c:a', 'aac',  # Audio codec
        '-b:a', f'{audio_bitrate}k',  # Audio bitrate
        '-ar', '44100',  # Audio sample rate
        '-ac', '2',  # Audio channels (stereo)
        '-f', 'flv',  # Output format
        
        # Advanced buffering and streaming optimizations
        '-fflags', '+genpts+igndts',  # Generate PTS and ignore DTS
        '-avoid_negative_ts', 'make_zero',  # Handle negative timestamps
        '-max_muxing_queue_size', '1024',  # Increase muxing queue size
        '-muxdelay', '0',  # No mux delay
        '-muxpreload', '0',  # No mux preload
        
        # TCP and network optimizations
        '-rtmp_live', 'live',  # RTMP live mode
        '-rtmp_buffer', '100',  # RTMP buffer size (ms)
        '-rtmp_flush_interval', '10',  # RTMP flush interval (ms)
        
        # Threading optimizations
        '-threads', '0',  # Use all available CPU cores
        '-thread_type', 'slice',  # Threading type
    ]
    
    # Resolution settings with aspect ratio optimization
    if resolution == "Original":
        # Keep original resolution but optimize for streaming
        cmd.extend(['-vf', 'format=yuv420p'])
    else:
        width, height = resolution.split('x')
        if shorts_mode:
            # For YouTube Shorts (9:16 aspect ratio)
            cmd.extend(['-vf', f'scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,format=yuv420p'])
        else:
            # Standard aspect ratio with padding if needed
            cmd.extend(['-vf', f'scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,format=yuv420p'])
    
    # Add RTMP URL
    rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{stream_key}"
    cmd.append(rtmp_url)
    
    return cmd

def monitor_ffmpeg_output(process, session_id):
    """Monitor FFmpeg output and extract statistics"""
    stats_pattern = re.compile(
        r'frame=\s*(\d+)\s+fps=\s*([\d.]+)\s+q=[\d.-]+\s+size=\s*(\d+)kB\s+time=(\d{2}:\d{2}:\d{2}\.\d{2})\s+bitrate=\s*([\d.]+)kbits/s\s+speed=\s*([\d.]+)x'
    )
    
    buffer = ""
    
    try:
        while process.poll() is None:
            output = process.stderr.read(1024)
            if output:
                try:
                    text = output.decode('utf-8', errors='ignore')
                    buffer += text
                    
                    # Process complete lines
                    while '\n' in buffer:
                        line, buffer = buffer.split('\n', 1)
                        
                        # Log FFmpeg output
                        if line.strip():
                            if 'error' in line.lower() or 'failed' in line.lower():
                                log_message('ERROR', f"FFmpeg: {line.strip()}", session_id)
                            elif 'warning' in line.lower():
                                log_message('WARNING', f"FFmpeg: {line.strip()}", session_id)
                            else:
                                log_message('DEBUG', f"FFmpeg: {line.strip()}", session_id)
                        
                        # Extract statistics
                        match = stats_pattern.search(line)
                        if match:
                            frame, fps, size_kb, time_str, bitrate, speed = match.groups()
                            
                            # Parse time
                            time_parts = time_str.split(':')
                            total_seconds = int(time_parts[0]) * 3600 + int(time_parts[1]) * 60 + float(time_parts[2])
                            
                            st.session_state.stream_stats = {
                                'frame': int(frame),
                                'fps': float(fps),
                                'size_kb': int(size_kb),
                                'time': time_str,
                                'total_seconds': total_seconds,
                                'bitrate': float(bitrate),
                                'speed': float(speed),
                                'last_update': datetime.now()
                            }
                            
                            # Log statistics periodically
                            if int(frame) % 300 == 0:  # Every 300 frames (~10 seconds at 30fps)
                                log_message('INFO', f"Streaming stats - Frame: {frame}, FPS: {fps}, Bitrate: {bitrate}kbps, Speed: {speed}x", session_id)
                
                except Exception as e:
                    log_message('ERROR', f"Error processing FFmpeg output: {e}", session_id)
            
            time.sleep(0.1)
    
    except Exception as e:
        log_message('ERROR', f"Error monitoring FFmpeg output: {e}", session_id)

def start_streaming(video_file, stream_key, resolution, bitrate, audio_bitrate, encoding_preset, shorts_mode):
    """Start streaming with optimized settings"""
    try:
        # Generate session ID
        session_id = str(uuid.uuid4())
        st.session_state.current_session_id = session_id
        
        log_message('INFO', f"Starting new streaming session: {session_id}", session_id)
        log_message('INFO', f"Video: {os.path.basename(video_file)}, Resolution: {resolution}, Bitrate: {bitrate}kbps", session_id)
        
        # Validate inputs
        if not os.path.exists(video_file):
            raise Exception(f"Video file not found: {video_file}")
        
        is_valid, message = validate_stream_key(stream_key)
        if not is_valid:
            raise Exception(f"Invalid stream key: {message}")
        
        # Build optimized FFmpeg command
        cmd = build_optimized_ffmpeg_command(
            video_file, stream_key, resolution, bitrate, 
            audio_bitrate, encoding_preset, shorts_mode
        )
        
        # Log command (sanitized)
        sanitized_cmd = [arg if 'rtmp://' not in arg else 'rtmp://a.rtmp.youtube.com/live2/[HIDDEN]' for arg in cmd]
        log_message('DEBUG', f"FFmpeg command: {' '.join(sanitized_cmd)}", session_id)
        
        # Start FFmpeg process with optimized settings
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            bufsize=0,  # Unbuffered
            universal_newlines=False
        )
        
        st.session_state.streaming_process = process
        st.session_state.streaming_active = True
        
        # Start monitoring thread
        monitor_thread = threading.Thread(
            target=monitor_ffmpeg_output,
            args=(process, session_id),
            daemon=True
        )
        monitor_thread.start()
        
        # Save to history with proper error handling
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO stream_history (session_id, video_file, resolution, bitrate, start_time, status)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (session_id, video_file, resolution, bitrate, datetime.now().isoformat(), 'STREAMING'))
                conn.commit()
        except Exception as db_error:
            log_message('WARNING', f"Failed to save stream history: {db_error}", session_id)
        
        log_message('INFO', "Streaming started successfully", session_id)
        return True, "Streaming started successfully"
        
    except Exception as e:
        error_msg = str(e)
        log_message('ERROR', f"Failed to start streaming: {error_msg}", session_id)
        return False, error_msg

def stop_streaming():
    """Stop streaming"""
    try:
        session_id = st.session_state.get('current_session_id')
        
        if st.session_state.streaming_process:
            log_message('INFO', "Stopping streaming...", session_id)
            
            # Gracefully terminate FFmpeg
            try:
                st.session_state.streaming_process.stdin.write(b'q')
                st.session_state.streaming_process.stdin.flush()
                
                # Wait for process to terminate
                try:
                    st.session_state.streaming_process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    log_message('WARNING', "FFmpeg didn't terminate gracefully, forcing kill", session_id)
                    st.session_state.streaming_process.kill()
                    st.session_state.streaming_process.wait()
                
            except:
                # Force kill if graceful termination fails
                st.session_state.streaming_process.kill()
                st.session_state.streaming_process.wait()
            
            # Update history with proper error handling
            if session_id:
                try:
                    with get_db_connection() as conn:
                        cursor = conn.cursor()
                        end_time = datetime.now().isoformat()
                        cursor.execute('''
                            UPDATE stream_history 
                            SET end_time = ?, status = ?
                            WHERE session_id = ? AND end_time IS NULL
                        ''', (end_time, 'STOPPED', session_id))
                        conn.commit()
                except Exception as db_error:
                    log_message('WARNING', f"Failed to update stream history: {db_error}", session_id)
            
            st.session_state.streaming_process = None
            st.session_state.streaming_active = False
            st.session_state.stream_stats = {}
            
            log_message('INFO', "Streaming stopped successfully", session_id)
            return True, "Streaming stopped successfully"
        else:
            return False, "No active streaming process"
            
    except Exception as e:
        error_msg = str(e)
        log_message('ERROR', f"Error stopping streaming: {error_msg}", session_id)
        return False, error_msg

def get_video_files(directory="/mount/src/liveyt9"):
    """Get list of video files"""
    # Use the correct directory path
    if not os.path.exists(directory):
        directory = "/mount/src/liveyt8"  # Fallback to correct directory
    
    video_extensions = ['.mp4', '.avi', '.mov', '.mkv', '.flv', '.webm', '.m4v']
    video_files = []
    
    try:
        if os.path.exists(directory):
            for file in os.listdir(directory):
                if any(file.lower().endswith(ext) for ext in video_extensions):
                    file_path = os.path.join(directory, file)
                    try:
                        size = os.path.getsize(file_path)
                        video_files.append({
                            'name': file,
                            'path': file_path,
                            'size': size,
                            'size_mb': round(size / (1024 * 1024), 2)
                        })
                    except:
                        continue
    except Exception as e:
        log_message('ERROR', f"Error scanning video files: {e}")
    
    return sorted(video_files, key=lambda x: x['name'])

def save_configuration(name, config):
    """Save streaming configuration"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO stream_configs 
                (name, stream_key, video_file, resolution, bitrate, audio_bitrate, encoding_preset, shorts_mode)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (name, config['stream_key'], config['video_file'], config['resolution'], 
                  config['bitrate'], config['audio_bitrate'], config['encoding_preset'], config['shorts_mode']))
            conn.commit()
        return True
    except Exception as e:
        log_message('ERROR', f"Failed to save configuration: {e}")
        return False

def load_configurations():
    """Load saved configurations"""
    try:
        with get_db_connection() as conn:
            df = pd.read_sql_query('SELECT * FROM stream_configs ORDER BY created_at DESC', conn)
        return df
    except Exception as e:
        log_message('ERROR', f"Failed to load configurations: {e}")
        return pd.DataFrame()

def show_stream_control():
    """Stream Control Interface"""
    st.header("🎥 Stream Control Center")
    
    # Check FFmpeg availability
    if not check_ffmpeg():
        st.error("❌ FFmpeg not found! Please install FFmpeg to use streaming features.")
        st.info("Install FFmpeg from: https://ffmpeg.org/download.html")
        return
    
    # Stream key input
    col1, col2 = st.columns([3, 1])
    with col1:
        stream_key = st.text_input(
            "🔑 YouTube Stream Key",
            type="password",
            help="Get your stream key from YouTube Studio > Go Live"
        )
    
    with col2:
        if st.button("🔍 Test Key", disabled=not stream_key):
            is_valid, message = validate_stream_key(stream_key)
            if is_valid:
                st.success("✅ Valid")
            else:
                st.error(f"❌ {message}")
    
    # Video file selection
    video_files = get_video_files()
    if not video_files:
        st.warning("📁 No video files found. Please upload videos to the File Manager.")
        return
    
    selected_video = st.selectbox(
        "📹 Select Video File",
        options=video_files,
        format_func=lambda x: f"{x['name']} ({x['size_mb']} MB)"
    )
    
    # Configuration options
    col1, col2, col3 = st.columns(3)
    
    with col1:
        resolution = st.selectbox(
            "📺 Resolution",
            ["Original", "1920x1080", "1280x720", "854x480", "640x360"]
        )
        
        shorts_mode = st.checkbox(
            "📱 YouTube Shorts Mode (9:16)",
            help="Optimize for vertical video format"
        )
    
    with col2:
        bitrate = st.slider(
            "📡 Video Bitrate (kbps)",
            min_value=500,
            max_value=8000,
            value=2500,
            step=100,
            help="Higher bitrate = better quality but requires more bandwidth"
        )
        
        audio_bitrate = st.selectbox(
            "🔊 Audio Bitrate (kbps)",
            [64, 96, 128, 160, 192, 256],
            index=2
        )
    
    with col3:
        encoding_preset = st.selectbox(
            "⚙️ Encoding Preset",
            ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"],
            index=2,
            help="Faster presets use less CPU but may reduce quality"
        )
    
    # Streaming controls
    st.markdown("---")
    
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        if not st.session_state.streaming_active:
            if st.button("▶️ Start Streaming", type="primary", disabled=not stream_key or not selected_video):
                with st.spinner("Starting stream..."):
                    success, message = start_streaming(
                        selected_video['path'],
                        stream_key,
                        resolution,
                        bitrate,
                        audio_bitrate,
                        encoding_preset,
                        shorts_mode
                    )
                    if success:
                        st.success(message)
                        st.rerun()
                    else:
                        st.error(message)
        else:
            st.success("🔴 STREAMING ACTIVE")
    
    with col2:
        if st.session_state.streaming_active:
            if st.button("⏹️ Stop Streaming", type="secondary"):
                with st.spinner("Stopping stream..."):
                    success, message = stop_streaming()
                    if success:
                        st.success(message)
                        st.rerun()
                    else:
                        st.error(message)
    
    with col3:
        if st.session_state.streaming_active:
            if st.button("🚨 Emergency Stop", type="secondary"):
                try:
                    if st.session_state.streaming_process:
                        st.session_state.streaming_process.kill()
                        st.session_state.streaming_process = None
                    st.session_state.streaming_active = False
                    st.session_state.stream_stats = {}
                    st.success("Emergency stop executed")
                    st.rerun()
                except Exception as e:
                    st.error(f"Emergency stop failed: {e}")
    
    with col4:
        # Save configuration
        config_name = st.text_input("💾 Config Name", placeholder="My Setup")
        if st.button("💾 Save Config", disabled=not config_name):
            config = {
                'stream_key': stream_key,
                'video_file': selected_video['path'],
                'resolution': resolution,
                'bitrate': bitrate,
                'audio_bitrate': audio_bitrate,
                'encoding_preset': encoding_preset,
                'shorts_mode': shorts_mode
            }
            if save_configuration(config_name, config):
                st.success("Configuration saved!")
            else:
                st.error("Failed to save configuration")
    
    # Live statistics
    if st.session_state.streaming_active and st.session_state.stream_stats:
        st.markdown("---")
        st.subheader("📊 Live Statistics")
        
        stats = st.session_state.stream_stats
        
        col1, col2, col3, col4, col5 = st.columns(5)
        
        with col1:
            st.metric("🎬 Frames", f"{stats.get('frame', 0):,}")
        
        with col2:
            st.metric("📈 FPS", f"{stats.get('fps', 0):.1f}")
        
        with col3:
            st.metric("📡 Bitrate", f"{stats.get('bitrate', 0):.1f} kbps")
        
        with col4:
            st.metric("⏱️ Time", stats.get('time', '00:00:00'))
        
        with col5:
            speed = stats.get('speed', 0)
            speed_color = "normal" if 0.95 <= speed <= 1.05 else "inverse"
            st.metric("⚡ Speed", f"{speed:.2f}x", delta_color=speed_color)
        
        # Progress bar
        if stats.get('total_seconds', 0) > 0:
            # For looped video, show progress within current loop
            video_info = get_video_info(selected_video['path'])
            if video_info and 'format' in video_info:
                duration = float(video_info['format'].get('duration', 0))
                if duration > 0:
                    current_pos = stats['total_seconds'] % duration
                    progress = current_pos / duration
                    st.progress(progress, text=f"Video Progress: {current_pos:.1f}s / {duration:.1f}s")

def show_live_logs():
    """Show live logs with filtering"""
    st.subheader("📋 Live Logs")
    
    # Log controls
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        log_level_filter = st.selectbox(
            "Log Level",
            ["ALL", "ERROR", "WARNING", "INFO", "DEBUG"],
            key="log_level_filter"
        )
    
    with col2:
        auto_scroll = st.checkbox("Auto Scroll", value=True, key="auto_scroll")
    
    with col3:
        if st.button("🗑️ Clear Logs"):
            st.session_state.stream_logs = []
            st.rerun()
    
    with col4:
        if st.button("📥 Export Logs"):
            if st.session_state.stream_logs:
                log_text = "\n".join([
                    f"[{log['timestamp'].strftime('%Y-%m-%d %H:%M:%S')}] {log['level']}: {log['message']}"
                    for log in st.session_state.stream_logs
                ])
                st.download_button(
                    "Download Logs",
                    log_text,
                    file_name=f"stream_logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
                    mime="text/plain"
                )
    
    # Display logs
    if st.session_state.stream_logs:
        # Filter logs
        filtered_logs = st.session_state.stream_logs
        if log_level_filter != "ALL":
            filtered_logs = [log for log in filtered_logs if log['level'] == log_level_filter]
        
        # Show recent logs (last 100)
        recent_logs = filtered_logs[-100:] if len(filtered_logs) > 100 else filtered_logs
        
        # Create log display
        log_container = st.container()
        with log_container:
            for log in recent_logs:
                timestamp = log['timestamp'].strftime('%H:%M:%S')
                level = log['level']
                message = log['message']
                
                # Color coding
                if level == 'ERROR':
                    st.error(f"[{timestamp}] {message}")
                elif level == 'WARNING':
                    st.warning(f"[{timestamp}] {message}")
                elif level == 'INFO':
                    st.info(f"[{timestamp}] {message}")
                else:
                    st.text(f"[{timestamp}] {level}: {message}")
    else:
        st.info("No logs available. Start streaming to see logs.")

def show_file_manager():
    """File Manager Interface"""
    st.header("📁 File Manager")
    
    # Use the correct current directory
    current_dir = "/mount/src/liveyt8"
    
    # Ensure directory exists
    if not os.path.exists(current_dir):
        try:
            os.makedirs(current_dir, exist_ok=True)
            log_message('INFO', f"Created directory: {current_dir}")
        except Exception as e:
            st.error(f"Failed to create directory: {e}")
            log_message('ERROR', f"Failed to create directory {current_dir}: {e}")
            return
    
    st.subheader(f"📂 Current Directory: {current_dir}")
    
    # File upload
    uploaded_files = st.file_uploader(
        "📤 Upload Video Files",
        type=['mp4', 'avi', 'mov', 'mkv', 'flv', 'webm', 'm4v'],
        accept_multiple_files=True
    )
    
    if uploaded_files:
        for uploaded_file in uploaded_files:
            file_path = os.path.join(current_dir, uploaded_file.name)
            try:
                # Check if file already exists
                if os.path.exists(file_path):
                    st.warning(f"⚠️ File {uploaded_file.name} already exists. Overwriting...")
                
                with open(file_path, "wb") as f:
                    f.write(uploaded_file.getbuffer())
                st.success(f"✅ Uploaded: {uploaded_file.name}")
                log_message('INFO', f"File uploaded: {uploaded_file.name}")
                
                # Refresh the page to show the new file
                time.sleep(1)
                st.rerun()
                
            except Exception as e:
                st.error(f"❌ Failed to upload {uploaded_file.name}: {e}")
                log_message('ERROR', f"File upload failed: {uploaded_file.name} - {e}")
    
    # Video files list
    st.subheader("🎬 Video Files")
    video_files = get_video_files(current_dir)
    
    if video_files:
        for video in video_files:
            with st.expander(f"📹 {video['name']} ({video['size_mb']} MB)"):
                col1, col2 = st.columns([2, 1])
                
                with col1:
                    st.write(f"**Path:** {video['path']}")
                    st.write(f"**Size:** {video['size_mb']} MB")
                    
                    # Get video info
                    video_info = get_video_info(video['path'])
                    if video_info and 'streams' in video_info:
                        for stream in video_info['streams']:
                            if stream.get('codec_type') == 'video':
                                width = stream.get('width', 'Unknown')
                                height = stream.get('height', 'Unknown')
                                fps = stream.get('r_frame_rate', 'Unknown')
                                if fps != 'Unknown' and '/' in str(fps):
                                    try:
                                        num, den = map(int, fps.split('/'))
                                        fps = round(num / den, 2) if den != 0 else 'Unknown'
                                    except:
                                        pass
                                st.write(f"**Resolution:** {width}x{height}")
                                st.write(f"**FPS:** {fps}")
                                break
                    
                    if video_info and 'format' in video_info:
                        duration = video_info['format'].get('duration', 'Unknown')
                        if duration != 'Unknown':
                            try:
                                duration_sec = float(duration)
                                minutes = int(duration_sec // 60)
                                seconds = int(duration_sec % 60)
                                st.write(f"**Duration:** {minutes}:{seconds:02d}")
                            except:
                                st.write(f"**Duration:** {duration}")
                
                with col2:
                    if st.button(f"🗑️ Delete", key=f"delete_{video['name']}"):
                        try:
                            os.remove(video['path'])
                            st.success(f"Deleted: {video['name']}")
                            log_message('INFO', f"File deleted: {video['name']}")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Failed to delete: {e}")
                            log_message('ERROR', f"File deletion failed: {video['name']} - {e}")
    else:
        st.info("No video files found. Upload some videos to get started!")

def show_video_merger():
    """Video Merger Interface"""
    st.header("🔗 Video Merger")
    
    video_files = get_video_files()
    if len(video_files) < 2:
        st.warning("You need at least 2 video files to merge. Please upload more videos.")
        return
    
    st.subheader("Select Videos to Merge")
    
    # Multi-select for videos
    selected_videos = st.multiselect(
        "Choose videos to merge (in order)",
        options=video_files,
        format_func=lambda x: f"{x['name']} ({x['size_mb']} MB)"
    )
    
    if len(selected_videos) < 2:
        st.info("Please select at least 2 videos to merge.")
        return
    
    # Output filename
    output_name = st.text_input(
        "Output filename",
        value="merged_video.mp4",
        help="Enter the name for the merged video file"
    )
    
    # Merge options
    col1, col2 = st.columns(2)
    
    with col1:
        merge_method = st.selectbox(
            "Merge Method",
            ["Concatenate", "Side by Side", "Top and Bottom"],
            help="Choose how to combine the videos"
        )
    
    with col2:
        output_resolution = st.selectbox(
            "Output Resolution",
            ["Original", "1920x1080", "1280x720", "854x480"],
            help="Resolution for the merged video"
        )
    
    # Merge button
    if st.button("🔗 Merge Videos", type="primary"):
        if not output_name.endswith('.mp4'):
            output_name += '.mp4'
        
        output_path = os.path.join("/mount/src/liveyt8", output_name)
        
        with st.spinner("Merging videos... This may take a while."):
            success, message = merge_videos(selected_videos, output_path, merge_method, output_resolution)
            
            if success:
                st.success(f"✅ Videos merged successfully: {output_name}")
                log_message('INFO', f"Videos merged: {output_name}")
                st.rerun()
            else:
                st.error(f"❌ Failed to merge videos: {message}")
                log_message('ERROR', f"Video merge failed: {message}")

def merge_videos(video_list, output_path, method, resolution):
    """Merge videos using FFmpeg"""
    try:
        if method == "Concatenate":
            # Create temporary file list
            filelist_path = "/tmp/filelist.txt"
            with open(filelist_path, 'w') as f:
                for video in video_list:
                    f.write(f"file '{video['path']}'\n")
            
            cmd = [
                'ffmpeg', '-y',
                '-f', 'concat',
                '-safe', '0',
                '-i', filelist_path,
                '-c', 'copy'
            ]
            
            if resolution != "Original":
                width, height = resolution.split('x')
                cmd.extend(['-vf', f'scale={width}:{height}'])
            
            cmd.append(output_path)
            
        elif method == "Side by Side":
            if len(video_list) != 2:
                return False, "Side by Side merge requires exactly 2 videos"
            
            cmd = [
                'ffmpeg', '-y',
                '-i', video_list[0]['path'],
                '-i', video_list[1]['path'],
                '-filter_complex', '[0:v][1:v]hstack=inputs=2[v]',
                '-map', '[v]',
                '-map', '0:a',
                '-c:a', 'copy'
            ]
            
            if resolution != "Original":
                width, height = resolution.split('x')
                cmd.extend(['-s', f'{width}x{height}'])
            
            cmd.append(output_path)
            
        elif method == "Top and Bottom":
            if len(video_list) != 2:
                return False, "Top and Bottom merge requires exactly 2 videos"
            
            cmd = [
                'ffmpeg', '-y',
                '-i', video_list[0]['path'],
                '-i', video_list[1]['path'],
                '-filter_complex', '[0:v][1:v]vstack=inputs=2[v]',
                '-map', '[v]',
                '-map', '0:a',
                '-c:a', 'copy'
            ]
            
            if resolution != "Original":
                width, height = resolution.split('x')
                cmd.extend(['-s', f'{width}x{height}'])
            
            cmd.append(output_path)
        
        # Execute FFmpeg command
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)  # 1 hour timeout
        
        if result.returncode == 0:
            return True, "Videos merged successfully"
        else:
            return False, f"FFmpeg error: {result.stderr}"
            
    except subprocess.TimeoutExpired:
        return False, "Merge operation timed out"
    except Exception as e:
        return False, str(e)
    finally:
        # Clean up temporary files
        try:
            if os.path.exists("/tmp/filelist.txt"):
                os.remove("/tmp/filelist.txt")
        except:
            pass

def show_analytics():
    """Analytics Dashboard"""
    st.header("📈 Analytics Dashboard")
    
    try:
        with get_db_connection() as conn:
            # Stream history
            history_df = pd.read_sql_query('''
                SELECT * FROM stream_history 
                ORDER BY start_time DESC
            ''', conn)
        
        if not history_df.empty:
            # Summary metrics
            col1, col2, col3, col4 = st.columns(4)
            
            with col1:
                total_streams = len(history_df)
                st.metric("📊 Total Streams", total_streams)
            
            with col2:
                successful_streams = len(history_df[history_df['status'] == 'STOPPED'])
                success_rate = (successful_streams / total_streams * 100) if total_streams > 0 else 0
                st.metric("✅ Success Rate", f"{success_rate:.1f}%")
            
            with col3:
                total_duration = history_df['duration'].fillna(0).sum()
                hours = int(total_duration // 3600)
                minutes = int((total_duration % 3600) // 60)
                st.metric("⏱️ Total Duration", f"{hours}h {minutes}m")
            
            with col4:
                avg_bitrate = history_df['bitrate'].mean()
                st.metric("📡 Avg Bitrate", f"{avg_bitrate:.0f} kbps")
            
            # Recent streams table
            st.subheader("📋 Recent Streams")
            display_df = history_df.copy()
            display_df['video_file'] = display_df['video_file'].apply(lambda x: os.path.basename(x) if pd.notna(x) else '')
            display_df['duration_formatted'] = display_df['duration'].apply(
                lambda x: f"{int(x//60)}:{int(x%60):02d}" if pd.notna(x) else "N/A"
            )
            
            st.dataframe(
                display_df[['start_time', 'video_file', 'resolution', 'bitrate', 'duration_formatted', 'status']],
                column_config={
                    'start_time': 'Start Time',
                    'video_file': 'Video File',
                    'resolution': 'Resolution',
                    'bitrate': 'Bitrate (kbps)',
                    'duration_formatted': 'Duration',
                    'status': 'Status'
                },
                use_container_width=True
            )
            
        else:
            st.info("No streaming history available yet. Start streaming to see analytics!")
        
    except Exception as e:
        st.error(f"Error loading analytics: {e}")
        log_message('ERROR', f"Analytics error: {e}")

def show_settings():
    """Settings Interface"""
    st.header("⚙️ Settings")
    
    # System Information
    st.subheader("💻 System Information")
    
    col1, col2 = st.columns(2)
    
    with col1:
        ffmpeg_status = "✅ Available" if check_ffmpeg() else "❌ Not Found"
        st.info(f"**FFmpeg Status:** {ffmpeg_status}")
        
        # Count configurations
        configs_df = load_configurations()
        st.info(f"**Saved Configurations:** {len(configs_df)}")
    
    with col2:
        current_dir = "/mount/src/liveyt9"
        # Use correct directory
        current_dir = "/mount/src/liveyt8"
        st.info(f"**Current Directory:** {current_dir}")
        
        # Count stream history
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT COUNT(*) FROM stream_history')
                history_count = cursor.fetchone()[0]
            st.info(f"**Stream History Records:** {history_count}")
        except:
            st.info("**Stream History Records:** 0")
    
    st.info(f"**Database:** streaming_app.db")
    
    # Application Settings
    st.subheader("🔧 Application Settings")
    
    auto_refresh = st.checkbox(
        "Auto-refresh statistics",
        value=True,
        help="Automatically refresh streaming statistics"
    )
    
    # Default Settings
    st.subheader("📋 Default Settings")
    
    col1, col2 = st.columns(2)
    
    with col1:
        default_bitrate = st.slider(
            "Default Bitrate (kbps)",
            min_value=500,
            max_value=8000,
            value=2500,
            step=100
        )
        
        default_resolution = st.selectbox(
            "Default Resolution",
            ["Original", "1920x1080", "1280x720", "854x480", "640x360"],
            index=0
        )
    
    with col2:
        default_audio_bitrate = st.selectbox(
            "Default Audio Bitrate (kbps)",
            [64, 96, 128, 160, 192, 256],
            index=2
        )
        
        default_preset = st.selectbox(
            "Default Encoding Preset",
            ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"],
            index=2
        )
    
    # Database Management
    st.subheader("🗄️ Database Management")
    
    col1, col2, col3 = st.columns(3)
    
    with col1:
        if st.button("🗑️ Clear Stream History"):
            try:
                with get_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute('DELETE FROM stream_history')
                    cursor.execute('DELETE FROM stream_logs')
                    conn.commit()
                
                # Clear session state logs too
                st.session_state.stream_logs = []
                
                st.success("Stream history cleared!")
                log_message('INFO', "Stream history cleared by user")
            except Exception as e:
                st.error(f"Failed to clear history: {e}")
    
    with col2:
        if st.button("🔄 Reset Database"):
            try:
                with get_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute('DROP TABLE IF EXISTS stream_configs')
                    cursor.execute('DROP TABLE IF EXISTS stream_history')
                    cursor.execute('DROP TABLE IF EXISTS app_settings')
                    cursor.execute('DROP TABLE IF EXISTS stream_logs')
                    conn.commit()
                init_database()
                
                # Clear session state
                st.session_state.stream_logs = []
                st.session_state.current_session_id = str(uuid.uuid4())
                
                st.success("Database reset successfully!")
                log_message('INFO', "Database reset by user")
            except Exception as e:
                st.error(f"Failed to reset database: {e}")
    
    with col3:
        # Export configurations
        configs_df = load_configurations()
        if not configs_df.empty:
            csv_data = configs_df.to_csv(index=False)
            st.download_button(
                "📥 Export Configs",
                csv_data,
                file_name=f"stream_configs_{datetime.now().strftime('%Y%m%d')}.csv",
                mime="text/csv"
            )

def main():
    """Main application"""
    st.set_page_config(
        page_title="🚀 Advanced YouTube Live Streamer Pro",
        page_icon="🚀",
        layout="wide",
        initial_sidebar_state="expanded"
    )
    
    # Custom CSS for better styling
    st.markdown("""
    <style>
    .main-header {
        text-align: center;
        padding: 1rem 0;
        background: linear-gradient(90deg, #ff6b6b, #4ecdc4);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        background-clip: text;
        font-size: 2.5rem;
        font-weight: bold;
        margin-bottom: 2rem;
    }
    
    .status-online {
        color: #28a745;
        font-weight: bold;
    }
    
    .status-offline {
        color: #dc3545;
        font-weight: bold;
    }
    
    .metric-card {
        background: #f8f9fa;
        padding: 1rem;
        border-radius: 0.5rem;
        border-left: 4px solid #007bff;
    }
    </style>
    """, unsafe_allow_html=True)
    
    # Header
    st.markdown('<h1 class="main-header">🚀 Advanced YouTube Live Streamer Pro</h1>', unsafe_allow_html=True)
    
    # Sidebar
    with st.sidebar:
        st.markdown("## 🎛️ Control Panel")
        
        # Status indicator
        if st.session_state.streaming_active:
            st.markdown('<p class="status-online">🔴 ONLINE</p>', unsafe_allow_html=True)
        else:
            st.markdown('<p class="status-offline">⚫ OFFLINE</p>', unsafe_allow_html=True)
        
        # Auto-refresh toggle
        auto_refresh = st.checkbox("🔄 Auto Refresh (5s)", value=True)
        if auto_refresh and st.session_state.streaming_active:
            time.sleep(5)
            st.rerun()
        
        st.markdown("---")
        
        # Navigation
        st.markdown("### Select Page")
        page = st.selectbox(
            "Choose a page:",
            ["🎥 Stream Control", "📁 File Manager", "🔗 Video Merger", "📈 Analytics", "⚙️ Settings"],
            key="page_selector"
        )
    
    # Main content
    if page == "🎥 Stream Control":
        show_stream_control()
        
        # Show live logs if streaming
        if st.session_state.streaming_active:
            st.markdown("---")
            show_live_logs()
            
    elif page == "📁 File Manager":
        show_file_manager()
        
    elif page == "🔗 Video Merger":
        show_video_merger()
        
    elif page == "📈 Analytics":
        show_analytics()
        
    elif page == "⚙️ Settings":
        show_settings()

if __name__ == "__main__":
    main()
