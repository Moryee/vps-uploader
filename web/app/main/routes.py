from flask import render_template, request, make_response, current_app
from app.main import bp
import tempfile
import os
from aiohttp import ClientSession, ClientResponse, FormData
import time
import asyncio
import socket
from tldextract import extract
from geopy.distance import distance


CHUNK_SIZE = 1024 * 1024 * 1


api_upload_url_test_endpoint = '/api/upload-url-test'

api_upload_url_client_endpoint = '/api/upload-url-client'
api_upload_url_publisher_endpoint = '/api/upload-url-publisher'
api_upload_url_endpoint = '/api/upload-url'
api_upload_file_endpoint = '/api/upload-file'
api_test_download_speed_endpoint = '/api/test-download-speed'


# Helper functions


async def upload_file(file, file_name):
    with tempfile.NamedTemporaryFile(delete=False) as temp:
        file.save(temp.name)

        # here can be saving file

        temp.close()
        os.unlink(temp.name)


async def upload_file_by_stream(stream: ClientResponse):
    with tempfile.SpooledTemporaryFile() as temp:
        while True:
            chunk = await stream.content.read(CHUNK_SIZE)
            if not chunk:
                break
            temp.write(chunk)
        temp.seek(0)

        # here can be saving file


async def get_closest_server(location):
    closest_server_distance = float('inf')
    closest_server_host = None
    for host_url in current_app.config['HOSTS_URLS']:
        server_location = await get_location_from_url(host_url)

        if server_location:
            d = distance(location, server_location).km
        else:
            d = float('inf')

        if d < closest_server_distance:
            closest_server_distance = d
            closest_server_host = host_url

    return closest_server_host


async def get_location_from_ip(ip_address):
    API_KEY = current_app.config['IP2LOCATION_API_KEY']

    async with ClientSession() as session:
        url = f'https://api.ip2location.io/?key={API_KEY}&ip={ip_address}'
        response = await session.get(url)
        if response.ok:
            data = await response.json()
            return (data['latitude'], data['longitude'])
        else:
            return None


def get_location_from_url(url):
    ip_address = get_ip_from_url(url)
    return get_location_from_ip(ip_address)


def get_ip_from_url(url):
    ext = extract(url)
    return socket.gethostbyname(ext.subdomain + '.' + ext.domain + '.' + ext.suffix)


# Routes


@bp.route('/')
async def index():
    return render_template('index.html')


@bp.route(api_upload_url_client_endpoint, methods=['POST'])
async def api_upload_url_client():
    '''
    request structure:
    {
        "url": "http://kyi.download.datapacket.com/10mb.bin"
    }
    '''

    if not current_app.config['MAIN_HOST']:
        return make_response({'error': 'This is not main server'}, 400)

    url = request.json.get('url')

    hosts_urls = current_app.config['HOSTS_URLS'][:]
    hosts_urls.append(current_app.config['MAIN_HOST_URL'])
    if current_app.config['DEBUG']:
        closest_host_url = hosts_urls[0]
    else:
        closest_host_url = await get_closest_server(await get_location_from_url(url))

    hosts_urls.remove(closest_host_url)

    async with ClientSession() as session:
        response = await session.post(f'{closest_host_url}{api_upload_url_publisher_endpoint}', json={'url': url, 'hosts_urls': hosts_urls})

        return await response.json()


@bp.route(api_upload_url_test_endpoint, methods=['POST'])
async def api_upload_url_test():
    if not current_app.config['MAIN_HOST']:
        return make_response({'error': 'This is not main server'}, 400)
    this_host_url = current_app.config['MAIN_HOST_URL']

    url = request.json.get('url')

    response = {
        'geo_distributed': None,
        'ordinary': None
    }
    async with ClientSession() as session:

        # geo distributed

        geo_distributed_resp = await session.post(f'{this_host_url}{api_upload_url_client_endpoint}', json={'url': url})
        if geo_distributed_resp.ok:
            response['geo_distributed'] = await geo_distributed_resp.json()
        else:
            response['geo_distributed'] = {'error': 'Something went wrong'}

        # ordinary

        ordinary_resp = await session.post(f'{this_host_url}{api_upload_url_endpoint}', json={'url': url})
        if ordinary_resp.ok:
            response['ordinary'] = await ordinary_resp.json()
        else:
            response['ordinary'] = {'error': 'Something went wrong'}

    return response


@bp.route(api_upload_url_publisher_endpoint, methods=['POST'])
async def api_upload_url_publisher():
    '''
    request example:
    {
        "url": "http://kyi.download.datapacket.com/10mb.bin",
        "hosts_urls": [
            "http://some-host:5000",
            "http://some-host:5000",
        ]
    }
    '''

    url = request.json.get('url')
    hosts_urls = request.json.get('hosts_urls')

    async with ClientSession() as session:
        tasks = []

        x1 = time.monotonic()
        async with session.get(url) as resp:
            with tempfile.NamedTemporaryFile(delete=False) as temp:
                while True:
                    chunk = await resp.content.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    temp.write(chunk)
                temp.seek(0)

                x2 = time.monotonic()

                with open(temp.name, 'rb') as file:
                    for host in hosts_urls:
                        data = FormData()

                        data.add_field('file', file, filename=temp.name, content_type='multipart/form-data')

                        task = asyncio.create_task(session.post(f'{host}{api_upload_file_endpoint}', data=data))
                        tasks.append(task)
                    hosts_responses = await asyncio.gather(*tasks)
                temp.close()
                os.unlink(temp.name)

    response = {
        'publisher_host_name': current_app.config['HOST_NAME'],
        'execution_time': round(x2 - x1, 2),
        'hosts_responses': [],
    }

    for host_response in hosts_responses:
        response['hosts_responses'].append(await host_response.json())

    return response


@bp.route(api_upload_url_endpoint, methods=['POST'])
async def api_upload_url():
    '''
    request example: {'url': 'http://kyi.download.datapacket.com/10mb.bin'}
    '''

    url = request.json.get('url')
    async with ClientSession() as session:
        async with session.get(url) as resp:
            if resp.ok:
                x1 = time.monotonic()
                await upload_file_by_stream(resp)
                return {
                    'host_name': current_app.config['HOST_NAME'],
                    'execution_time': round(time.monotonic() - x1, 2),
                }
            else:
                return {
                    'error': f'Couldn\'t upload file by url. {resp.status}',
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
