import os
import sys
import re
import json
import logging
import tempfile
import itertools as it
import subprocess as sp
import configparser as cp
import urllib.parse as urlparse
import mimetypes as mt

import bs4
import m3u8
import requests as rq

import exceptions as ex

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ' \
                     'AppleWebKit/537.36 (KHTML, like Gecko) ' \
                     'Chrome/111.0.0.0 Safari/537.36'

def grouper(iterable, n):
    'Collect data into fixed-length chunks or blocks'
    assert n > 0

    ret = []

    for obj in iterable:
        if len(ret) == n:
            yield b''.join(ret)
            ret = []

        if len(ret) < n:
            ret += [obj]

    # at this point, we're out of
    # objects but len(ret) < n
    if len(ret) > 0:
        yield b''.join(ret)

# AudioStream represents 'what to do' and this class
# represents 'how to do it'. Fetching and parsing logic
# is here; chunk sizes, retry configuration, the actual
# URL to fetch from, etc, are in AudioStream.
class MediaIterator(object):
    def __init__(self, **kwargs):
        try:
            self.stream = kwargs.pop('stream')
        except KeyError:
            raise ValueError("Must pass stream object")

        self.timeout = kwargs.pop('timeout', 10)
        self.user_agent = kwargs.pop('user_agent', DEFAULT_USER_AGENT)

        super(MediaIterator, self).__init__(**kwargs)

        # We should assume that when these objects are created, we're
        # at the top of some loop, so there's no need to suspend
        # network I/O for later
        self.session = rq.Session()
        self._refresh()

        self.retry_error_cnt = 0

    def close(self):
        try:
            self.session.close()
        except Exception as e:
            pass

    def _refresh(self):
        raise NotImplementedError("Subclasses must define _refresh")

    def __iter__(self):
        return self

    def __next__(self):
        while self.retry_error_cnt <= self.stream.retry_error_max:
            try:
                return next(self.content)
            except rq.exceptions.RequestException as e:
                logger.exception("Failed to get next chunk")
                self.retry_error_cnt += 1

                if self.retry_error_cnt <= self.stream.retry_error_max:
                    self._refresh()
                else:
                    raise
            except StopIteration:
                if self.stream.retry_on_close:
                    self._refresh()
                    continue
                else:
                    raise

    def _fetch_url_stream_safe(self, url=None, max_size=2**20):
        # This method is called by subclasses which expect self.stream.url
        # to be a short text file (playlist or web page), but need to be
        # robust to the possibility of server misconfiguration actually
        # returning an audio stream. Trying to fetch the whole thing would
        # cause the process to hang and eventually lead to out-of-memory
        # errors. Instead we'll fetch it as a stream and return only the
        # first max_size decoded bytes.
        if url is None:
            url = self.stream.url

        headers = {'User-Agent': self.user_agent}
        resp = self.session.get(url, stream=True, timeout=self.timeout,
                                headers=headers)

        if not resp.ok:
            resp.raise_for_status()

        txt = resp.raw.read(max_size + 1, decode_content=True)

        if len(txt) > max_size:
            msg = 'Too large a response - is it actually an audio file?'
            raise ValueError(msg)
        else:
            return txt

    next = __next__

class DirectStreamIterator(MediaIterator):
    def _refresh(self):
        headers = {'User-Agent': self.user_agent}
        self.conn = self.session.get(self.stream.url, stream=True,
                                     timeout=self.timeout, headers=headers)
        self.content = self.conn.iter_content(chunk_size=self.stream.chunk_size)

class PlaylistIterator(MediaIterator):
    def _get_component_urls(self, txt):
        msg = "Subclasses must implement _get_component_urls"
        raise NotImplementedError(msg)

    def _refresh(self):
        # get the URLs
        txt = self._fetch_url_stream_safe(max_size=2**16)
        comps = self._get_component_urls(txt.decode())

        # make streams out of them
        args = dict(self.stream.args, unknown_formats='direct')

        # Don't propagate this setting down to children, for this class
        # only. If we do propagate it, playlists with multiple segments
        # won't read correctly: we'll be stuck on the first segment forever
        # after it closes, reopening and repeatedly reading it.
        if 'retry_on_close' in args.keys():
            args['retry_on_close'] = False

        components = [AudioStream(**dict(args, url=x)) for x in comps]

        self.content = grouper(it.chain(*components), self.stream.chunk_size)

