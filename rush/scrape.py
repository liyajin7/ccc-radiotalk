#!/usr/bin/env python

import logging
import argparse

from rush_scraper import RushScraper

logger = logging.getLogger(__name__)

def parse_args():
    parser = argparse.ArgumentParser(description='Scrape Rush Limbaugh audio data')
    subparsers = parser.add_subparsers(help='scrape-related subcommands',
                                       dest='subcommand')

    initdb_parser = subparsers.add_parser('initdb')
    spider_parser = subparsers.add_parser('spider')
    process_parser = subparsers.add_parser('process')
    s3upload_parser = subparsers.add_parser('s3upload')

    # Common arguments
    for p in [initdb_parser, spider_parser, process_parser, s3upload_parser]:
        p.add_argument('-f', '--dbfile', required=True, help='Target file path for sqlite db')
        p.add_argument('--debug', action='store_true', help='More verbose logging output')
    
    # Spidering args
    spider_parser.add_argument('-c', '--cutoff-dt', default='2010-01-01', help='Oldest data to scrape')
    
    # Processing args
    process_parser.add_argument('-m', '--mode', default='unprocessed',
                                choices=['unprocessed', 'reprocess'],
                                help='Directory to write audio files')
    process_parser.add_argument('-d', '--audio-dir', default='.', help='Directory to write audio files')
    process_parser.add_argument('--allow-audio-failure', action='store_true',
                                help='Allow processing to succeed even if fetching audio file fails')

    # S3 parser - we're assuming creds are in the environment
    s3upload_parser.add_argument('-b', '--bucket', help='S3 bucket to write to')
    s3upload_parser.add_argument('-r', '--prefix', default='', help='Prefix in the S3 bucket')

    return parser.parse_args()

if __name__ == '__main__':
    args = parse_args()

    if args.debug:
        loglevel = logging.DEBUG
    else:
        loglevel = logging.INFO

    fmt = '%(asctime)s %(name)-12s %(levelname)-8s %(message)s'
    logging.basicConfig(level=loglevel, format=fmt,
                        datefmt='%Y-%m-%d %H:%M:%S')

    scraper = RushScraper(dbpath=args.dbfile)

    if args.subcommand == 'initdb':
        scraper.initdb()
    elif args.subcommand == 'spider':
        scraper.spider(cutoff_dt=args.cutoff_dt)
    elif args.subcommand == 'process':
        scraper.process(audio_dir=args.audio_dir, mode=args.mode,
                        allow_audio_failure=args.allow_audio_failure)
    elif args.subcommand == 's3upload':
        scraper.s3_upload(args.bucket, args.prefix)

