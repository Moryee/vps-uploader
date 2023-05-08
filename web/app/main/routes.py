from flask import render_template, request, make_response, current_app
from app.main import bp
import tempfile
import os
from aiohttp import ClientSession, ClientResponse
import time
import asyncio
import boto3
from app.models.test import Test


CHUNK_SIZE = 1024 * 1024 * 1
DOWNLOADING_SPEED = 100 * 1024 * 1024 / 8


api_upload_url_test_endpoint = '/api/upload-url-test'

api_upload_url_endpoint = '/api/upload-url'
api_upload_file_endpoint = '/api/upload-file'
api_upload_tebi_endpoint = '/api/upload-tebi'
api_test_download_speed_endpoint = '/api/test-download-speed'


# Helper functions


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


async def publish(endpoint, json):
    this_host_url = current_app.config['MAIN_HOST_URL']
    hosts_urls = current_app.config['HOSTS_URLS'][:]
    hosts_urls.append(this_host_url)

    async def make_post(session, host, endpoint, json):
        start_time = time.monotonic()

        resp: ClientResponse
        async with session.post(f'{host}{endpoint}', json=json) as resp:
            end_time = time.monotonic()

            json_mod = await resp.json()

            ttfb_client = end_time - start_time
            ttfb_server = float(resp.headers['x-ttfb'])

            json_mod['ttfb'] = round(ttfb_client, 2)
            json_mod['latency'] = round(ttfb_client - ttfb_server, 2)

            return {'response': resp, 'json': json_mod}

    async with ClientSession() as session:
        tasks = []

        for host in hosts_urls:
            task = asyncio.create_task(make_post(session, host, endpoint, json))
            tasks.append(task)

        hosts_responses = await asyncio.gather(*tasks)

    responses = []

    for h_resp in hosts_responses:
        if h_resp['response'].ok:
            responses.append(h_resp['json'])
        else:
            current_app.logger.error(f'Couldn\'t publish to host \'{host + endpoint}\'. Response: {h_resp}')
            responses.append({'error': f'Couldn\'t publish to host \'{host}\''})

    return responses


async def publish_url(url: str):
    return await publish(api_upload_url_endpoint, {'url': url})


async def publish_tebi(file_name: str):
    return await publish(api_upload_tebi_endpoint, {'file_name': file_name})


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
    file_name = time.strftime('%Y-%m-%d_%H-%M-%S_') + url.split('/')[-1]

    response = {
        'tebi': None,
        'ordinary': None
    }

    async with ClientSession() as session:
        async with session.get(url) as resp:
            if not resp.ok:
                response['tebi'] = {'error': f'Couldn\'t publish url \'{url}\'. Response: {resp}'}

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
                new_temp_file.truncate(downloaded_size)

                with open(new_temp_file.name, 'rb') as f:
                    get_bucket().put_object(Key=file_name, Body=f)

                new_temp_file.close()
                os.unlink(new_temp_file.name)

    s3_client = tebi_get_client()

    def is_all_replicated(replication_status):
        return replication_status == 'DE:1,USE:1,USW:1'

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
        response['tebi'] = await publish_tebi(file_name)
        get_bucket().delete_objects(Delete={'Objects': [{'Key': file_name}]})
    else:
        response['tebi'] = {'error': f'Couldn\'t publish url \'{url}\'. Replication status: {replication_status}'}

    response['ordinary'] = await publish_url(url)

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
        'host_name': current_app.config['HOST_NAME'],
        'execution_time': round(time.monotonic() - x1, 2),
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
                    'host_name': current_app.config['HOST_NAME'],
                    'execution_time': round(time.monotonic() - x1, 2),
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
        'host_name': current_app.config['HOST_NAME'],
        'execution_time': round(time.monotonic() - x1, 2),
    }