class AsxIterator(PlaylistIterator):
    def _get_component_urls(self, txt):
        soup = bs4.BeautifulSoup(txt)
        hrefs = [ x['href'] for x in soup.find_all('ref') ]

        return hrefs

class PlsIterator(PlaylistIterator):
    def _get_component_urls(self, txt):
        prs = cp.ConfigParser(interpolation=None)
        prs.read_string(txt)

        # Section names are case sensitive by default, so we
        # need to find the case that 'playlist' actually has
        sections = prs.sections()
        matches = [ re.search('playlist', x, re.I) for x in sections ]
        matched = [ x is not None for x in matches ]
        ind = matched.index(True)
        key = sections[ind]

        keys = [ x for x in prs[key].keys() if x[0:4] == 'file' ]
        urls = [ prs['playlist'][x] for x in keys ]

        return urls

class M3uIterator(PlaylistIterator):
    def _get_component_urls(self, txt, i=0):
        if i >= 10:
            raise ex.IngestException("m3u playlists nested too deeply")

        pls = m3u8.loads(txt)

        if not pls.is_variant:
            if len(pls.segments) == 0:
                # this seems to be a bug in the m3u8 package
                # which we'll try to work around
                segs = [line.strip() for line in txt.split('\n') if line.strip() != '']
                for seg in segs:
                    pls.add_segment(m3u8.Segment(uri=seg, base_uri=pls.base_uri))

            urls = [x.uri for x in pls.segments]
        else:
            urls = []
            for subpls in pls.playlists:
                subtxt = self._fetch_url_stream_safe(subpls.uri)
                urls += self._get_component_urls(subtxt.decode(), i=i+1)

        return urls

class WebscrapeIterator(MediaIterator):
    retry_on_close = False

    def _webscrape_extract_media_url(self, txt):
        msg = "Subclasses must implement _webscrape_extract_media_url"
        raise NotImplementedError(msg)

    def _refresh(self):
        txt = self._fetch_url_stream_safe(max_size=2**20)
        url = self._webscrape_extract_media_url(txt)

        # we'll just proxy for an iterator on the real stream
        args = dict(self.stream.args)
        args['url'] = url

        if self.retry_on_close:
            args['retry_on_close'] = True

        s = AudioStream(**args, unknown_formats='direct')

        if s.is_playlist:
            if s.is_asx:
                self.content = AsxIterator(stream=s)
            if s.is_pls:
                self.content = PlsIterator(stream=s)
            if s.is_m3u:
                self.content = M3uIterator(stream=s)
        elif s.is_webscrape:
            raise ex.IngestException("WebscrapeIterators may not be nested")
        else: # fallback to streaming
            self.content = DirectStreamIterator(stream=s)

class IHeartIterator(WebscrapeIterator):
    retry_on_close = True

    def _webscrape_extract_media_url(self, txt):
        # There's a chunk of json in the page with our URLs in it
        soup = bs4.BeautifulSoup(txt, 'lxml')
        script = soup.find_all('script', id='initialState')[0].text

        # Get the specific piece of json with the urls of interest
        js = json.loads(script)
        stations = js['live']['stations']
        key = list(stations.keys())[0]
        streams = stations[key]['streams']
        urls = streams.values()

        # Decide which to return
        try:
            assert len(urls) > 0

            fmts = [
                'flv', 'mp3', 'aac', 'wma', 'ogg', 'wav', 'flac', # direct streams
                'm3u8', 'm3u', 'pls', 'asx' #playlists, try second
            ]

            ret = None
            for fmt in fmts:
                matches = [x for x in urls if re.search(fmt, x)]
                if len(matches) > 0:
                    ret = matches[0]
            assert ret is not None
        except AssertionError as e:
            msg = 'No usable streams could be found on %s'
            vals = (self.stream.url,)
            raise ex.IngestException(msg % vals)

        return ret

