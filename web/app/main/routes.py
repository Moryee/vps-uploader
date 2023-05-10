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


CHUNK_SIZE = 1024 * 1024 * 1
DOWNLOADING_SPEED = 100 * 1024 * 1024 / 8


api_upload_url_test_endpoint = '/api/upload-url-test'
api_host_test = '/api/host-test'
api_listen_upload_endpoint = '/api/listen-upload'

api_upload_url_endpoint = '/api/upload-url'
api_upload_file_endpoint = '/api/upload-file'
api_upload_tebi_endpoint = '/api/upload-tebi'
api_test_download_speed_endpoint = '/api/test-download-speed'


# Helper functions


class UploadStatus:
    '''
    status structure:
        {
            'tebi_status': int,
            'vps': [
                {
                    vps_ip: str,
                    tebi: int,
                    object: int
                }
            ]
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
        self.tebi_status = None
        self.vps = {}
        self.uuid = uuid

    def set_tebi_status(self, status: int):
        if status not in [0, 1, 2, 3]:
            raise ValueError('status must be either 0, 1, 2, or 3')
        self.tebi_status = status

        self.make_announcement()

    def vps_update_status(self, vps_ip: str, storage: str, status: int):
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

        if vps_ip not in self.vps.keys():
            self.vps[vps_ip] = {}

        self.vps[vps_ip][storage] = status

        self.make_announcement()

    def get_status(self):
        vps_statuses = []

        for vps_ip, vps_storage in self.vps.items():
            vps_statuses.append({
                'vps_ip': vps_ip,
                'tebi': vps_storage.get('tebi', 0),
                'object': vps_storage.get('object', 0)
            })

        return {
            'tebi_status': self.tebi_status,
            'vps': vps_statuses
        }

    def make_announcement(self):
        sse.publish(self.get_status(), channel=self.uuid)


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


async def publish(url, file_name, upload_status: UploadStatus):
    '''
    returns dict
    {
        'ok': int,
        'failed': int,
        'responses': []
    }
    '''
    this_host_url = current_app.config['MAIN_HOST_URL']
    hosts_urls = current_app.config['HOSTS_URLS'][:]
    hosts_urls.append(this_host_url)

    output = {
        'ok': 0,
        'failed': 0,
        'responses': []
    }

    async def make_post(session, host, endpoint, json) -> tuple[ClientResponse, dict]:
        start_time = time.monotonic()

        resp: ClientResponse
        async with session.post(f'{host}{endpoint}', json=json) as resp:
            if not resp.ok:
                current_app.logger.error(f'Couldn\'t publish to host \'{host}\'. Response: {resp.status} {resp.reason}')
                return resp, {'ok': False, 'error': f'Couldn\'t publish to host \'{host}\''}

            end_time = time.monotonic()

            json_mod = await resp.json()

            ttfb_client = end_time - start_time
            ttfb_server = float(resp.headers['x-ttfb'])

            json_mod['ok'] = True
            json_mod['ttfb'] = round(ttfb_client, 2)
            json_mod['latency'] = round(ttfb_client - ttfb_server, 2)

            return resp, json_mod

    async with ClientSession() as session:
        for h_url in hosts_urls:
            h_ip = get_ip_from_url(h_url)
            upload_status.vps_update_status(h_ip, 'object', 1)
            upload_status.vps_update_status(h_ip, 'tebi', 1)

            url_resp, url_json = await make_post(session, h_url, api_upload_url_endpoint, {'url': url})
            tebi_resp, tebi_json = await make_post(session, h_url, api_upload_tebi_endpoint, {'file_name': file_name})

            upload_status.vps_update_status(h_ip, 'object', 2)
            upload_status.vps_update_status(h_ip, 'tebi', 2)

            if url_resp.ok or tebi_resp.ok:
                vps_name = url_json['vps_name'] if url_resp.ok else tebi_json['vps_name']

                output['responses'].append(
                    {
                        'vps_name': vps_name,
                        'vps_ip': get_ip_from_url(vps_name),
                        'tebi': tebi_json,
                        'object': url_json
                    }
                )

            output['ok'] += url_json['ok'] + tebi_json['ok']
            output['failed'] += 2 - url_json['ok'] - tebi_json['ok']

    return output


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
        "uuid": 'uuid',
        "url": "http://kyi.download.datapacket.com/10mb.bin",
    }
    '''
    if not current_app.config['MAIN_HOST']:
        return make_response({'error': 'This is not main server'}, 400)

    url = request.json.get('url')

    channel_uuid = request.json.get('uuid')
    upload_status = UploadStatus(channel_uuid)

    if not url:
        return make_response({'error': 'No url provided'}, 400)

    if not channel_uuid:
        return make_response({'error': 'No uuid provided'}, 400)

    upload_status.set_tebi_status(0)  # waiting

    file_name = time.strftime('%Y-%m-%d_%H-%M-%S_') + url.split('/')[-1]
    file_ip = get_ip_from_url(url)

    response = {
        'file_size': None,
        'file_ip': file_ip,
        'ok': 0,
        'failed': 0,
        'tebi_servers': None,
        'vps': None,
    }

    async with ClientSession() as session:
        async with session.get(url) as resp:
            if not resp.ok:
                current_app.logger.error(f'Couldn\'t get file from \'{url}\'. Response: {resp}')
                return {'error': f'Couldn\'t get file from \'{url}\'.'}, 400

            upload_status.set_tebi_status(1)  # uploading file

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
                response['file_size'] = downloaded_size / (1024 ** 2)
                new_temp_file.truncate(downloaded_size)

                with open(new_temp_file.name, 'rb') as f:
                    get_bucket().put_object(Key=file_name, Body=f)

                new_temp_file.close()
                os.unlink(new_temp_file.name)

    upload_status.set_tebi_status(2)  # waiting for replication

    s3_client = tebi_get_client()

    def is_all_replicated(replication_status):
        return replication_status == 'DE:1,SGP:1,USE:1,USW:1'

    replication_complete = False
    tries = 0
    while not replication_complete and tries < 20:
        try:
            resp = s3_client.head_object(Bucket=current_app.config['TEBI_BUCKET'], Key=file_name)
            replication_status = resp.get('ResponseMetadata', {}).get('HTTPHeaders', {}).get('x-tb-replication', None)

            if is_all_replicated(replication_status):
                replication_complete = True
        except Exception as e:
            print(f"Error checking replication status: {e}")
            break

        if not replication_complete:
            tries += 1
            await asyncio.sleep(2)

    if replication_complete:
        upload_status.set_tebi_status(3)  # replication completed
        publish_data = await publish(url, file_name, upload_status)
        response['vps'] = publish_data['responses']
        response['ok'] = publish_data['ok']
        response['failed'] = publish_data['failed']
        response['tebi_servers'] = replication_status
        get_bucket().delete_objects(Delete={'Objects': [{'Key': file_name}]})
    else:
        current_app.logger.error(f'Couldn\'t replicate file. Replication status: {replication_status}')
        return {'error': f'Couldn\'t replicate file. Replication status: {replication_status}'}, 500

    return response


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
