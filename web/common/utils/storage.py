from flask import current_app
import tempfile
import os
from aiohttp import ClientResponse
import time
from common.settings import CHUNK_SIZE


async def upload_file_by_chunks(stream: ClientResponse, downloading_speed):
    with tempfile.NamedTemporaryFile(delete=False) as temp:
        start_time = time.monotonic()
        total_bytes_downloaded = 0

        while True:
            chunk = await stream.content.read(CHUNK_SIZE)
            if not chunk:
                break
            temp.write(chunk)

            total_bytes_downloaded += len(chunk)

            elapsed_time = time.monotonic() - start_time
            expected_time = total_bytes_downloaded / (downloading_speed * 1024 * 1024)
            if elapsed_time < expected_time:
                time.sleep(expected_time - elapsed_time)
        temp.seek(0)
        temp.close()
        os.unlink(temp.name)

    actual_download_speed = total_bytes_downloaded / (1024 * 1024 * elapsed_time)
    current_app.logger.info(f'downloading speed object: {actual_download_speed}mbps')


def upload_file(file, downloading_speed):
    with tempfile.NamedTemporaryFile(delete=False) as temp:
        start_time = time.monotonic()
        total_bytes_downloaded = 0
        while True:
            chunk = file.read(CHUNK_SIZE)
            if not chunk:
                break
            temp.write(chunk)

            total_bytes_downloaded += len(chunk)

            elapsed_time = time.monotonic() - start_time
            expected_time = total_bytes_downloaded / (downloading_speed * 1024 * 1024)
            if elapsed_time < expected_time:
                time.sleep(expected_time - elapsed_time)
        temp.seek(0)
        temp.close()
        os.unlink(temp.name)
