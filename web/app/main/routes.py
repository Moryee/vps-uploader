from flask import render_template, request, make_response, current_app
from app.main import bp
import tempfile
import os
from aiohttp import ClientSession, ClientResponse
import time
import asyncio
import boto3
from app.models.test import Test
from tldextract import extract
import socket
from flask_sse import sse
import uuid
from celery import shared_task


CHUNK_SIZE = 1024 * 1024 * 1
DOWNLOADING_SPEED = 100 * 1024 * 1024 / 8


api_upload_url_test_endpoint = '/api/upload-url-test'
api_host_test = '/api/host-test'

api_upload_url_endpoint = '/api/upload-url'
api_upload_file_endpoint = '/api/upload-file'
api_upload_tebi_endpoint = '/api/upload-tebi'
api_test_download_speed_endpoint = '/api/test-download-speed'


# Helper functions


class UploadStatus:
    '''
    status structure:
        {
            "file_size": float kb,
            "file_ip": str,
            "tebi_status": int,
            "tebi_servers": "DE:1,USE:1,USW:2,SGP:1",
            "ok": int,
            "failed": int,
            "finished": bool,
            "vps": {
                "vps_name_1": {
                    "ip": "10.10.10.10",
                    "tebi": {
                        "ip": "100.100.100.100",
                        "status": "1",
                        "latency": "10.234",
                        "ttfb": "123.456",
                        "time": "1234.765"
                    },
                    "object": {
                        "ip": "100.100.100.100",
                        "status": "2",
                        "latency": "12.543",
                        "ttfb": "123.654",
                        "time": "123.456"
                    }
                }
            }
        }

    tebi_status:
        0 - waiting
        1 - uploading file
        2 - waiting for replication
        3 - replication completed

    test_status:
        0 - waiting
        1 - speed test started
        2 - speed test completed
    '''

    def __init__(self, uuid) -> None:
        self._uuid = str(uuid)

        self._file_ip = None
        self._file_size = None
        self._tebi_status = None
        self._tebi_servers = None

        self._ok = 0
        self._failed = 0
        self._vps = {}

        for vps_name, vps_url in current_app.config['VPS_URLS'].items():
            self._vps[vps_name] = {
                'ip': get_ip_from_url(vps_url),
                'tebi': {
                    'status': 0
                },
                'object': {
                    'status': 0
                }
            }

        self._finished = False
        self._error_message = None

    @property
    def file_ip(self):
        return self._file_ip

    @file_ip.setter
    def file_ip(self, value):
        self._file_ip = value

    @property
    def file_size(self):
        return self._file_size

    @file_size.setter
    def file_size(self, value):
        self._file_size = value

    @property
    def tebi_status(self):
        return self._tebi_status

    @tebi_status.setter
    def tebi_status(self, value):
        if value not in [0, 1, 2, 3]:
            raise ValueError('status must be either 0, 1, 2, or 3')
        self._tebi_status = value

        self._make_announcement()

    @property
    def tebi_servers(self):
        return self._tebi_servers

    @tebi_servers.setter
    def tebi_servers(self, value):
        self._tebi_servers = value

        self._make_announcement()

    @property
    def ok(self):
        return self._ok

    @ok.setter
    def ok(self, value):
        self._ok = value

    @property
    def failed(self):
        return self._failed

    @failed.setter
    def failed(self, value):
        self._failed = value

    def vps_update_status(self, vps_name: str, storage: str, status: int):
        '''update vps status

        Args:
            `vps_ip` (str): ip address of vps
            `storage` (str): 'tebi' or 'object'
            `status` (str): 0 - waiting, 1 - speed test started, 2 - speed test completed

        Raises:
            ValueError: _description_
        '''
        if storage not in ['tebi', 'object']:
            raise ValueError('storage must be either \'tebi\' or \'object\'')

        if vps_name not in self._vps.keys():
            self._vps[vps_name] = {}

        if storage not in self._vps[vps_name].keys():
            self._vps[vps_name][storage] = {}

        self._vps[vps_name][storage]['status'] = status

        self._make_announcement()

    def vps_complete_status(self, vps_name: str, storage: str, latency: float, ttfb: float, time: float):
        if storage not in ['tebi', 'object']:
            raise ValueError('storage must be either \'tebi\' or \'object\'')

        if vps_name not in self._vps.keys():
            raise ValueError(f'vps_name \'{vps_name}\' not found')

        self._vps[vps_name][storage] = {
            'status': 2,
            'latency': latency,
            'ttfb': ttfb,
            'time': time,
            'ok': True
        }

        self.ok += 1

        self._make_announcement()

    def vps_failed_status(self, vps_name: str, storage: str):
        if storage not in ['tebi', 'object']:
            raise ValueError('storage must be either \'tebi\' or \'object\'')

        if vps_name not in self._vps.keys():
            raise ValueError(f'vps_name \'{vps_name}\' not found')

        self.failed += 1

        self._vps[vps_name][storage] = {
            'ok': False
        }

    def get_status(self):
        output = {}

        output['finished'] = self._finished

        if self._error_message is not None:
            output['error'] = self._error_message
            return output

        if self._file_ip is not None:
            output['file_ip'] = self._file_ip

        if self._file_size is not None:
            output['file_size'] = self._file_size

        if self._tebi_status is not None:
            output['tebi_status'] = self._tebi_status

        if self._tebi_servers is not None:
            output['tebi_servers'] = self._tebi_servers

        output['ok'] = self._ok
        output['failed'] = self._failed
        output['vps'] = self._vps

        return output

    def _make_announcement(self):
        status = self.get_status()
        sse.publish(status, channel=self._uuid)

    def finished(self):
        self._finished = True
        self._make_announcement()

    def finished_with_exception(self, error_message):
        self._finished = True
        self._error_message = error_message
        self._make_announcement()


