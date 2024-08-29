#!/usr/bin/env python

import os
import re
import time
import random
import shutil
import logging
import urlparse
import datetime
import sqlite3 as sq
from StringIO import StringIO

import us
import boto3
import requests as rq

from dateutil import parser as dtp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

class NPRScraper(object):
    def __init__(self, dbpath, audio_dir='.', initdb=False, min_cutoff_dt='2010-01-01',
                 max_cutoff_dt=None):
        self.audio_dir = audio_dir

        self.min_cutoff_dt = dtp.parse(min_cutoff_dt).date()
        
        if max_cutoff_dt is None:
            self.max_cutoff_dt = datetime.date.today()
        else:
            self.max_cutoff_dt = dtp.parse(max_cutoff_dt).date()

        if dbpath == ':memory:':
            raise ValueError("In-memory sqlite databases are not allowed")
        else:
            self.dbpath = dbpath
            self.db = sq.connect(dbpath)

            if initdb:
                # This is a separate method so that you can call it again
                # to refresh the object state
                self.initdb()
            else:
                pass

    def initdb(self):
        cur = self.db.cursor()

        cur.executescript('''
        drop table if exists program;
        create table program
        (
            program_id integer primary key,

            name text not null,

            constraint name_unique unique (name)
        );
        
        drop table if exists program_show;
        create table program_show
        (
            program_show_id integer primary key,

            show_date text not null,
            npr_id text not null,
            url text not null,

            program_id integer not null,
            foreign key(program_id) references program(program_id),
            
            constraint npr_id_unique unique (npr_id),
            constraint url_unique unique (url)
        );

        drop table if exists program_show_segment;
        create table program_show_segment
        (
            program_show_segment_id integer primary key,

            processed integer not null default 0,
            successful integer not null default 0,
            url text not null,
            
            audio_length_in_seconds integer,
            audio_path text,
            transcript text,
            
            program_show_id integer not null,
            foreign key(program_show_id) references program_show(program_show_id),
            
            constraint url_unique unique (url),
            check(processed in (0, 1)),
            check(successful in (0, 1))
        );
        ''')

    def programs(self):
        cur = self.db.cursor()

        cur.execute('''
        select
            name
        from program;
        ''')

        return map(lambda x: x[0], cur.fetchall())

    def max_id_for_table(self, table):
        cur = self.db.cursor()
        
        cur.execute('''
        select
            max({0}_id)
        from {0};
        '''.format(table))
        
        max_id = cur.fetchone()[0]
        if max_id is None:
            max_id = 0

        return max_id

    def add_programs(self, programs, duplicates='pass'):
        cur = self.db.cursor()
        
        max_id = self.max_id_for_table('program')

        vals = map(lambda x: (max_id + x[0], x[1]), enumerate(programs))
        
        try:
            cur.executemany('''
            insert into program
                (program_id, name)
            values
                (?, ?);
            ''', vals)
        except sq.IntegrityError:
            if duplicates == 'fail':
                raise
            else:
                pass

    def has_program(self, program):
        cur = self.db.cursor()

        cur.execute('''
        select
            1
        from program
        where
            name = ?;
        ''', (program,))

        if len(cur.fetchall()) > 0:
            return True
        else:
            return False

    def has_program_show(self, npr_id):
        cur = self.db.cursor()

        cur.execute('''
        select
            1
        from program_show
        where
            npr_id = ?;
        ''', (npr_id,))

        if len(cur.fetchall()) > 0:
            return True
        else:
            return False

    def has_program_show_segment(self, program_show_segment_id):
        cur = self.db.cursor()

        cur.execute('''
        select
            1
        from program_show_segment
        where
            program_show_segment_id = ?;
        ''', (program_show_segment_id,))

        if len(cur.fetchall()) > 0:
            return True
        else:
            return False

    def _program_id(self, program):
        if not self.has_program(program):
            raise ValueError("Bad program %s" % (program,))

        cur = self.db.cursor()
        cur.execute('''
        select
            program_id
        from program
        where
            name = ?;
        ''', (program,))

        return cur.fetchone()[0]

    def date_range_for_program(self, program):
        if not self.has_program(program):
            raise ValueError("Bad program %s" % (program,))
        else:
            program_id = self._program_id(program)

        cur = self.db.cursor()
        cur.execute('''
        select
            min(show_date),
            max(show_date)
        from program_show
        where
            program_id = ?;
        ''', (program_id,))

        return cur.fetchone()

    def segments_for_program(self, program, processed=None, successful=None):
        cur = self.db.cursor()

        if processed is None:
            proc = "1 = 1"
        elif processed:
            proc = "pss.processed = 1"
        else:
            proc = "pss.processed = 0"
        
        if successful is None:
            succ = "1 = 1"
        elif successful:
            succ = "pss.successful = 1"
        else:
            succ = "pss.successful = 0"
        
        cur.execute('''
        select
            pss.program_show_segment_id
        from program_show_segment pss
            inner join program_show ps using(program_show_id)
            inner join program p using(program_id)
        where
            p.name = ? and
            %s and
            %s
        ''' % (proc, succ),
        (program,))

        res = cur.fetchall()
        return map(lambda x: x[0], res)

    def scrape_program(self, program, mean_wait_time=2):
        cur = self.db.cursor()
        
        if not self.has_program(program):
            raise ValueError("Bad program %s" % (program,))
        else:
            program_id = self._program_id(program)

        # Initially, we fetch a url of this form; later, we follow
        # the provided pagination links
        init_pattern = 'https://www.npr.org/programs/{0}/archive?date={1}'
        url = init_pattern.format(program, self.max_cutoff_dt)
        
        min_observed_dt = self.max_cutoff_dt
        
        while min_observed_dt >= self.min_cutoff_dt:
            time.sleep(random.uniform(0, 2*mean_wait_time))

            resp = rq.get(url)
            soup = BeautifulSoup(resp.text, 'lxml')
            logger.debug('Fetched and rendered ' + url)

            articles = soup.find_all('article', class_='program-show')

            for a in articles:
                # load the program_show row
                header = a.find('h2', class_='program-show__title')

                ps_id = self.max_id_for_table('program_show') + 1
                episode_date = dtp.parse(a['data-episode-date']).date()
                episode_id = a['data-episode-id']
                episode_url = header.a['href']
                
                if episode_date < min_observed_dt:
                    min_observed_dt = episode_date

                if self.has_program_show(episode_id):
                    continue
                if episode_date < self.min_cutoff_dt:
                    continue

                vals = (ps_id, episode_date.isoformat(), episode_id,
                        episode_url, program_id)

                try:
                    cur.execute('''
                    insert into program_show
                        (program_show_id, show_date, npr_id, url, program_id)
                    values
                        (?, ?, ?, ?, ?);
                    ''', vals)

                    # load info for each segment
                    segments_list = a.find('section', class_='program-show__segments')
                    segments = segments_list.find_all('article', class_='program-segment')
                    for s in segments:
                        pss_id = self.max_id_for_table('program_show_segment') + 1
                        
                        header = s.find_all('h3', class_='program-segment__title')[0]
                        vals = (pss_id, header.a['href'], ps_id)

                        cur.execute('''
                        insert into program_show_segment
                            (program_show_segment_id, url, program_show_id)
                        values
                            (?, ?, ?);
                        ''', vals)
                except:
                    self.db.rollback()
                else:
                    self.db.commit()

                logger.info('Loaded article %s from %s with %s segments' % (episode_id, episode_date, len(segments)))

            # Finally, get the infinite scroll link and follow it
            link = soup.find('div', id='scrolllink').a['href']
            url = urlparse.urljoin('https://www.npr.org', link)
        
        return

    @staticmethod
    def get_transcript_from_soup(soup):
        div = soup.findAll('div', class_='transcript')[0]
        strs = div.find_all('p')
        for (i, s) in enumerate(strs):
            try:
                if s['class'] == 'disclaimer':
                    strs.pop(i)
            except: # non-disclaimer text has no class attr
                pass
        ts = '\n'.join(map(lambda x: x.text, strs))
        
        return ts

    def scrape_program_show_segment(self, program_show_segment_id):
        cur = self.db.cursor()

        # Get the url, or die if this segment doesn't exist
        cur.execute('''
        select
            url
        from program_show_segment
        where
            program_show_segment_id = ?;
        ''', (program_show_segment_id,))
        res = cur.fetchone()
        if len(res) == 0:
            raise ValueError("No such program show segment: %s" % (program_show_segment_id,))
        else:
            url = res[0]

        resp = rq.get(url)
        soup = BeautifulSoup(resp.text, "lxml")
        logger.debug('Fetched and rendered ' + url)

        successful = 1 #set to 0 later if things go wrong
        
        # Get the transcript
        try:
            ts = self.get_transcript_from_soup(soup)
        except: # didn't work, try to find a transcript link and handle it
            try:
                ts_url = soup.find('li', class_='audio-tool-transcript').a['href']
                ts_resp = rq.get(ts_url)
                ts_soup = BeautifulSoup(ts_resp.text, "lxml")
                logger.debug('Fetched and rendered ' + ts_url)

                ts = self.get_transcript_from_soup(ts_soup)
            except: # still didn't work
                successful = 0
                ts = '' # hasn't been transcribed, esp true of today's pieces
                logger.exception('Failed to save transcript')
        
        # Get the audio length
        try:
            tm = soup.find_all('time', class_='audio-module-duration')[0].text
            (mins, secs) = tm.split(':')
            audio_length = 60 * mins + secs
        except:
            audio_length = None

        # Get and save the audio file
        try:
            audio_url = soup.findAll('li', class_='audio-tool-download')[0].a['href']
            try:
                ext = os.path.splitext(urlparse.urlparse(audio_url)[2])[1]
            except:
                ext = 'unknown'
            audio_path = os.path.abspath(os.path.join(self.audio_dir, str(program_show_segment_id) + ext))

            r = rq.get(audio_url, stream=True)
            with open(audio_path, 'wb') as f:
                shutil.copyfileobj(r.raw, f)
        except:
            audio_path = ''
            successful = 0
            logger.exception('Failed to save audio file')

        vals = (successful, audio_length, audio_path, ts, program_show_segment_id)

        try:
            cur.execute('''
            update program_show_segment
            set
                processed = 1,
                successful = ?,
                
                audio_length_in_seconds = ?,
                audio_path = ?,
                transcript = ?
            where
                program_show_segment_id = ?;
            ''', vals)
        except:
            self.db.rollback()
        else:
            self.db.commit()

        if successful == 1:
            msg = "Successfully scraped program show segment at %s"
        else:
            msg = "Unsuccessfully scraped program show segment at %s"
            
        logger.info(msg % (url,))
        return

    def scrape(self, how='all'):
        valid = ('all', 'programs', 'unprocessed_segments', 'failed_segments')
        if how not in valid:
            raise ValueError("Invalid value %s for argument how" % (how,))
        
        for program in self.programs():
            if how in ('all', 'programs'):
                self.scrape_program(program)

            if how in ('all', 'unprocessed_segments', 'failed_segments'):
                args = {'program': program}
                
                if how == 'all':
                    args['processed'] = None
                    args['successful'] =  None
                elif how == 'unprocessed_segments':
                    args['processed'] = 0
                    args['successful'] = None
                else:
                    args['processed'] = 1
                    args['successful'] = 0

                segments = self.segments_for_program(**args)
                for s in segments:
                    self.scrape_program_show_segment(s)

        return

    # AWS credentials are assumed to be in the environment
    def s3_upload(self, bucket, prefix=''):
        cur = self.db.cursor()

        cur.execute('''
        select
            program_show_segment_id
        from program_show_segment pss
        where
            processed = 1 and
            successful = 1;
        ''')

        ids = map(lambda x: x[0], cur.fetchall())

        for i in ids:
            self.s3_upload_segment(i, bucket, prefix)

        return

    def s3_upload_segment(self, program_show_segment_id, bucket, prefix=''):
        cur = self.db.cursor()

        if not self.has_program_show_segment(program_show_segment_id):
            raise ValueError("Bad segment id " + str(program_show_segment_id))

        # Get the audio file and transcript - we'll defer processing the
        # transcript into a WER-friendly format until it's time to actually
        # compute the error rates
        cur.execute('''
        select
            audio_path,
            transcript
        from program_show_segment
        where
            program_show_segment_id = ?;
        ''', (program_show_segment_id,))
        res = cur.fetchone()
        audio_path, transcript = res[0], res[1]

        # Upload to S3
        bs = os.path.basename(audio_path)
        audio_key = os.path.join(prefix, bs)
        transcript_key = os.path.join(prefix, bs + '.transcript')
        
        s3 = boto3.client('s3')
        s3.upload_file(audio_path, bucket, audio_key)
        s3.upload_fileobj(StringIO(transcript.encode('utf-8')), bucket, transcript_key)

        # Log it
        msg = 'Uploaded audio and transcript for segment %s to %s'
        vals = (str(program_show_segment_id),
                's3://' + os.path.join(bucket, prefix))
        logger.info(msg % vals)

        return

