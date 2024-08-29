#!/usr/bin/env python

import logging
import argparse

from npr_scraper import NPRScraper

logger = logging.getLogger(__name__)

#PROGRAMS = ['all-things-considered', 'morning-edition', 'talk-of-the-nation', 'fresh-air']

def parse_args():
    parser = argparse.ArgumentParser(description='Scrape NPR audio data')
    subparsers = parser.add_subparsers(help='scrape-related subcommands',
                                       dest='subcommand')

    initdb_parser = subparsers.add_parser('initdb')
    scrape_parser = subparsers.add_parser('scrape')
    s3upload_parser = subparsers.add_parser('s3upload')

    # Common arguments
    for p in [initdb_parser, scrape_parser, s3upload_parser]:
        p.add_argument('-f', '--dbfile', required=True, help='Target file path for sqlite db')
        p.add_argument('--debug', action='store_true', help='More verbose logging output')
    
    # Scraping args
    scrape_parser.add_argument('program', nargs='+', help='NPR programs to scrape')
    scrape_parser.add_argument('-d', '--audio-dir', default='.', help='Directory to write audio files')
    scrape_parser.add_argument('-i', '--min-cutoff-dt', default='2010-01-01', help='Oldest data to scrape')
    scrape_parser.add_argument('-a', '--max-cutoff-dt', default='2010-01-01', help='Newest data to scrape')
    scrape_parser.add_argument('--how', default='all', nargs='?',
                               choices=['all', 'programs', 'unprocessed_segments', 'failed_segments'],
                               help='Scraping mode')

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

    init_args = {
        'dbpath': args.dbfile,
        'audio_dir': args.audio_dir if hasattr(args, 'audio_dir') else None,
        'min_cutoff_dt': args.min_cutoff_dt if hasattr(args, 'min_cutoff_dt') else '2010-01-01',
        'max_cutoff_dt': args.max_cutoff_dt if hasattr(args, 'max_cutoff_dt') else None
    }

    scraper = NPRScraper(**init_args)

    if args.subcommand == 'initdb':
        scraper.initdb()
    elif args.subcommand == 'scrape':
        scraper.add_programs(args.program, duplicates='pass')
        scraper.scrape(how=args.how)
    elif args.subcommand == 's3upload':
        scraper.s3_upload(args.bucket, args.prefix)

