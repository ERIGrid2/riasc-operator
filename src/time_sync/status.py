import json
import logging
import os
import subprocess
import sys
import threading
import time
import kubernetes

from datetime import datetime
from http.client import responses
from tornado import ioloop, web
from gpsdclient import GPSDClient

API_PREFIX = '/api/v1'
UPDATE_INTERVAL = 10.0
ANNOTATION_PREFIX = 'time-sync.riasc.eu'
NODE_NAME = os.environ.get('NODE_NAME')
DEBUG = os.environ.get('DEBUG') in ['true', '1', 'on']


class BaseRequestHandler(web.RequestHandler):

    def initialize(self, status: dict, config: dict):
        self.status = status
        self.config = config

    def write_error(self, status_code, **kwargs):
        self.finish({
            'error': responses.get(status_code, 'Unknown error'),
            'code': status_code,
            **kwargs
        })


class StatusHandler(BaseRequestHandler):

    def get(self):
        if self.status:
            self.write(self.status)
        else:
            raise web.HTTPError(500, 'failed to get status')


class ConfigHandler(BaseRequestHandler):

    def get(self):
        if self.config:
            self.write(self.config)
        else:
            raise web.HTTPError(500, 'failed to get config')


class SyncedHandler(BaseRequestHandler):

    def get(self):
        if not self.config.get('synced'):
            raise web.HTTPError(500, 'not synced')


def patch_node_status(v1, status: dict):
    synced = status.get('synced')
    if synced is True:
        condition = {
            'type': 'TimeSynced',
            'status': 'True',
            'reason': 'ChronyHasSyncSource',
            'message': 'Time of node is synchronized'
        }
    elif synced is False:
        condition = {
            'type': 'TimeSynced',
            'status': 'False',
            'reason': 'ChronyHasNoSyncSource',
            'message': 'Time of node is not synchronized'
        }
    else:  # e.g. None
        condition = {
            'type': 'TimeSynced',
            'status': 'Unknown',
            'reason': 'ChronyNotRunning',
            'message': 'Time of node is not synchronized'
        }

    patch = {
        'status': {
            'conditions': [condition]
        }
    }

    v1.patch_node_status(NODE_NAME, patch)

    logging.info('Updated node condition')


def patch_node(v1, status: dict):
    gpsd_status = status.get('gpsd')
    chrony_status = status.get('chrony')

    annotations = {}

    synced = status.get('synced')
    if synced is None:
        annotations['synced'] = 'unknown'
    elif synced:
        annotations['synced'] = 'true'
    else:
        annotations['synced'] = 'false'

    if chrony_status:
        for key in ['stratum', 'ref_name', 'leap_status']:
            annotations[key] = chrony_status.get(key)

    if gpsd_status:
        tpv = gpsd_status.get('tpv')
        if tpv:
            if tpv.get('mode') == 1:
                fix = 'none'
            elif tpv.get('mode') == 2:
                fix = '2d'
            elif tpv.get('mode') == 3:
                fix = '3d'
            else:
                fix = 'unknown'

            if tpv.get('status') == 2:
                status = 'dgps'
            else:
                status = 'none'

            annotations.update({
                'position-latitude': tpv.get('lat'),
                'position-longitude': tpv.get('lon'),
                'position-altitude': tpv.get('alt'),
                'gps-fix': fix,
                'gps-status': status,
                'last-gps-time': tpv.get('time')
            })

    patch = {
        'metadata': {
            'annotations': {
                ANNOTATION_PREFIX + '/' + key.replace('_', '-'): str(value) for (key, value) in annotations.items()
            }
        }
    }

    v1.patch_node(NODE_NAME, patch)

    logging.info('Updated node annotations')


