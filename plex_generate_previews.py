#!/usr/bin/env python3

import sys
import re
import subprocess
import shutil
import glob
import os
import struct
import urllib3
import array
import time
import math

import gpustat
import requests

from datetime import timedelta
from concurrent.futures import ProcessPoolExecutor

from dotenv import load_dotenv
from loguru import logger
from pymediainfo import MediaInfo
from plexapi.server import PlexServer
from rich.console import Console
from rich.progress import Progress, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn, SpinnerColumn, MofNCompleteColumn

load_dotenv()

# Plex server URL. Can also use for local server: http://localhost:32400
PLEX_URL = os.environ.get('PLEX_URL', '')

# Plex Authentication Token
PLEX_TOKEN = os.environ.get('PLEX_TOKEN', '')

# Interval between preview images
PLEX_BIF_FRAME_INTERVAL = int(os.environ.get('PLEX_BIF_FRAME_INTERVAL', 5))

# Preview image quality (2-6)
THUMBNAIL_QUALITY = int(os.environ.get('THUMBNAIL_QUALITY', 4))

# Local Plex media path
PLEX_LOCAL_MEDIA_PATH = os.environ.get('PLEX_LOCAL_MEDIA_PATH', '/path_to/plex/Library/Application Support/Plex Media Server/Media')

# Temporary folder for preview generation
TMP_FOLDER = os.environ.get('TMP_FOLDER', '/dev/shm/plex_generate_previews')

# Timeout for Plex API requests (seconds)
PLEX_TIMEOUT = int(os.environ.get('PLEX_TIMEOUT', 60))

# Path mappings for remote preview generation.
# So you can have another computer generate previews for your Plex server
# If you are running on your plex server, you can set both variables to ''

# Local video path for the script
PLEX_LOCAL_VIDEOS_PATH_MAPPING = os.environ.get('PLEX_LOCAL_VIDEOS_PATH_MAPPING', '')

# Plex server video path
PLEX_VIDEOS_PATH_MAPPING = os.environ.get('PLEX_VIDEOS_PATH_MAPPING', '')

# Number of GPU threads for preview generation
GPU_THREADS = int(os.environ.get('GPU_THREADS', 4))

# Number of CPU threads for preview generation
CPU_THREADS = int(os.environ.get('CPU_THREADS', 4))

# Set the timeout envvar for https://github.com/pkkid/python-plexapi
os.environ["PLEXAPI_PLEXAPI_TIMEOUT"] = str(PLEX_TIMEOUT)

if not shutil.which("mediainfo"):
    print('MediaInfo not found.  MediaInfo must be installed and available in PATH.')
    sys.exit(1)

FFMPEG_PATH = shutil.which("ffmpeg")
if not FFMPEG_PATH:
    print('FFmpeg not found.  FFmpeg must be installed and available in PATH.')
    sys.exit(1)

# Logging setup
console = Console()

logger.remove()

