import os
import re
import time
import logging
import multiprocessing as mp

import pyodbc

import exceptions as ex
from radio_worker import RadioWorker, payload

logger = logging.getLogger(__name__)

class RadioPool(object):
    def __init__(self, **kwargs):
        try:
            self.s3_bucket = kwargs.pop('s3_bucket')
        except KeyError:
            raise ValueError("Must provide s3_bucket")

        self.s3_prefix = kwargs.pop('s3_prefix', '')
        self.dsn = kwargs.pop('dsn', 'Database')
        self.chunk_error_behavior = kwargs.pop('chunk_error_behavior', 'ignore')
        self.chunk_error_threshold = kwargs.pop('chunk_error_threshold', 10)
        self.chunk_size = kwargs.pop('chunk_size', 5 * 2**20)
        self.create_schema = kwargs.pop('create_schema', 1)
        self.db_setup = kwargs.pop('db_setup', None)

        self.poll_interval = kwargs.pop('poll_interval', 300)
        self.n_tasks = kwargs.pop('n_tasks', 10)

        super(RadioPool, self).__init__(**kwargs)

        self.db = pyodbc.connect(dsn=self.dsn)
        self.pool = mp.Pool(self.n_tasks, maxtasksperchild=1)

    def __enter__(self):
        return self

    def __exit__(self, tp, val, traceback):
        self.close()

    def close(self):
        try:
            self.db.close()
        except Exception as e:
            pass

        try:
            self.pool.terminate()
        except Exception as e:
            pass

    def run(self):
        # Spawn initial set of tasks
        results = []

        args = {
            'dsn': self.dsn,
            's3_bucket': self.s3_bucket,
            's3_prefix': self.s3_prefix,
            'chunk_error_behavior': self.chunk_error_behavior,
            'chunk_error_threshold': self.chunk_error_threshold,
            'chunk_size': self.chunk_size,
            'poll_interval': self.poll_interval,
            'create_schema': self.create_schema,
            'db_setup': self.db_setup
        }

        logger.debug('Spawning initial tasks')
        for i in range(0, self.n_tasks):
            results += [self.pool.apply_async(payload, (args,))]
        logger.debug('Spawned initial tasks')

        while True:
            time.sleep(self.poll_interval)

            for (i, res) in enumerate(results):
                if res.ready():
                    if res.successful():
                        msg = "Incorrect termination by ingest worker"
                        raise ValueError(msg)
                    else:
                        try:
                            res.get()
                        except Exception as e:
                            logger.exception("Worker exited")

                    # Respawn the task after it exited
                    results[i] = self.pool.apply_async(payload, (args,))