@shared_task(ignore_result=False)
def upload_url_task(url, channel_uuid):
    asyncio.run(upload_url(url, channel_uuid))


async def upload_url(url, channel_uuid):
    file_ip = get_ip_from_url(url)

    upload_status = UploadStatus(channel_uuid)
    upload_status.tebi_status = 0  # waiting

    try:
        file_name, file_size = await replicate_url(url, upload_status)
    except Exception as e:
        current_app.logger.error(e)
        upload_status.finished_with_exception('error while replicating url')

    upload_status.file_size = file_size
    upload_status.file_ip = file_ip

    try:
        await publish(url, file_name, upload_status)
    except Exception as e:
        current_app.logger.error(e)
        upload_status.finished_with_exception('error while uploading file to vps')


async def replicate_url(url, upload_status: UploadStatus):
    '''
    returns tuple `file_name`, `file_size`
    '''

    correct_replication_status = 'DE:1,SGP:1,USE:2,USW:1'
    file_name: str = time.strftime('%Y-%m-%d_%H-%M-%S_') + url.split('/')[-1]
    file_size = 0

    async with ClientSession() as session:
        async with session.get(url) as resp:
            if not resp.ok:
                current_app.logger.error(f'Couldn\'t get file from \'{url}\'. Response: {resp}')
                return {'error': f'Couldn\'t get file from \'{url}\'.'}, 400

            upload_status.tebi_status = 1  # uploading file

            downloaded_size = 0
            with tempfile.NamedTemporaryFile(delete=False) as temp:
                while True:
                    chunk = await resp.content.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    downloaded_size += len(chunk)
                    temp.write(chunk)
                temp.seek(0)

                temp.close()
                os.unlink(temp.name)

            with tempfile.NamedTemporaryFile(delete=False) as new_temp_file:
                file_size = downloaded_size / 1024
                new_temp_file.truncate(downloaded_size)

                with open(new_temp_file.name, 'rb') as f:
                    get_bucket().put_object(Key=file_name, Body=f)

                new_temp_file.close()
                os.unlink(new_temp_file.name)

    upload_status.tebi_status = 2  # waiting for replication

    s3_client = tebi_get_client()

    def is_all_replicated(replication_status):
        return replication_status == correct_replication_status

    replication_complete = False
    tries = 0
    while not replication_complete and tries < 20:
        try:
            resp = s3_client.head_object(Bucket=current_app.config['TEBI_BUCKET'], Key=file_name)
            replication_status = resp.get('ResponseMetadata', {}).get('HTTPHeaders', {}).get('x-tb-replication', None)

            if is_all_replicated(replication_status):
                replication_complete = True
                upload_status.tebi_status = 3  # replication complete
                upload_status.tebi_servers = replication_status

        except Exception as e:
            print(f"Error checking replication status: {e}")
            break

        if not replication_complete:
            tries += 1
            await asyncio.sleep(2)

    return file_name, file_size