class MediaUrl(object):
    def __init__(self, **kwargs):
        try:
            self.url = kwargs.pop('url')
        except KeyError:
            raise ValueError("Must provide url")

        autodetect = kwargs.pop('autodetect', True)

        super(MediaUrl, self).__init__(**kwargs)

        ext = self._parse_ext()
        if ext is not None and ext != '':
            self._ext = ext
        elif autodetect:
            try:
                # Open a stream to it and guess by MIME type
                args = {
                    'url': self.url,
                    'stream': True,
                    'timeout': 10
                }

                with rq.get(**args) as resp:
                    mimetype = resp.headers.get('Content-Type')

                    if mimetype is None:
                        autoext = ''
                    else:
                        if ';' in mimetype:
                            mimetype = mimetype.split(';')[0]

                        autoext = mt.guess_extension(mimetype)
                        if autoext is None:
                            autoext = ''
                        else:
                            autoext = autoext[1:]

                self._ext = autoext
            except Exception as e:
                msg = "Encountered exception while guessing stream type"
                logger.warning(msg)

                self._ext = ''
        else:
            self._ext = ''

    def _parse_ext(self):
        pth = urlparse.urlparse(self.url).path
        ext = os.path.splitext(os.path.basename(pth))[1][1:]

        return ext

    ##
    ## Direct streams
    ##
    @property
    def is_mp3(self):
        return self._ext == 'mp3'

    @property
    def is_aac(self):
        return self._ext == 'aac'

    @property
    def is_wma(self):
        return self._ext == 'wma'

    @property
    def is_ogg(self):
        return self._ext == 'ogg'

    @property
    def is_wav(self):
        return self._ext == 'wav'

    @property
    def is_flac(self):
        return self._ext == 'flac'

    @property
    def is_flv(self):
        return self._ext == 'flv'

    @property
    def is_direct(self):
        return self.is_mp3 or self.is_aac or self.is_wma or \
               self.is_ogg or self.is_wav or self.is_flac or \
               self.is_flv

    ##
    ## Playlists
    ##
    @property
    def is_pls(self):
        return self._ext == 'pls'

    @property
    def is_m3u(self):
        return self._ext in ('m3u', 'm3u8')

    @property
    def is_asx(self):
        return self._ext == 'asx'

    @property
    def is_playlist(self):
        return self.is_pls or self.is_m3u or self.is_asx

    ##
    ## Things we have to webscrape
    ##
    @property
    def is_iheart(self):
        parsed = urlparse.urlparse(self.url)
        netloc, pth = parsed.netloc, parsed.path

        return (netloc == 'www.iheart.com' and pth[0:5] == '/live')

    @property
    def is_webscrape(self):
        return self.is_iheart

class AudioStream(MediaUrl):
    def __init__(self, **kwargs):
        # for use in webscrape iterators' descendant streams
        self.args = dict(kwargs)

        self.chunk_size = kwargs.pop('chunk_size', 2**20)
        self.retry_error_max = kwargs.pop('retry_error_max', 0)
        self.unknown_formats = kwargs.pop('unknown_formats', 'error')
        self.retry_on_close = kwargs.pop('retry_on_close', False)

        super(AudioStream, self).__init__(**kwargs)

        try:
            assert self.unknown_formats in ('direct', 'error')
        except AssertionError:
            msg = "unknown_formats must be 'direct' or 'error'"
            raise ValueError(msg)

        cls = self._iterator_for_stream(self)
        if cls is not None:
            self._iterator = cls(stream=self)
        elif self.unknown_formats == 'direct':
            # Fall back to trying to stream it
            self._iterator = DirectStreamIterator(stream=self)
        else:
            msg = "No iterator available for %s"
            vals = (self.url,)
            raise NotImplementedError(msg % vals)

    def __enter__(self):
        return self

    def __exit__(self, tp, val, traceback):
        self.close()

    def close(self):
        self._iterator.close()

    @staticmethod
    def _iterator_for_stream(stream):
        if stream.is_direct:
            return DirectStreamIterator
        elif stream.is_asx:
            return AsxIterator
        elif stream.is_pls:
            return PlsIterator
        elif stream.is_m3u:
            return M3uIterator
        elif stream.is_iheart:
            return IHeartIterator
        else:
            return None

    def __iter__(self):
        return self._iterator

