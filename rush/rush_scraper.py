import os
import re
import time
import json
import random
import shutil
import logging
import urlparse
import datetime
import subprocess
import tempfile
import sqlite3 as sq
from StringIO import StringIO

import us
import m3u8
import boto3
import requests as rq

from dateutil import parser as dtp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

class RushScraper(object):
    def __init__(self, dbpath, initdb=False):
        if dbpath == ':memory:':
            raise ValueError("In-memory sqlite databases are not allowed")
        
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
        drop table if exists stg_episode;
        create table stg_episode
        (
            stg_episode_id integer primary key,

            url text not null,
            html text not null,
            published_dt text not null,

            constraint url_unique unique (url)
        );

        drop table if exists episode;
        create table episode
        (
            episode_id integer primary key,
            stg_episode_id integer not null,
            
            rush_show_id integer,
            rush_group_id integer,
            rush_episode_id integer,

            transcript text,
            
            media_url text,
            api_response_json text,
            media_local_path text,

            foreign key (stg_episode_id) references stg_episode(stg_episode_id),
            constraint media_url_unique unique (media_url),
            constraint stg_episode_id_unique unique (stg_episode_id)
        );
        ''')

    def has_stg_episode_url(self, url):
        cur = self.db.cursor()

        cur.execute('''
        select
            1
        from stg_episode
        where
            url = ?;
        ''', (url,))

        return len(cur.fetchall()) > 0
 
    def has_stg_episode_id(self, stg_episode_id):
        cur = self.db.cursor()

        cur.execute('''
        select
            1
        from stg_episode
        where
            stg_episode_id = ?;
        ''', (stg_episode_id,))

        return len(cur.fetchall()) > 0
 
    def has_episode_id(self, episode_id):
        cur = self.db.cursor()

        cur.execute('''
        select
            1
        from episode
        where
            episode_id = ?;
        ''', (episode_id,))

        return len(cur.fetchall()) > 0
 
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

    def spider(self, cutoff_dt='2010-01-01', mean_wait_time=2):
        cur = self.db.cursor()
        
        if isinstance(cutoff_dt, basestring):
            cutoff_dt = dtp.parse(cutoff_dt).date()

        # Initially we fetch this url, and later we follow the provided
        # pagination links
        url = 'https://www.rushlimbaugh.com/archives/'
        
        min_observed_dt = datetime.date.today()
        while min_observed_dt >= cutoff_dt:
            time.sleep(random.uniform(0, 2*mean_wait_time))

            resp = rq.get(url)
            soup = BeautifulSoup(resp.text, 'lxml')
            logger.debug('Fetched and rendered ' + url)

            # The articles we're processing from the just-fetched page
            mc = soup.find('div', id='main-content')
            articles = mc.find_all('article', class_='post')

            for ar in articles:
                # Get the values we need to insert
                se_id = self.max_id_for_table('stg_episode') + 1
                
                article_dt = dtp.parse(ar.p.span.text).date()
                article_url = ar.h2.a['href']
                article_html = rq.get(article_url).text
                logger.debug('Fetched ' + article_url)

                # if already loaded, don't try to insert again
                if self.has_stg_episode_url(article_url):
                    continue
                
                # ensure we don't go back further than the desired
                # cutoff date
                if article_dt < min_observed_dt:
                    min_observed_dt = article_dt
                if article_dt < cutoff_dt:
                    break

                try:
                    vals = (se_id, article_url, article_html,
                            article_dt.isoformat())

                    cur.execute('''
                    insert into stg_episode
                        (stg_episode_id, url, html, published_dt)
                    values
                        (?, ?, ?, ?);
                    ''', vals)
                except:
                    self.db.rollback()
                    logger.exception('Failed to load article row')
                else:
                    self.db.commit()

                msg = 'Loaded article from %s at %s'
                logger.info(msg % (article_dt.isoformat(), article_url))

            # The page we should fetch next at the top of the loop
            url = soup.find('div', class_='pagination').a['href']
        
        return

    @staticmethod
    def get_transcript_from_soup(soup):
        ps = soup.find('article').find('div', class_='entry-content').find_all('p')

        strs = map(lambda x: x.get_text(), ps)

        return '\n'.join(strs)

    def process(self, audio_dir='.', allow_audio_failure=False,
                mean_wait_time=2, mode='unprocessed'):
        cur = self.db.cursor()

        if mode not in ('unprocessed', 'reprocess'):
            raise ValueError("Must have mode in ('unprocessed', 'reprocess')")
        
        if mode == 'unprocessed':
            cur.execute('''
            select
                se.stg_episode_id
            from stg_episode se
                left join episode e using(stg_episode_id)
            where
                e.stg_episode_id is null
            order by se.published_dt desc;
            ''')
        else:
            cur.execute('delete from episodes;')
            self.db.commit()
            
            cur.execute('select stg_episode_id from stg_episode order by published_dt desc;')

        ids = map(lambda x: x[0], cur.fetchall())
        
        for i in ids:
            time.sleep(random.uniform(0, 2*mean_wait_time))
            
            self.process_stg_episode(i, audio_dir,allow_audio_failure)

        return

    def process_stg_episode(self, stg_episode_id, audio_dir='.',
                            allow_audio_failure=False):
        cur = self.db.cursor()

        if not self.has_stg_episode_id(stg_episode_id):
            raise ValueError("Bad stg_episode " + str(stg_episode_id))

        episode_id = self.max_id_for_table('episode') + 1
        
        # Get our cached copy of the page
        cur.execute('''
        select
            url,
            html
        from stg_episode
        where
            stg_episode_id = ?;
        ''', (stg_episode_id,))

        res = cur.fetchone()
        url, html = res[0], res[1]
        soup = BeautifulSoup(html, 'lxml')
        
        try:
            # Extract the transcript
            try:
                transcript = self.get_transcript_from_soup(soup)
            except AttributeError:
                logger.warning('Could not find a transcript on %s' % (url,))
                return

            # Find the video page URL we're going to use, first one way
            # and then another for two different ways they seem to have
            # put these links into pages
            media_url = None
            
            # way #1
            links = soup.find('div', class_='custom-links-post').find_all('a')
            for lk in links:
                if re.compile('listen', re.I).search(lk.img['src']):
                    media_url = lk['href']
                    break # take the first one we get
                elif re.compile('watch', re.I).search(lk.img['src']):
                    media_url = lk['href']
                    break # take the first one we get
                else:
                    pass
            
            # way #2
            if media_url is None:
                imgs = soup.find('div', class_='entry-content').find_all('img')
                lb = filter(lambda x: re.compile('listen', re.I).search(x['src']), imgs)

                if len(lb) > 0 and lb[0].parent.name == 'a':
                    media_url = lb[0].parent['href']
            
            if media_url is None:
                logger.warning('Could not find a media page link on %s' % (url,))
                return
            
            # Parse it into the (show id, group id, episode id) we need, trying
            # several different ways of doing it because this godawful site has
            # several different URL formats for its audio/video links
            rush_show_id = 4 # seems to be the only value Rush's site uses
            
            (scheme, host, path, params, query, frag) = urlparse.urlparse(media_url)
            
            rx1 = re.compile('\!?/([0-9])+/([0-9]+)(/.*)?', re.I)
            match1 = rx1.match(frag)

            rx2 = re.compile('/videos/([0-9])+/([0-9]+)(/.*)?', re.I)
            match2 = rx2.match(path)
            
            # this is - no, really, it really is - for links of the form
            #   https://videos/XXXX/YYYYY
            rx3 = re.compile('/([0-9])+/([0-9]+)(/.*)?', re.I)
            match3 = rx3.match(path)
            
            if match1:
                match = match1
            elif match2:
                match = match2
            elif match3:
                match = match3
            else:
                logger.warning('Could not process media page link on %s' % (url,))
                return

            rush_group_id = match.group(1)
            rush_episode_id = match.group(2)
            
            # Rush has an ajax endpoint for fetching real media URLs, and it doesn't
            # require any authentication. GET it with a (show id, group id, episode id)
            # triple and it'll send you back json containing among other things the url
            # of an m3u8 playlist for the episode, hosted on Akamai's cdn. The
            # show ID seems to always be 4, and the group id + episode id are embedded in
            # the url for the view page (https://host/videos/#!/GROUP_ID/EPISODE_ID). It's
            # possible all three are also provided in the page html.
            endpoint = 'https://www.rushlimbaugh.com/wp-admin/admin-ajax.php?action=ampmedia_get_episode_feed&showId={0}&groupId={1}&episodeId={2}'

            resp = rq.get(endpoint.format(rush_show_id, rush_group_id,
                                          rush_episode_id))
            try:
                rj = json.loads(resp.text)
                mcs = rj['channel']['item']['media-group']['media-content']
                
                flt = lambda x: x['@attributes']['type'] == 'application/x-mpegURL'
                srcs = filter(flt, mcs)
                if len(srcs) == 0:
                    logger.warning('API response had no usable media on %s' % (url,))
                    return
                else:
                    src = srcs[0]['@attributes']['url']
                
                playlist = rq.get(src).text
                playlist_file = tempfile.NamedTemporaryFile(suffix='.m3u8')
                playlist_file.write(playlist)
                playlist_file.seek(0, 0)

                mp4_cmd = 'ffmpeg -y -protocol_whitelist file,http,https,tcp,tls,crypto -i {0} -c copy -bsf:a aac_adtstoasc {1}'
                mp4_file = tempfile.NamedTemporaryFile(suffix='.mp4')
                
                with open(os.devnull, 'wb') as devnull:
                    subprocess.check_call(mp4_cmd.format(playlist_file.name, mp4_file.name),
                                          shell=True, stdout=devnull, stderr=devnull)

                aac_cmd = 'ffmpeg -y -i {0} -vn -acodec copy {1}'
                media_local_path = os.path.abspath(os.path.join(audio_dir, str(episode_id) + '.aac'))
                with open(os.devnull, 'wb') as devnull:
                    subprocess.check_call(aac_cmd.format(mp4_file.name, media_local_path),
                                          shell=True, stdout=devnull, stderr=devnull)
            except:
                if allow_audio_failure:
                    media_local_path = ''
                else:
                    raise

            # Load our newly processed row into the sqlite db
            vals = (episode_id, stg_episode_id, rush_show_id, rush_group_id,
                    rush_episode_id, transcript, media_url, resp.text,
                    media_local_path)
            try:
                cur.execute('''
                insert into episode
                    (episode_id, stg_episode_id, rush_show_id, rush_group_id,
                     rush_episode_id, transcript, media_url, api_response_json,
                     media_local_path)
                values
                    (?, ?, ?, ?, ?, ?, ?, ?, ?);
                ''', vals)
            except:
                self.db.rollback()
                raise
            else:
                self.db.commit()
        except:
            logger.exception('Unsuccessfully processed ' + url)
        else:
            logger.info('Successfully processed ' + url)

        return

    # AWS credentials are assumed to be in the environment
    def s3_upload(self, bucket, prefix=''):
        cur = self.db.cursor()

        cur.execute('''
        select
            episode_id
        from episode;
        ''')

        ids = map(lambda x: x[0], cur.fetchall())

        for i in ids:
            self.s3_upload_episode(i, bucket, prefix)

        return

    def s3_upload_episode(self, episode_id, bucket, prefix=''):
        cur = self.db.cursor()

        if not self.has_episode_id(episode_id):
            raise ValueError("Bad episode_id " + str(episode_id))

        # Get the audio file and transcript - we'll defer processing the
        # transcript into a WER-friendly format until it's time to actually
        # compute the error rates
        cur.execute('''
        select
            media_local_path,
            transcript
        from episode
        where
            episode_id = ?;
        ''', (episode_id,))
        res = cur.fetchone()
        media_local_path, transcript = res[0], res[1]

        # Upload to S3
        bs = os.path.basename(media_local_path)
        media_key = os.path.join(prefix, bs)
        transcript_key = os.path.join(prefix, bs + '.transcript')
        
        s3 = boto3.client('s3')
        s3.upload_file(media_local_path, bucket, media_key)
        s3.upload_fileobj(StringIO(transcript.encode('utf-8')), bucket, transcript_key)

        # Log it
        msg = 'Uploaded audio and transcript for episode %s to %s'
        vals = (str(episode_id), 's3://' + os.path.join(bucket, prefix))
        logger.info(msg % vals)

        return

