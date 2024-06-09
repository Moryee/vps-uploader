from flask_sse import sse
from flask import current_app
from .network import get_ip_from_url


class UploadStatus:
    '''
    status structure:
        {
            "file_size": float kb,
            "tebi_status": int,
            "tebi_servers": "DE:2,SGP:1,USE:2,USW:2",
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
