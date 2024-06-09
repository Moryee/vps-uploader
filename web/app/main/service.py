from common.utils import UploadStatus
from flask import current_app
import tempfile
import os
from aiohttp import ClientSession, ClientResponse
import time
import asyncio
from app.models.test import Test
from app.models.monopoly import MonopolyMode
from app.extensions import db
import datetime
from sqlalchemy.exc import SQLAlchemyError

from common.settings import CHUNK_SIZE
from app.main.urls import api_upload_url_endpoint, api_upload_file_endpoint
from celery import shared_task


@shared_task(ignore_result=False)
def upload_url_task(url, channel_uuid, speed, monopoly, amount, retries=0):
    asyncio.run(ReplicationService.upload_url(
        url=url,
        channel_uuid=channel_uuid,
        speed=speed,
        monopoly=monopoly,
        amount=amount,
        retries=retries,
    ))


class ReplicationService:
    def __init__(self) -> None:
        pass

    @classmethod
    async def upload_url(cls, url, channel_uuid, speed, monopoly, amount, retries=0):
        monopoly_model: MonopolyMode = MonopolyMode.get()
        if not monopoly_model.start_mode(monopoly):
            max_retries = 20
            wait_seconds = 10

            retries += 1

            if retries == max_retries:
                current_app.logger.info('Couldn\'t wait for task execution')
                UploadStatus(channel_uuid).finished_with_exception('Couldn\'t wait for task execution. Max retries exceeded')
                return

            current_app.logger.info(f'Waiting for task execution {retries}/{max_retries}. Active tests: {monopoly_model.active_tests}, lock {monopoly_model.lock}. monopoly: {monopoly}, url: {url}, amount: {amount}')

            kwargs = {
                'url': url,
                'channel_uuid': channel_uuid,
                'speed': speed,
                'monopoly': monopoly,
                'amount': amount,
                'retries': retries,
            }
            upload_url_task.apply_async(
                kwargs=kwargs, eta=datetime.datetime.utcnow() + datetime.timedelta(seconds=wait_seconds)
            )
            return
        try:
            start_time = time.monotonic()

            upload_status = UploadStatus(channel_uuid)
            upload_status.tebi_status = 0  # waiting

            upload_object_task = asyncio.create_task(ReplicationService.publish_ordinary(api_upload_url_endpoint, {'url': url, 'speed': speed, 'amount': amount}, 'object', upload_status))
            upload_distributed_task = asyncio.create_task(ReplicationService.publish_distributed(api_upload_file_endpoint, {'url': url, 'speed': speed, 'amount': amount}, 'tebi', upload_status))

            try:
                await asyncio.gather(upload_object_task, upload_distributed_task)
            except Exception as e:
                current_app.logger.error(e)
                upload_status.finished_with_exception('error while uploading file to vps')

            upload_status.finished()
            current_app.logger.info(f'finished {upload_status.get_status()}')

            end_time = time.monotonic()

            try:
                db.session.begin_nested()
                test = Test(
                    id=upload_status.uuid,
                    content=upload_status.get_status(),
                    url=url,
                    execution_time=round((end_time - start_time) * 1000, 3)
                )
                db.session.add(test)
                db.session.commit()
            except SQLAlchemyError:
                db.session.rollback()

        except Exception as e:
            current_app.logger.error(e)
            raise e
        finally:
            monopoly_model.end_mode(monopoly)

    @classmethod
    async def publish_ordinary(cls, upload_endpoint, json_data, storage_type, upload_status: UploadStatus):
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


    @classmethod
    async def publish_distributed(cls, upload_endpoint, json_data, storage_type, upload_status: UploadStatus):
        vps_urls: dict = current_app.config['VPS_URLS'].copy()

        url = json_data['url']

        async with ClientSession() as session:
            async with session.get(url) as resp:
                if not resp.ok:
                    current_app.logger.error(f'Couldn\'t get file from \'{url}\'. Response: {resp}')
                    return {'error': f'Couldn\'t get file from \'{url}\'.'}, 400

                downloaded_size = 0
                name = ''
                with tempfile.NamedTemporaryFile(delete=False) as temp:
                    while True:
                        chunk = await resp.content.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        downloaded_size += len(chunk)
                        temp.write(chunk)

                    for vps_name, vps_url in vps_urls.items():
                        temp.seek(0)
                        try:
                            upload_status.vps_update_status(vps_name, storage_type, 1)

                            time = 0
                            ttfb = 0
                            latency = 0

                            resp: ClientResponse
                            while True:
                                chunk = temp.read(CHUNK_SIZE)

                                if not chunk:
                                    break

                                async with session.post(f'{vps_url}{upload_endpoint}', data={'file': chunk}) as resp:
                                    if not resp.ok:
                                        current_app.logger.error(f'Couldn\'t publish to host \'{vps_url}{upload_endpoint}\'. Response: {resp.status} {resp.reason}')
                                        upload_status.vps_failed_status(vps_name, storage_type)
                                        return
                                    resp_dict: dict = await resp.json()
                                    time += float(resp_dict.get('time', 0))
                                    ttfb += float(resp_dict.get('ttfb', 0))
                                    latency += float(resp_dict.get('latency', 0))

                            upload_status.vps_complete_status(
                                vps_name=vps_name,
                                storage=storage_type,
                                ip=resp_dict.get('file_ip'),
                                time=time,
                                ttfb=ttfb,
                                latency=latency,
                            )
                        except Exception as e:
                            current_app.logger.error(e)
                            upload_status.vps_failed_status(vps_name, storage_type)

                    name = temp.name
                    temp.close()
                    os.unlink(name)
