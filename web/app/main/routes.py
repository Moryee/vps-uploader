from flask import render_template, request, make_response, current_app
from app.main import bp
import tempfile
import os
from aiohttp import ClientSession, ClientResponse
import time
import asyncio
import boto3
from app.models.test import Test
from app.models.monopoly import MonopolyMode
from tldextract import extract
import socket
from flask_sse import sse
import uuid
from celery import shared_task
from app.extensions import db
import datetime


CHUNK_SIZE = 1024 * 1024 * 1


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
            "tebi_status": int,
            "tebi_servers": "DE:1,USE:1,USW:2,SGP:1",
            "ok": int,
            "failed": int,
            "finished": bool,
            "vps": {
                "vps_name_1": {
                    "ip": "10.10.10.10",
                    "tebi": {
                        "ip": str,
                        "status": "1",
                        "latency": "10.234",
                        "ttfb": "123.456",
                        "time": "1234.765"
                    },
                    "object": {
                        "ip": str,
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
    def uuid(self):
        return self._uuid

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

    def vps_complete_status(self, vps_name: str, storage: str, latency: float, ttfb: float, time: float, ip: str):
        if storage not in ['tebi', 'object']:
            raise ValueError('storage must be either \'tebi\' or \'object\'')

        if vps_name not in self._vps.keys():
            raise ValueError(f'vps_name \'{vps_name}\' not found')

        self._vps[vps_name][storage] = {
            'status': 2,
            'latency': latency,
            'ttfb': ttfb,
            'time': time,
            'ok': True,
            'ip': ip
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
def upload_url_task(url, channel_uuid, speed, monopoly, amount):
    asyncio.run(upload_url(
        url=url,
        channel_uuid=channel_uuid,
        speed=speed,
        monopoly=monopoly,
        amount=amount,
    ))


async def upload_url(url, channel_uuid, speed, monopoly, amount):
    monopoly_model: MonopolyMode = MonopolyMode.get()
    try:
        monopoly_model.start_mode(monopoly)

        start_time = time.monotonic()

        upload_status = UploadStatus(channel_uuid)
        upload_status.tebi_status = 0  # waiting

        upload_object_task = asyncio.create_task(publish(api_upload_url_endpoint, {'url': url, 'speed': speed, 'amount': amount}, 'object', upload_status))

        file_name, file_size = await replicate_url(url, upload_status)

        try:
            await asyncio.gather(upload_object_task)
            await publish(api_upload_tebi_endpoint, {'file_name': file_name, 'speed': speed, 'amount': amount}, 'tebi', upload_status)
            get_bucket().delete_objects(Delete={'Objects': [{'Key': file_name}]})
        except Exception as e:
            current_app.logger.error(e)
            upload_status.finished_with_exception('error while uploading file to vps')

        upload_status.finished()
        current_app.logger.info(f'finished {upload_status.get_status()}')

        end_time = time.monotonic()

        test = Test(
            id=upload_status.uuid,
            content=upload_status.get_status(),
            url=url,
            execution_time=round((end_time - start_time) * 1000, 3)
        )
        db.session.add(test)
        db.session.commit()
    except Exception as e:
        raise e
    finally:
        monopoly_model.end_mode(monopoly)


async def replicate_url(url, upload_status: UploadStatus):
    '''
    returns tuple `file_name`, `file_size`
    '''

    correct_replication_status = 'DE:2,SGP:1,USE:2,USW:2'
    file_name: str = time.strftime('%Y-%m-%d_%H-%M-%S_') + url.split('/')[-1]
    file_size = 0

    async with ClientSession() as session:
        async with session.get(url) as resp:
            if not resp.ok:
                current_app.logger.error(f'Couldn\'t get file from \'{url}\'. Response: {resp}')
                return {'error': f'Couldn\'t get file from \'{url}\'.'}, 400

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
                upload_status.file_size = downloaded_size / 1024

                new_temp_file.truncate(downloaded_size)

                upload_status.tebi_status = 1  # uploading file

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


async def publish(upload_endpoint, json_data, storage_type, upload_status: UploadStatus):
    vps_urls: dict = current_app.config['VPS_URLS'].copy()

    async def make_post(session, vps_name, vps_url, endpoint, json, storage_type) -> tuple[ClientResponse, dict]:
        upload_status.vps_update_status(vps_name, storage_type, 1)

        resp: ClientResponse
        async with session.post(f'{vps_url}{endpoint}', json=json) as resp:
            if not resp.ok:
                current_app.logger.error(f'Couldn\'t publish to host \'{vps_url}{endpoint}\'. Response: {resp.status} {resp.reason}')
                upload_status.vps_failed_status(vps_name, storage_type)
                return

            resp_dict: dict = await resp.json()

            upload_status.vps_complete_status(
                vps_name=vps_name,
                storage=storage_type,
                ip=resp_dict.get('file_ip'),
                time=float(resp_dict.get('time')),
                ttfb=float(resp_dict.get('ttfb')),
                latency=float(resp_dict.get('latency')),
            )

    async with ClientSession() as session:
        for vps_name, vps_url in vps_urls.items():
            try:
                await make_post(session, vps_name, vps_url, upload_endpoint, json_data, storage_type)
            except Exception as e:
                current_app.logger.error(e)
                upload_status.vps_failed_status(vps_name, storage_type)


async def upload_file(file, file_name):
    with tempfile.NamedTemporaryFile(delete=False) as temp:
        file.save(temp.name)

        # here can be saving file

        temp.close()
        os.unlink(temp.name)


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

        # here can be saving file

        temp.close()
        os.unlink(temp.name)

    actual_download_speed = total_bytes_downloaded / (1024 * 1024 * elapsed_time)
    current_app.logger.info(f'downloading speed object: {actual_download_speed}mbps')


def get_ip_from_url(url):
    ext = extract(url)

    if not ext.suffix:
        return ext.domain

    subdomain = ext.subdomain if ext.subdomain else 'www'
    return socket.gethostbyname(subdomain + '.' + ext.domain + '.' + ext.suffix)


def format_download_time(seconds: float):
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


def calculate_downloading_speed(speed):
    return speed * 1024 * 1024 / 8


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
        "amount": int (default 1),
        "speed": int mb/s (default 100),
        "monopoly": bool (default false),
        "eta": int (timestamp, utc, not required)
    }
    '''
    if not current_app.config['MAIN_HOST']:
        return make_response({'error': 'This is not main server'}, 400)

    # amount & url

    amount = int(request.json.get('amount', 1))
    if amount >= 2 and amount <= 100:
        url = 'http://kyi.download.datapacket.com/1mb.bin'
    elif amount == 1:
        url = request.json.get('url')
        if not url:
            return make_response({'error': 'No url provided'}, 400)
    else:
        return make_response({'error': '\'amount\' must be greater than 0 and less than 100'}, 400)

    # speed

    try:
        speed = int(request.json.get('speed', 100))

        if speed < 1:
            make_response({'error': '\'speed\' must be greater than 1'}, 400)
    except ValueError:
        return make_response({'error': '\'speed\' must be integer'}, 400)

    # monopoly

    monopoly = request.json.get('monopoly', False)
    monopoly_model: MonopolyMode = MonopolyMode.get()
    if monopoly_model.lock:
        return make_response({'error': 'Monopoly test is currently running'}, 400)

    # eta (estimated time of arrival)

    try:
        eta = int(request.json.get('eta', 0))
    except ValueError:
        return make_response({'error': '\'eta\' must be integer'})

    if eta:
        eta_datetime = datetime.datetime.fromtimestamp(eta, tz=datetime.timezone.utc)
    else:
        eta_datetime = None

    if eta < 1:
        make_response({'error': '\'eta\' must be greater than 1'})

    current_app.logger.info(f'Upload url test, url: {url}, amount: {amount}, speed: {speed}, monopoly: {monopoly}, eta: {eta_datetime}')

    channel_uuid = str(uuid.uuid4())

    kwargs = {
        'url': url,
        'channel_uuid': channel_uuid,
        'speed': speed,
        'monopoly': monopoly,
        'amount': amount
    }
    if not eta_datetime:
        upload_url_task.delay(
            **kwargs
        )
    else:
        upload_url_task.apply_async(
            kwargs=kwargs, eta=eta_datetime
        )

    main_host_url = current_app.config['MAIN_HOST_URL']
    if current_app.config['DEBUG']:
        return {'sse_stream_url': f'/stream?channel={channel_uuid}', 'uuid': channel_uuid}
    else:
        return {'sse_stream_url': f'{main_host_url}/stream?channel={channel_uuid}', 'uuid': channel_uuid}


@bp.route(api_upload_tebi_endpoint, methods=['POST'])
async def api_upload_tebi():
    '''
    request example:
    {
        "file_name": "file_name.bin",
        "speed": int mb/s,
        "amount": int (default 1),
    }
    '''

    amount = int(request.json.get('amount', 1))
    if amount >= 2 and amount <= 100:
        file_name = '1mb.bin'
    elif amount == 1:
        file_name = request.json.get('file_name')
        if not file_name:
            return make_response({'error': 'No file_name provided'}, 400)
    else:
        return make_response({'error': 'amount must be greater than 0 and less than 100'}, 400)

    try:
        speed = int(request.json.get('speed', 100))

        if speed < 1:
            make_response({'error': 'Speed must be greater than 1'}, 400)
    except ValueError:
        return make_response({'error': 'Speed must be integer'}, 400)

    head_start_time = time.monotonic()
    tebi_get_client().head_object(Bucket=current_app.config['TEBI_BUCKET'], Key=file_name)
    latency = time.monotonic() - head_start_time

    with tempfile.NamedTemporaryFile(delete=False) as temp:
        start_time = time.monotonic()
        total_bytes_downloaded = 0

        for i in range(amount):
            start_time = time.monotonic()
            body = get_bucket().Object(file_name).get()['Body']
            ttfb = time.monotonic() - start_time

            while True:
                chunk = body.read(CHUNK_SIZE)
                if not chunk:
                    break
                temp.write(chunk)

                total_bytes_downloaded += len(chunk)

                elapsed_time = time.monotonic() - start_time
                expected_time = total_bytes_downloaded / (speed * 1024 * 1024)
                if elapsed_time < expected_time:
                    time.sleep(expected_time - elapsed_time)
            temp.close()
            os.unlink(temp.name)
        actual_download_speed = total_bytes_downloaded / (1024 * 1024 * elapsed_time)
        current_app.logger.info(f'downloading speed tebi: {actual_download_speed}mbps')

    return {
        'vps_name': current_app.config['HOST_NAME'],
        'file_ip': get_ip_from_url('https://s3.tebi.io'),
        'time': format_download_time(time.monotonic() - start_time),
        'ttfb': format_download_time(ttfb),
        'latency': format_download_time(latency / 2),
    }


@bp.route(api_upload_url_endpoint, methods=['POST'])
async def api_upload_url():
    '''
    request example:
    {
        "url": "http://kyi.download.datapacket.com/10mb.bin",
        "speed": int mb/s,
        "amount": int (default 1),
    }
    '''

    amount = int(request.json.get('amount', 1))
    if amount >= 2 and amount <= 100:
        url = 'http://kyi.download.datapacket.com/1mb.bin'
    elif amount == 1:
        url = request.json.get('url')
        if not url:
            return make_response({'error': 'No url provided'}, 400)
    else:
        return make_response({'error': 'amount must be greater than 0 and less than 100'}, 400)

    try:
        speed = int(request.json.get('speed', 100))

        if speed < 1:
            make_response({'error': 'Speed must be greater than 1'}, 400)
    except ValueError:
        return make_response({'error': 'Speed must be integer'}, 400)

    async with ClientSession() as session:
        start_time = time.monotonic()
        async with session.get(url) as resp:
            if resp.ok:
                ttfb = time.monotonic() - start_time

                for i in range(amount):
                    await upload_file_by_chunks(resp, speed)
                return {
                    'vps_name': current_app.config['HOST_NAME'],
                    'file_ip': get_ip_from_url(url),
                    'time': format_download_time(time.monotonic() - start_time),
                    'ttfb': format_download_time(ttfb),
                    'latency': format_download_time(ttfb / 2)
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