def get_chrony_status() -> dict:
    sources = {}
    fields = {
        'sources': sources
    }

    ret = subprocess.run(['chronyc', '-ncm', 'tracking', 'sources'], capture_output=True, check=True)

    lines = ret.stdout.decode('ascii').split('\n')

    logging.debug('Received update from Chrony: %s', lines)

    cols = lines[0].split(',')

    fields['ref_id'] = int(cols[0], 16)
    fields['ref_name'] = cols[1]
    fields['stratum'] = int(cols[2])
    fields['ref_time'] = datetime.utcfromtimestamp(float(cols[3]))
    fields['current_correction'] = float(cols[4])
    fields['last_offset'] = float(cols[5])
    fields['rms_offset'] = float(cols[6])
    fields['freq_ppm'] = float(cols[7])
    fields['resid_freq_ppm'] = float(cols[8])
    fields['skew_ppm'] = float(cols[9])
    fields['root_delay'] = float(cols[10])
    fields['root_dispersion'] = float(cols[11])
    fields['last_update_interval'] = float(cols[12])
    fields['leap_status'] = cols[13].lower()

    for line in lines[1:]:
        cols = line.split(',')
        if len(cols) < 8:
            continue

        name = cols[2]
        if cols[0] == '^':
            mode = 'server'
        elif cols[0] == '=':
            mode = 'peer'
        elif cols[0] == '#':
            mode = 'ref_clock'
        else:
            mode = 'unknown'

        if cols[1] == '*':
            state = 'synced'
        elif cols[1] == '+':
            state = 'combined'
        elif cols[1] == '-':
            state = 'excluded'
        elif cols[1] == '?':
            state = 'lost'
        elif cols[1] == 'x':
            state = 'false'
        elif cols[1] == '~':
            state = 'too_variable'
        else:
            state = 'unknown'

        sources[name] = {
            'mode': mode,
            'state': state,
            'stratum': cols[3],
            'poll': cols[4],
            'reach': cols[5],
            'last_rx': cols[6],
            'last_sample': cols[7]
        }

    return fields


def is_synced(status: dict) -> bool:
    chrony_status = status.get('chrony')
    if chrony_status is None:
        return None

    for _, source in status.get('sources', {}).items():
        if source.get('state', 'unknown') == 'synced':
            return True

    return False


def update_status_gpsd(status: dict):
    status['gpsd'] = {}

    while True:
        client = GPSDClient()
        for result in client.dict_stream(convert_datetime=True):
            cls = result['class'].lower()
            status['gpsd'][cls] = result

            logging.info('Received update from GPSd: %s', result)


def update_status(v1, status: dict):
    while True:
        try:
            status['chrony'] = get_chrony_status()
            status['synced'] = is_synced(status)

            logging.info('Received update from Chrony: %s', status['chrony'])
        except Exception as e:
            logging.error('Failed to query chrony status: %s', e)

            status['chrony'] = None

        try:
            patch_node_status(v1, status)
            patch_node(v1, status)
        except Exception as e:
            logging.error('Failed to update node status: %s', e)

        time.sleep(UPDATE_INTERVAL)


def load_config(fn: str = '/config.json') -> dict:
    with open(fn) as f:
        return json.load(f)


def main():
    logging.basicConfig(level=logging.DEBUG if DEBUG else logging.INFO)

    if os.environ.get('KUBECONFIG'):
        kubernetes.config.load_kube_config()
    else:
        kubernetes.config.load_incluster_config()

    v1 = kubernetes.client.CoreV1Api()

    if len(sys.argv) >= 2:
        config = load_config(sys.argv[1])
    else:
        config = load_config()

    # Check if we have a valid config
    if not config:
        raise RuntimeError('Missing configuration')

    status = {}

    # Check if we have a node name
    if not NODE_NAME:
        raise RuntimeError('Missing node-name')

    # Start background threads
    t = threading.Thread(target=update_status, args=(v1, status))
    t.start()

    gps_config = config.get('gps')
    if gps_config and gps_config.get('enabled'):
        t2 = threading.Thread(target=update_status_gpsd, args=(status,))
        t2.start()

    args = {
        'status': status,
        'config': config,
    }

    app = web.Application([
        (API_PREFIX + r"/status", StatusHandler, args),
        (API_PREFIX + r"/status/synced", SyncedHandler, args),
        (API_PREFIX + r"/config", ConfigHandler, args),
    ])

    while True:
        try:
            app.listen(8099)
            break
        except Exception as e:
            logging.error('Failed to bind for HTTP API: %s. Retrying in 5 sec', e)
            time.sleep(5)

    ioloop.IOLoop.current().start()


if __name__ == '__main__':
    main()