async def publish(url, file_name, upload_status: UploadStatus):
    vps_urls: dict = current_app.config['VPS_URLS'].copy()

    async def make_post(session, vps_name, vps_url, endpoint, json, storage_type) -> tuple[ClientResponse, dict]:
        current_app.logger.info(f'{vps_name}, {storage_type}, starting')
        start_time = time.monotonic()
        upload_status.vps_update_status(vps_name, storage_type, 1)

        resp: ClientResponse
        async with session.post(f'{vps_url}{endpoint}', json=json) as resp:
            if not resp.ok:
                current_app.logger.error(f'Couldn\'t publish to host \'{vps_url}{endpoint}\'. Response: {resp.status} {resp.reason}')
                upload_status.vps_failed_status(vps_name, storage_type)
                return

            end_time = time.monotonic()

            ttfb_client = end_time - start_time
            ttfb_server = float(resp.headers['x-ttfb'])

            ttfb = round(ttfb_client, 2)
            latency = round(ttfb_client - ttfb_server, 2)

            current_app.logger.info(f'{vps_name}, {storage_type}, done')
            upload_status.vps_complete_status(vps_name, storage_type, latency * 1000, ttfb, (end_time - start_time) * 1000)

    async with ClientSession() as session:
        for vps_name, vps_url in vps_urls.items():
            try:
                await make_post(session, vps_name, vps_url, api_upload_url_endpoint, {'url': url}, 'object')
            except Exception as e:
                current_app.logger.error(e)
                upload_status.vps_failed_status(vps_name, 'object')

            try:
                await make_post(session, vps_name, vps_url, api_upload_tebi_endpoint, {'file_name': file_name}, 'tebi')
            except Exception as e:
                current_app.logger.error(e)
                upload_status.vps_failed_status(vps_name, 'tebi')

        for i in range(3):
            upload_status.finished()


async def upload_file(file, file_name):
    with tempfile.NamedTemporaryFile(delete=False) as temp:
        file.save(temp.name)

        # here can be saving file

        temp.close()
        os.unlink(temp.name)


async def upload_file_by_chunks(stream: ClientResponse, file_name):
    with tempfile.NamedTemporaryFile(delete=False) as temp:
        start_time = time.monotonic()
        while True:
            chunk = await stream.content.read(CHUNK_SIZE)
            if not chunk:
                break
            temp.write(chunk)
            time_elapsed = time.monotonic() - start_time
            expected_time = len(chunk) / DOWNLOADING_SPEED - time_elapsed
            if expected_time > 0:
                time.sleep(expected_time)
        temp.seek(0)

        # here can be saving file

        temp.close()
        os.unlink(temp.name)


def get_ip_from_url(url):
    ext = extract(url)

    if not ext.suffix:
        return ext.domain

    subdomain = ext.subdomain if ext.subdomain else 'www'
    return socket.gethostbyname(subdomain + '.' + ext.domain + '.' + ext.suffix)


def format_download_time(seconds):
    return round(seconds * 1000, 3)


def tebi_get_client():
    return boto3.client(
        service_name='s3',
        aws_access_key_id=current_app.config['TEBI_KEY'],
        aws_secret_access_key=current_app.config['TEBI_SECRET'],
        endpoint_url='https://s3.tebi.io'
    )