logger.add(
    lambda _: console.print(_, end=''),
    level='INFO',
    format='<green>{time:YYYY/MM/DD HH:mm:ss}</green> | {level.icon} - <level>{message}</level>',
    enqueue=True
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

GPU = None

def detect_gpu():
    # Check for NVIDIA GPUs
    try:
        import pynvml
        pynvml.nvmlInit()
        num_nvidia_gpus = pynvml.nvmlDeviceGetCount()
        pynvml.nvmlShutdown()
        if num_nvidia_gpus > 0:
            return 'NVIDIA'
    except ImportError:
        logger.warning("NVIDIA GPU detection library (pynvml) not found. NVIDIA GPUs will not be detected.")
    except pynvml.NVMLError as e:
        logger.warning(f"Error initializing NVIDIA GPU detection {e}. NVIDIA GPUs will not be detected.")

    # Check for AMD GPUs
    try:
        from amdsmi import amdsmi_interface
        amdsmi_interface.amdsmi_init()
        devices = amdsmi_interface.amdsmi_get_processor_handles()
        found = None
        if len(devices) > 0:
            for device in devices:
                processor_type = amdsmi_interface.amdsmi_get_processor_type(device)
                if processor_type == amdsmi_interface.AMDSMI_PROCESSOR_TYPE_GPU:
                    found = True
        amdsmi_interface.amdsmi_shut_down()
        if found:
                vaapi_device_dir = "/dev/dri"
                if os.path.exists(vaapi_device_dir):
                    for entry in os.listdir(vaapi_device_dir):
                        if entry.startswith("renderD"):
                            return os.path.join(vaapi_device_dir, entry)
    except ImportError:
        logger.warning("AMD GPU detection library (amdsmi) not found. AMD GPUs will not be detected.")
    except Exception as e:
        logger.warning(f"Error initializing AMD GPU detection: {e}. AMD GPUs will not be detected.")


def get_amd_ffmpeg_processes():
    from amdsmi import amdsmi_init, amdsmi_shut_down, amdsmi_get_processor_handles, amdsmi_get_gpu_process_list
    try:
        amdsmi_init()
        gpu_handles = amdsmi_get_processor_handles()
        ffmpeg_processes = []

        for gpu in gpu_handles:
            processes = amdsmi_get_gpu_process_list(gpu)
            for process in processes:
                if process['name'].lower().startswith('ffmpeg'):
                    ffmpeg_processes.append(process)

        return ffmpeg_processes
    finally:
        amdsmi_shut_down()

def human_readable_size(size_bytes):
    """Convert size in bytes to human-readable format"""
    if size_bytes == 0:
        return "0 B"
    size_name = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB", "ZiB", "YiB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"

def format_time(seconds):
    """Convert seconds to HH:MM:SS format"""
    return str(timedelta(seconds=int(seconds)))

def generate_images(video_file_param, output_folder):
    video_file = video_file_param.replace(PLEX_VIDEOS_PATH_MAPPING, PLEX_LOCAL_VIDEOS_PATH_MAPPING)
    media_info = MediaInfo.parse(video_file)

    vf_parameters = (
        "fps=fps={}:round=up,"
        "scale=w=320:h=240:force_original_aspect_ratio=decrease"
        ).format(round(1 / PLEX_BIF_FRAME_INTERVAL, 6))

    # Check if we have a HDR Format
    if media_info.video_tracks:
        if media_info.video_tracks[0].hdr_format != "None" and media_info.video_tracks[0].hdr_format is not None:
            vf_parameters = (
                "fps=fps={}:round=up,"
                "zscale=t=linear:npl=100,"
                "format=gbrpf32le,"
                "zscale=p=bt709,"
                "tonemap=tonemap=hable:desat=0,"
                "zscale=t=bt709:m=bt709:r=tv,"
                "format=yuv420p,"
                "scale=w=320:h=240:force_original_aspect_ratio=decrease"
            ).format(round(1 / PLEX_BIF_FRAME_INTERVAL, 6))

    args = [
        FFMPEG_PATH, "-loglevel", "info", "-skip_frame:v", "nokey", "-threads:0", "1",
        "-i", video_file, "-an", "-sn", "-dn", "-q:v", str(THUMBNAIL_QUALITY),
        "-vf", vf_parameters, '{}/img-%06d.jpg'.format(output_folder)
    ]

    start = time.time()
    hw = False

    if GPU == 'NVIDIA':
        gpu_stats_query = gpustat.core.new_query()
        gpu = gpu_stats_query[0] if gpu_stats_query else None
        if gpu:
            gpu_ffmpeg = [c for c in gpu.processes if c["command"].lower().startswith("ffmpeg")]
            if len(gpu_ffmpeg) < GPU_THREADS or CPU_THREADS == 0:
                hw = True
                args.insert(5, "-hwaccel")
                args.insert(6, "cuda")
    elif GPU:
        # Must be AMD
        gpu_ffmpeg = get_amd_ffmpeg_processes()
        if len(gpu_ffmpeg) < GPU_THREADS or CPU_THREADS == 0:
            hw = True
            args.insert(5, "-hwaccel")
            args.insert(6, "vaapi")
            args.insert(7, "-vaapi_device")
            args.insert(8, GPU)
            # Adjust vf_parameters for AMD VAAPI
            vf_parameters = vf_parameters.replace("scale=w=320:h=240:force_original_aspect_ratio=decrease", "format=nv12|vaapi,hwupload,scale_vaapi=w=320:h=240:force_original_aspect_ratio=decrease")
            args[args.index("-vf") + 1] = vf_parameters

    # Get video length
    video_track = next((track for track in media_info.tracks if track.track_type == "Video"), None)
    if video_track and video_track.duration is not None:
        video_length = float(video_track.duration) / 1000  # Convert ms to seconds
        video_length_formatted = format_time(video_length)
        total_expected_thumbnails = round(video_length / PLEX_BIF_FRAME_INTERVAL)
    else:
        video_length = 0
        video_length_formatted = "00:00:00"  # Set to 00:00:00 if duration can't be determined
        total_expected_thumbnails = 0

    file_size = os.path.getsize(video_file)
    file_size_human = human_readable_size(file_size)

    logger.info(f"Generating thumbnails for [magenta]{video_file}[/magenta] ({video_length_formatted}, {file_size_human}): HW={hw}")

    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

    last_progress = 0
    for line in proc.stderr:
        time_match = re.search(r'time=(\d{2}):(\d{2}):(\d{2}.\d{2})', line)
        speed_match = re.search(r'speed=\s*([\d.]+)x', line)

        if time_match and speed_match:
            hours, minutes, seconds = map(float, time_match.groups())
            current_time = hours * 3600 + minutes * 60 + seconds
            speed_multiple = float(speed_match.group(1))

            if video_length > 0:
                progress_percentage = min((current_time / video_length) * 100, 100)
                thumbnails_generated = int(current_time / PLEX_BIF_FRAME_INTERVAL)

                # Only log every 1% progress to avoid cluttering the output
                if int(progress_percentage) - last_progress >= 1:
                    logger.info(f"[magenta]{video_file}[/magenta]: "
                                f"[bold yellow]{int(progress_percentage)}[/]% | "
                                f"[bold green]{thumbnails_generated}/{total_expected_thumbnails}[/] thumbnails "
                                f"@ [bold blue]{speed_multiple}x[/] speed "
                                f"(HW={hw})")
                    last_progress = int(progress_percentage)

    # Compute speed
    end = time.time()
    processing_time = end - start
    if video_length > 0:
        speed = video_length / processing_time
    else:
        speed = 0

    # Optimize and Rename Images
    for image in glob.glob('{}/img*.jpg'.format(output_folder)):
        frame_no = int(os.path.basename(image).strip('-img').strip('.jpg')) - 1
        frame_second = frame_no * PLEX_BIF_FRAME_INTERVAL
        os.rename(image, os.path.join(output_folder, '{:010d}.jpg'.format(frame_second)))

    logger.info(
        f"Generated [bold green]{total_expected_thumbnails}[/] thumbnails "
        f"for [magenta]{video_file}[/]: "
        f"took [bold green]{round(processing_time)}[/] seconds "
        f"@ {speed}x speed (HW={HW})"
    )

def generate_bif(bif_filename, images_path):
    """
    Build a .bif file
    @param bif_filename name of .bif file to create
    @param images_path Directory of image files 00000001.jpg
    """
    magic = [0x89, 0x42, 0x49, 0x46, 0x0d, 0x0a, 0x1a, 0x0a]
    version = 0

    images = [img for img in os.listdir(images_path) if os.path.splitext(img)[1] == '.jpg']
    images.sort()

    f = open(bif_filename, "wb")
    array.array('B', magic).tofile(f)
    f.write(struct.pack("<I", version))
    f.write(struct.pack("<I", len(images)))
    f.write(struct.pack("<I", 1000 * PLEX_BIF_FRAME_INTERVAL))
    array.array('B', [0x00 for x in range(20, 64)]).tofile(f)

    bif_table_size = 8 + (8 * len(images))
    image_index = 64 + bif_table_size
    timestamp = 0

    # Get the length of each image
    for image in images:
        statinfo = os.stat(os.path.join(images_path, image))
        f.write(struct.pack("<I", timestamp))
        f.write(struct.pack("<I", image_index))
        timestamp += 1
        image_index += statinfo.st_size

    f.write(struct.pack("<I", 0xffffffff))
    f.write(struct.pack("<I", image_index))

    # Now copy the images
    for image in images:
        data = open(os.path.join(images_path, image), "rb").read()
        f.write(data)

    f.close()


def process_item(item_key):
    sess = requests.Session()
    sess.verify = False
    plex = PlexServer(PLEX_URL, PLEX_TOKEN, timeout=PLEX_TIMEOUT, session=sess)

    data = plex.query('{}/tree'.format(item_key))

    for media_part in data.findall('.//MediaPart'):
        if 'hash' in media_part.attrib:
            # Filter Processing by HDD Path
            if len(sys.argv) > 1:
                if sys.argv[1] not in media_part.attrib['file']:
                    return
            bundle_hash = media_part.attrib['hash']
            media_file = media_part.attrib['file']

            if not os.path.isfile(media_file):
                logger.error('Skipping as file not found {}'.format(media_file))
                continue

            try:
                bundle_file = '{}/{}{}'.format(bundle_hash[0], bundle_hash[1::1], '.bundle')
            except Exception as e:
                logger.error('Error generating bundle_file for {} due to {}:{}'.format(media_file, type(e).__name__, str(e)))
                continue

            bundle_path = os.path.join(PLEX_LOCAL_MEDIA_PATH, 'localhost', bundle_file)
            indexes_path = os.path.join(bundle_path, 'Contents', 'Indexes')
            index_bif = os.path.join(indexes_path, 'index-sd.bif')
            tmp_path = os.path.join(TMP_FOLDER, bundle_hash)
            if not os.path.isfile(index_bif):
                if not os.path.isdir(indexes_path):
                    try:
                        os.makedirs(indexes_path)
                    except OSError as e:
                        logger.error('Error generating images for {}. `{}:{}` error when creating index path {}'.format(media_file, type(e).__name__, str(e), indexes_path))
                        continue

                try:
                    if not os.path.isdir(tmp_path):
                        os.makedirs(tmp_path)
                except OSError as e:
                    logger.error('Error generating images for {}. `{}:{}` error when creating tmp path {}'.format(media_file, type(e).__name__, str(e), tmp_path))
                    continue

                try:
                    generate_images(media_part.attrib['file'], tmp_path)
                except Exception as e:
                    logger.error('Error generating images for {}. `{}: {}` error when generating images'.format(media_file, type(e).__name__, str(e)))
                    if os.path.exists(tmp_path):
                        shutil.rmtree(tmp_path)
                    continue

                try:
                    generate_bif(index_bif, tmp_path)
                except Exception as e:
                    # Remove bif, as it prob failed to generate
                    if os.path.exists(index_bif):
                        os.remove(index_bif)
                    logger.error('Error generating images for {}. `{}:{}` error when generating bif'.format(media_file, type(e).__name__, str(e)))
                    continue
                finally:
                    if os.path.exists(tmp_path):
                        shutil.rmtree(tmp_path)


def run():
    # Ignore SSL Errors
    sess = requests.Session()
    sess.verify = False

    plex = PlexServer(PLEX_URL, PLEX_TOKEN, session=sess)

    for section in plex.library.sections():
        logger.info('Getting the media files from library \'{}\''.format(section.title))

        if section.METADATA_TYPE == 'episode':
            media = [m.key for m in section.search(libtype='episode')]
        elif section.METADATA_TYPE == 'movie':
            media = [m.key for m in section.search()]
        else:
            logger.info('Skipping library {} as \'{}\' is unsupported'.format(section.title, section.METADATA_TYPE))
            continue

        logger.info('Got {} media files for library {}'.format(len(media), section.title))

        with Progress(SpinnerColumn(), *Progress.get_default_columns(), MofNCompleteColumn(), console=console) as progress:
            with ProcessPoolExecutor(max_workers=CPU_THREADS + GPU_THREADS) as process_pool:
                futures = [process_pool.submit(process_item, key) for key in media]
                for future in progress.track(futures):
                    future.result()


if __name__ == '__main__':
    logger.info('GPU Detection (with AMD Support) was recently added to this script.')
    logger.info('Please log issues here https://github.com/stevezau/plex_generate_vid_previews/issues')

    if not os.path.exists(PLEX_LOCAL_MEDIA_PATH):
        logger.error(
            '%s does not exist, please edit PLEX_LOCAL_MEDIA_PATH environment variable' % PLEX_LOCAL_MEDIA_PATH)
        exit(1)

    if not os.path.exists(os.path.join(PLEX_LOCAL_MEDIA_PATH, 'localhost')):
        logger.error(
            'You set PLEX_LOCAL_MEDIA_PATH to "%s". There should be a folder called "localhost" in that directory but it does not exist which suggests you haven\'t mapped it correctly. Please fix the PLEX_LOCAL_MEDIA_PATH environment variable' % PLEX_LOCAL_MEDIA_PATH)
        exit(1)

    if PLEX_URL == '':
        logger.error('Please set the PLEX_URL environment variable')
        exit(1)

    if PLEX_TOKEN == '':
        logger.error('Please set the PLEX_TOKEN environment variable')
        exit(1)

    # detect GPU's
    GPU = detect_gpu()
    if GPU == 'NVIDIA':
        logger.info('Found NVIDIA GPU')
    elif GPU:
        logger.info(f'Found AMD GPU {GPU}')
    if not GPU:
        logger.warning('No GPUs detected. Defaulting to CPU ONLY.')
        logger.warning('If you think this is an error please log an issue here https://github.com/stevezau/plex_generate_vid_previews/issues')

    try:
        # Clean TMP Folder
        if os.path.isdir(TMP_FOLDER):
            shutil.rmtree(TMP_FOLDER)
        os.makedirs(TMP_FOLDER)
        run()
    finally:
        if os.path.isdir(TMP_FOLDER):
            shutil.rmtree(TMP_FOLDER)
