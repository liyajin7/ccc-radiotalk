import gc
import io
import os
import sys
import csv
import time
import json
import random
import tarfile
import logging
import tempfile

import boto3
import pyodbc

import exceptions as ex
from audio_stream import AudioStream

logger = logging.getLogger(__name__)
logging.getLogger('boto3').setLevel(logging.WARNING)
logging.getLogger('botocore').setLevel(logging.WARNING)

def payload(args):
    try:
        with RadioWorker(**args) as worker:
            worker.run()
    except Exception as e:
        logger.exception("Error in station ingest")
        raise

class RadioWorker(object):
    def __init__(self, **kwargs):
        # No AWS creds - we assume they're in the environment
        try:
            s3_bucket = kwargs.pop('s3_bucket')
        except KeyError:
            raise ValueError("Must provide s3 bucket")

        dsn = kwargs.pop('dsn', 'Database')
        s3_prefix = kwargs.pop('s3_prefix', '')
        chunk_error_behavior = kwargs.pop('chunk_error_behavior', 'ignore')
        chunk_error_threshold = kwargs.pop('chunk_error_threshold', 10)
        chunk_size = kwargs.pop('chunk_size', 5 * 2**20)
        poll_interval = kwargs.pop('poll_interval', 300)
        create_schema = kwargs.pop('create_schema', 1)
        db_setup = kwargs.pop('db_setup', None)

        super(RadioWorker, self).__init__(**kwargs)

        if chunk_error_behavior not in ('exit', 'ignore'):
            raise ValueError("chunk_error_behavior must be 'exit' or 'ignore'")

        self.dsn = dsn
        self.s3_bucket = s3_bucket
        self.s3_prefix = s3_prefix
        self.chunk_error_behavior = chunk_error_behavior
        self.chunk_error_threshold = chunk_error_threshold
        self.chunk_size = chunk_size
        self.poll_interval = poll_interval
        self.create_schema = create_schema
        self.db_setup = db_setup

        self.db = pyodbc.connect(dsn=self.dsn)
        self.db.autocommit = True

        self.station = None
        self.station_id = None
        self.stream_url = None

    def __enter__(self):
        return self

    def __exit__(self, tp, val, traceback):
        self.close()

    def lock_task(self):
        params = (
            self.chunk_error_threshold is None,
            self.chunk_error_threshold,
            self.chunk_error_threshold is None,
            self.chunk_error_threshold
        )

        with self.db.cursor() as cur:
            cur.execute('''
            -- SQL ported from https://github.com/chanks/que
            with recursive job_locks as
            (
                select
                    (j).*,
                    pg_try_advisory_lock((j).station_id) as locked
                from
                (
                    select
                        j
                    from app.jobs j
                    where
                        ? or
                        j.error_count < ?
                    order by station_id
                    limit 1
                ) as t1

                union all

                (
                    select
                        (j).*,
                        pg_try_advisory_lock((j).station_id) as locked
                    from
                    (
                        select
                        (
                            select
                                j
                            from app.jobs j
                            where
                                j.station_id > job_locks.station_id and
                                (
                                    ? or
                                    j.error_count < ?
                                )
                            order by station_id
                            limit 1
                        ) as j
                        from job_locks
                        where
                            job_locks.station_id is not null
                        limit 1
                    ) as t1
                )
            )
            select
                station_id
            from job_locks
            where
                locked
            limit 1;
            ''', params)

            res = cur.fetchall()
            if len(res) > 0:
                return res[0][0]
            else:
                return None

    def release_lock(self):
        with self.db.cursor() as cur:
            cur.execute('''
            select
                pg_advisory_unlock(?);
            ''', (self.station_id,))

            return cur.fetchone()[0]

    def close(self):
        try:
            self.release_lock()
        except Exception as e:
            pass

        try:
            self.db.close()
        except Exception as e:
            pass

        self.station_id = None
        self.stream_url = None

    def get_stop_conditions(self):
        with self.db.cursor() as cur:
            params = (
                self.station_id,
                self.station_id,
                self.chunk_error_threshold
            )

            cur.execute('''
            select
                not exists(
                    select
                        1
                    from app.jobs
                    where
                        station_id = ?
                ) as deleted,

                exists(
                    select
                        1
                    from app.jobs
                    where
                        station_id = ? and
                        error_count >= ?
                ) as failed;
            ''', params)

            ret = cur.fetchone()
            cols = [col[0] for col in cur.description]

            return dict(zip(cols, ret))

    def do_db_setup(self):
        logger.info('Attempting database setup')

        # So we don't have all the workers try to get the lock at once
        time.sleep(random.uniform(0, 2*self.poll_interval))

        try:
            cur = self.db.cursor()

            # use the two-argument version to avoid overlapping with
            # locks on station_id values
            cur.execute('select pg_try_advisory_lock(0, 0);')

            if not cur.fetchone()[0]:
                return # someone else got there first

            # Check this in case we wake up after someone else gets the
            # lock and does setup; if we can lock it again and we can tell
            # the work is already done, just exit
            cur.execute('''
            select
                not exists(
                    select
                        1
                    from information_schema.schemata
                    where
                        schema_name = 'app'
                ) as needs_setup;
            ''')

            if not cur.fetchone()[0]:
                logger.info("Database already set up; aborting")
                return

            tables_to_copy = (
                ('main.csv', 'data.station'),
            )

            data_source_bucket = self.db_setup['data_source_s3_bucket']
            data_source_key = self.db_setup['data_source_s3_key']

            # Set up the schema to hold data we'll be fetching
            cur.execute(self.db_setup['schema_sql'])
            logger.info("Set up database schema")

            # Fetch the data from S3 and extract it
            # => main.csv, maps.csv, map_coordinates.csv
            s3 = boto3.resource('s3')
            bucket = s3.Bucket(data_source_bucket)

            with tempfile.TemporaryDirectory() as tmpdir:
                with tempfile.NamedTemporaryFile(dir=tmpdir) as fn:
                    bucket.download_file(data_source_key, fn.name)
                    with tarfile.open(fn.name, 'r:gz') as tar:
                        tar.extractall(path=tmpdir)

                # Load the data we've extracted into the DB
                for fname, tbl in tables_to_copy:
                    pth = os.path.join(tmpdir, fname)

                    with open(pth, 'rt') as f:
                        reader = csv.reader(f, dialect='excel-tab')
                        cols = next(reader)

                        # NOTE: All this sql string munging isn't great, but
                        # the assumption is we do control the input data
                        placeholders = (tbl, ','.join(cols),
                                        ','.join(['?'] * len(cols)))

                        while cur.nextset():
                            pass

                        cur.executemany('''
                        insert into %s
                            (%s)
                        values
                            (%s)
                        ''' % placeholders, reader)

                        logger.info("Copied %s from %s" % (tbl, fname))

            logger.info('Database successfully set up')
        except Exception as e:
            logger.exception("Failed to set up database")

            try:
                self.db.rollback()
            except Exception as e:
                logger.exception("Failed to roll back database set up")

            raise
        else:
            try:
                self.db.commit()
            except Exception as e:
                logger.exception("Failed to commit database set up")
        finally:
            try:
                cur.close()
            except Exception as e:
                pass

    def acquire_task(self):
        # in a high-concurrency situation,
        # spread out the load on the DB
        time.sleep(random.uniform(0, 2*self.poll_interval))

        # Use a spinlock; if there's nothing to work on, let's
        # wait around and keep checking if there is
        while True:
            res = self.lock_task()
            if res is None: # nothing to lock
                logger.debug('Nothing to work on; spinning')
                time.sleep(self.poll_interval)
                continue
            else:
                break
        self.station_id = res

        with self.db.cursor() as cur:
            cur.execute('''
            select
                callsign || '-' || band as station,
                stream_url
            from data.station
            where
                station_id = ?;
            ''', (self.station_id,))

            res = cur.fetchone()
            self.station = res[0]
            self.stream_url = res[1]

        return self

    def run(self):
        if self.create_schema:
            self.do_db_setup()

        self.acquire_task()

        msg = "Began ingesting station_id %s from %s"
        vals = (self.station_id, self.stream_url)
        logger.info(msg % vals)

        args = {
            'url': self.stream_url,
            'chunk_size': self.chunk_size
        }

        s3 = boto3.client('s3')
        stream, it = None, None

        while True:
            conds = self.get_stop_conditions()
            if conds['deleted']:
                msg = "Job %s cancelled"
                vals = (self.station_id,)
                raise ex.JobCancelledException(msg % vals)
            elif conds['failed']:
                msg = "Job %s had too many failures"
                vals = (self.station_id,)
                raise ex.TooManyFailuresException(msg % vals)

            try:
                # do this rather than "for chunk in stream" so that
                # we can get everything inside the try block
                if stream is None:
                    stream = AudioStream(**args)
                    it = iter(stream)

                chunk = next(it)

                # Put it into S3
                tm = str(int(time.time() * 1000000))
                key = os.path.join(self.s3_prefix, self.station, tm)

                with io.BytesIO(chunk) as f:
                    s3.upload_fileobj(f, self.s3_bucket, key)

                # Log the success
                msg = 'Successfully fetched and uploaded %s'
                s3_url = 's3://' + self.s3_bucket + '/' + key
                logger.info(msg % (s3_url,))
            except Exception as e:
                with self.db.cursor() as cur:
                    # log the failure; this is concurency-safe because
                    # we have the lock on this station_id
                    cur.execute('''
                    update app.jobs
                    set
                        error_count = error_count + 1,
                        last_error = ?
                    where
                        station_id = ?;
                    ''', (str(sys.exc_info()), self.station_id))

                if isinstance(e, StopIteration):
                    raise # no point continuing after we hit this
                elif self.chunk_error_behavior == 'exit':
                    raise
                else:
                    logger.exception('Chunk failed; ignoring')
            else:
                # log the success
                with self.db.cursor() as cur:
                    cur.execute('''
                    insert into app.chunks
                        (station_id, s3_url)
                    values
                        (?, ?);
                    ''', (self.station_id, s3_url))
            finally:
                gc.collect()

                try:
                    stream.close()
                except Exception as e:
                    pass

        return self