def get_bucket():
    return boto3.resource(
        service_name='s3',
        aws_access_key_id=current_app.config['TEBI_KEY'],
        aws_secret_access_key=current_app.config['TEBI_SECRET'],
        endpoint_url='https://s3.tebi.io'
    ).Bucket(current_app.config['TEBI_BUCKET'])


@bp.before_request
def before_request():
    request.start_time = time.time()


@bp.after_request
def after_request(response):
    ttfb = time.time() - request.start_time
    response.headers['X-TTFB'] = str(ttfb)
    return response


# Routes


@bp.route('/')
async def index():
    if not current_app.config['MAIN_HOST']:
        return make_response('This is not main server', 400)

    return render_template('index.html')


@bp.route('/tests')
async def tests():
    if not current_app.config['MAIN_HOST']:
        return make_response('This is not main server', 400)

    tests = Test.query.order_by(Test.datetime.desc()).all()
    return render_template('tests.html', tests=tests)


@bp.route(api_upload_url_test_endpoint, methods=['POST'])
async def api_upload_url_test():
    '''
    request example:
    {
        "url": "http://kyi.download.datapacket.com/10mb.bin",
    }
    '''
    if not current_app.config['MAIN_HOST']:
        return make_response({'error': 'This is not main server'}, 400)

    url = request.json.get('url')
    channel_uuid = str(uuid.uuid4())

    if not url:
        return make_response({'error': 'No url provided'}, 400)

    upload_url_task.delay(url, channel_uuid)

    main_host_url = current_app.config['MAIN_HOST_URL']
    if current_app.config['DEBUG']:
        return {'sse_stream_url': f'/stream?channel={channel_uuid}'}
    else:
        return {'sse_stream_url': f'{main_host_url}/stream?channel={channel_uuid}'}


@bp.route(api_upload_tebi_endpoint, methods=['POST'])
async def api_upload_tebi():
    '''
    request example: {'file_name': 'file_name.bin'}
    '''

    file_name = request.json.get('file_name')

    x1 = time.monotonic()
    with tempfile.NamedTemporaryFile(delete=False) as temp:
        start_time = time.monotonic()
        file_size = get_bucket().Object(file_name).content_length
        bytes_read = 0
        body = get_bucket().Object(file_name).get()['Body']

        while True:
            chunk = body.read(CHUNK_SIZE)
            if not chunk:
                break
            temp.write(chunk)
            bytes_read += len(chunk)
            time_elapsed = time.monotonic() - start_time
            expected_time = (bytes_read / file_size) * file_size / DOWNLOADING_SPEED - time_elapsed
            if expected_time > 0:
                time.sleep(expected_time)

        temp.close()
        os.unlink(temp.name)

    return {
        'vps_name': current_app.config['HOST_NAME'],
        'download_time': format_download_time(time.monotonic() - x1),
    }


@bp.route(api_upload_url_endpoint, methods=['POST'])
async def api_upload_url():
    '''
    request example: {'url': 'http://kyi.download.datapacket.com/10mb.bin'}
    '''

    url = request.json.get('url')
    file_name = url.split('/')[-1]

    async with ClientSession() as session:
        x1 = time.monotonic()
        async with session.get(url) as resp:
            if resp.ok:
                await upload_file_by_chunks(resp, file_name)
                return {
                    'vps_name': current_app.config['HOST_NAME'],
                    'download_time': format_download_time(time.monotonic() - x1),
                }
            else:
                current_app.logger.error(f'Couldn\'t upload file by url \'{url}\'. Response: {resp}')
                return {
                    'error': f'Couldn\'t upload file by url \'{url}\'. Response: {resp}',
                }


@bp.route(api_upload_file_endpoint, methods=['POST'])
async def api_upload_file():
    file = request.files['file']
    file_name = file.filename

    x1 = time.monotonic()
    await upload_file(file, file_name)

    return {
        'vps_name': current_app.config['HOST_NAME'],
        'download_time': format_download_time(time.monotonic() - x1),
    }
