#!/usr/bin/env python3

import os
import logging

from radio_pool import RadioPool

logger = logging.getLogger(__name__)

if __name__ == '__main__':
    try:
        ll = os.environ['LOG_LEVEL']

        if ll == 'DEBUG':
            LOG_LEVEL = logging.DEBUG
        elif ll == 'INFO':
            LOG_LEVEL = logging.INFO
        elif ll == 'WARNING':
            LOG_LEVEL = logging.WARNING
        elif ll == 'ERROR':
            LOG_LEVEL = logging.ERROR
        elif ll == 'CRITICAL':
            LOG_LEVEL = logging.CRITICAL
        else:
            raise ValueError(f'Bad log level {ll}')
    except KeyError:
        LOG_LEVEL = logging.INFO

    logging.basicConfig(
        level=LOG_LEVEL,
        format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    try:
        S3_BUCKET = os.environ['S3_BUCKET']
    except KeyError as exc:
        raise ValueError("Must provide S3 bucket") from exc

    try:
        S3_PREFIX = os.environ['S3_PREFIX']
    except KeyError:
        S3_PREFIX = ''

    try:
        DSN = os.environ['DSN']
    except KeyError:
        DSN = 'Database'

    try:
        N_TASKS = int(os.environ['N_TASKS'])
    except KeyError:
        N_TASKS = 10

    try:
        POLL_INTERVAL = int(os.environ['POLL_INTERVAL'])
    except KeyError:
        POLL_INTERVAL = 300

    try:
        CHUNK_ERROR_BEHAVIOR = os.environ['CHUNK_ERROR_BEHAVIOR']
    except KeyError:
        CHUNK_ERROR_BEHAVIOR = 'ignore'

    try:
        CHUNK_SIZE = int(os.environ['CHUNK_SIZE'])
    except KeyError:
        CHUNK_SIZE = 5 * 2**20

    try:
        CHUNK_ERROR_THRESHOLD = int(os.environ['CHUNK_ERROR_THRESHOLD'])
    except KeyError:
        CHUNK_ERROR_THRESHOLD = 10

    try:
        CREATE_SCHEMA = int(os.environ['CREATE_SCHEMA'])
    except KeyError:
        CREATE_SCHEMA = 1

    try:
        DATA_SOURCE_S3_BUCKET = os.environ['DATA_SOURCE_S3_BUCKET']
    except KeyError:
        DATA_SOURCE_S3_BUCKET = 'lsm-data-1'    #  prev 'lsm-data'

    try:
        DATA_SOURCE_S3_KEY = os.environ['DATA_SOURCE_S3_KEY']
    except KeyError:
        DATA_SOURCE_S3_KEY = 'talk-radio/radio.tar.gz'

    args = {
        's3_bucket': S3_BUCKET,
        's3_prefix': S3_PREFIX,
        'dsn': DSN,
        'n_tasks': N_TASKS,
        'chunk_error_behavior': CHUNK_ERROR_BEHAVIOR,
        'poll_interval': POLL_INTERVAL,
        'chunk_size': CHUNK_SIZE,
        'chunk_error_threshold': CHUNK_ERROR_THRESHOLD,
        'create_schema': CREATE_SCHEMA,
        'db_setup': {
            'data_source_s3_bucket': DATA_SOURCE_S3_BUCKET,
            'data_source_s3_key': DATA_SOURCE_S3_KEY
        }
    }

    with open('schema.sql', 'r', encoding='utf-8') as f:
        args['db_setup']['schema_sql'] = f.read().strip()

    with RadioPool(**args) as pool:
        pool.run()
