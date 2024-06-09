from flask import render_template, request, make_response, current_app
from app.main import bp
import time
from app.models.test import Test
import uuid
import datetime
from aiohttp import ClientSession


from common.utils import (
    get_ip_from_url,
    upload_file_by_chunks,
    upload_file,
    format_download_time,
)
from app.main.urls import (
    api_upload_url_test_endpoint,
    api_upload_url_endpoint,
    api_upload_file_endpoint,
)
from app.main.service import upload_url_task


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
        "eta": int (unix timestamp, utc, not required)
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

    # eta (estimated time of arrival)

    try:
        eta = int(request.json.get('eta', 0))
        if eta:
            eta_datetime = datetime.datetime.fromtimestamp(eta, tz=datetime.timezone.utc)
        else:
            eta_datetime = None
    except ValueError:
        make_response({'error': 'Coudn\'t read \'eta\' value, make sure \'eta\' is unix timestamp'})

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
    """
    request example:
    {
        "url": "http://kyi.download.datapacket.com/10mb.bin",
        "speed": int mb/s,
        "amount": int (default 1),
    }
    """
    current_app.logger.info('uploading file')
    file = request.files['file']

    # try:
    #     speed = int(request.form.get('speed', 100))

    #     if speed < 1:
    #         make_response({'error': 'Speed must be greater than 1'}, 400)
    # except ValueError:
    #     return make_response({'error': 'Speed must be integer'}, 400)

    start_time = time.monotonic()
    upload_file(file, 100)

    return {
        'vps_name': current_app.config['HOST_NAME'],
        'file_ip': 'None',
        'time': format_download_time(time.monotonic() - start_time),
        'ttfb': 0,
        'latency': 0
    }
