"""
Functions dedicated to command-line processing.
"""

## This file is available from https://github.com/adbar/trafilatura
## under GNU GPL v3 license


import logging
import random
import re
import signal
import string
import sys

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from functools import partial
from multiprocessing import Pool
from os import makedirs, path, walk
from time import sleep

from courlan import extract_domain, validate_url

from .core import extract
from .filters import content_fingerprint
from .settings import (DOWNLOAD_THREADS, FILENAME_LEN, FILE_PROCESSING_CORES,
                       MIN_FILE_SIZE, MAX_FILE_SIZE, MAX_FILES_PER_DIRECTORY,
                       PROCESSING_TIMEOUT)
from .utils import fetch_url


LOGGER = logging.getLogger(__name__)
random.seed(345)  # make generated file names reproducible


# try signal https://stackoverflow.com/questions/492519/timeout-on-a-function-call
def handler(signum, frame):
    '''Raise a timeout exception to handle rare malicious files'''
    raise Exception('unusual file processing time, aborting')


def load_input_urls(filename):
    '''Read list of URLs to process'''
    input_urls = []
    try:
        # optional: errors='strict', buffering=1
        with open(filename, mode='r', encoding='utf-8') as inputfile:
            for line in inputfile:
                url_match = re.match(r'https?://[^ ]+', line.strip())  # if not line.startswith('http'):
                try:
                    input_urls.append(url_match.group(0))
                except AttributeError:
                    LOGGER.warning('Not an URL, discarding line: %s', line)
                    continue
    except UnicodeDecodeError:
        sys.exit('ERROR: system, file type or buffer encoding')
    return input_urls


def load_blacklist(filename):
    '''Read list of unwanted URLs'''
    blacklist = set()
    with open(filename, mode='r', encoding='utf-8') as inputfh:
        for line in inputfh:
            url = line.strip()
            blacklist.add(url)
            # add http/https URLs for safety
            if url.startswith('https'):
                blacklist.add(re.sub(r'^https:', 'http:', url))
            else:
                blacklist.add(re.sub(r'^http:', 'https:', url))
    return blacklist



def check_outputdir_status(directory):
    '''Check if the output directory is within reach and writable'''
    # check the directory status
    if not path.exists(directory) or not path.isdir(directory):
        try:
            makedirs(directory, exist_ok=True)
        except OSError:
            # maybe the directory has already been created
            #sleep(0.25)
            #if not path.exists(directory) or not path.isdir(directory):
            sys.stderr.write('ERROR: Destination directory cannot be created: ' + directory + '\n')
            # raise OSError()
            return False
    return True


def determine_counter_dir(dirname, counter):
    '''Return a destination directory based on a file counter'''
    if counter is not None:
        counter_dir = str(int(counter/MAX_FILES_PER_DIRECTORY) + 1)
    else:
        counter_dir = ''
    return path.join(dirname, counter_dir)


def get_writable_path(destdir, extension):
    '''Find a writable path and return it along with its random file name'''
    charclass = string.ascii_letters + string.digits
    filename = ''.join(random.choice(charclass) for _ in range(FILENAME_LEN))
    output_path = path.join(destdir, filename + extension)
    while path.exists(output_path):
        filename = ''.join(random.choice(charclass) for _ in range(FILENAME_LEN))
        output_path = path.join(destdir, filename + extension)
    return output_path, filename


def determine_output_path(args, orig_filename, content, counter=None, new_filename=None):
    '''Pick a directory based on selected options and a file name based on output type'''
    # determine extension
    extension = '.txt'
    if args.xml or args.xmltei or args.output_format == 'xml':
        extension = '.xml'
    elif args.csv or args.output_format == 'csv':
        extension = '.csv'
    elif args.json or args.output_format == 'json':
        extension = '.json'
    # use cryptographic hash on file contents to define name
    if args.hash_as_name is True:
        new_filename = content_fingerprint(content)[:27].replace('/', '-')
    # determine directory
    if args.keep_dirs is True:
        # strip directory
        orig_directory = re.sub(r'[^/]+$', '', orig_filename)
        destination_directory = path.join(args.outputdir, orig_directory)
        # strip extension
        filename = re.sub(r'\.[a-z]{2,5}$', '', orig_filename)
        output_path = path.join(args.outputdir, filename + extension)
    else:
        destination_directory = determine_counter_dir(args.outputdir, counter)
        # determine file slug
        if new_filename is None:
            output_path, _ = get_writable_path(destination_directory, extension)
        else:
            output_path = path.join(destination_directory, new_filename + extension)
    return output_path, destination_directory


def archive_html(htmlstring, args, counter=None):
    '''Write a copy of raw HTML in backup directory'''
    destination_directory = determine_counter_dir(args.backup_dir, counter)
    output_path, filename = get_writable_path(destination_directory, '.html')
    # check the directory status
    if check_outputdir_status(destination_directory) is True:
        # write
        with open(output_path, mode='w', encoding='utf-8') as outputfile:
            outputfile.write(htmlstring)
    return filename


def write_result(result, args, orig_filename=None, counter=None, new_filename=None):
    '''Deal with result (write to STDOUT or to file)'''
    if result is None:
        return
    if args.outputdir is None:
        sys.stdout.write(result + '\n')
    else:
        destination_path, destination_directory = determine_output_path(args, orig_filename, result, counter, new_filename)
        # check the directory status
        if check_outputdir_status(destination_directory) is True:
            with open(destination_path, mode='w', encoding='utf-8') as outputfile:
                outputfile.write(result)


def generate_filelist(inputdir):
    '''Walk the directory tree and output all file names'''
    for root, _, inputfiles in walk(inputdir):
        for fname in inputfiles:
            yield path.join(root, fname)


def file_processing(filename, args, counter=None):
    '''Aggregated functions to process a file in a list'''
    with open(filename, 'rb') as inputf:
        htmlstring = inputf.read()
    result = examine(htmlstring, args, url=args.URL)
    write_result(result, args, filename, counter, new_filename=None)


def url_processing_checks(blacklist, input_urls):
    '''Filter and deduplicate input urls'''
    # control blacklist
    if blacklist:
        input_urls = [u for u in input_urls if u not in blacklist]
    # check for invalid URLs
    if input_urls:
        input_urls = [u for u in input_urls if validate_url(u)[0] is True]
    # deduplicate
    if input_urls:
        return list(OrderedDict.fromkeys(input_urls))
    LOGGER.error('No URLs to process, invalid or blacklisted input')
    return []


def process_result(htmlstring, args, url, counter):
    '''Extract text and metadata from a download webpage and eventually write out the result'''
    # backup option
    if args.backup_dir:
        fileslug = archive_html(htmlstring, args, counter)
    else:
        fileslug = None
    # process
    result = examine(htmlstring, args, url=url)
    write_result(result, args, orig_filename=None, counter=None, new_filename=fileslug)
    # increment written file counter
    if counter is not None:
        counter += 1
    return counter


def draw_backoff_url(domain_dict, backoff_dict, sleeptime, i):
    '''Select a random URL from the domains pool and apply backoff rule'''
    domain = random.choice(list(domain_dict))
    # safeguard
    if domain in backoff_dict and \
        (datetime.now() - backoff_dict[domain]).total_seconds() < sleeptime:
        i += 1
        if i >= len(domain_dict)*3:
            LOGGER.debug('spacing request for domain name %s', domain)
            sleep(sleeptime)
            i = 0
    # draw URL
    url = domain_dict[domain].pop()
    # clean registries
    if not domain_dict[domain]:
        del domain_dict[domain]
        try:
            del backoff_dict[domain]
        except KeyError:
            pass
    # register backoff
    else:
        backoff_dict[domain] = datetime.now()
    return url, domain_dict, backoff_dict, i


def single_threaded_processing(domain_dict, backoff_dict, args, sleeptime, counter):
    '''Implement a single threaded processing algorithm'''
    # start with a higher level
    i = 3
    errors = []
    while domain_dict:
        url, domain_dict, backoff_dict, i = draw_backoff_url(domain_dict, backoff_dict, sleeptime, i)
        htmlstring = fetch_url(url)
        if htmlstring is not None:
            counter = process_result(htmlstring, args, url, counter)
        else:
            LOGGER.debug('No result for URL: %s', url)
            errors.append(url)
    return errors, counter


def multi_threaded_processing(domain_dict, args, sleeptime, counter):
    '''Implement a multi-threaded processing algorithm'''
    i, backoff_dict, errors = 0, dict(), []
    download_threads = args.parallel or DOWNLOAD_THREADS
    while domain_dict:
        # the remaining list is too small, process it differently
        if len({x for v in domain_dict.values() for x in v}) < download_threads:
            errors, counter = single_threaded_processing(domain_dict, backoff_dict, args, sleeptime, counter)
            return errors, counter
        # populate buffer
        bufferlist = []
        while len(bufferlist) < download_threads:
            url, domain_dict, backoff_dict, i = draw_backoff_url(domain_dict, backoff_dict, sleeptime, i)
            bufferlist.append(url)
        # start several threads
        with ThreadPoolExecutor(max_workers=download_threads) as executor:
            future_to_url = {executor.submit(fetch_url, url): url for url in bufferlist}
            for future in as_completed(future_to_url):
                url = future_to_url[future]
                # handle result
                if future.result() is not None:
                    counter = process_result(future.result(), args, url, counter)
                else:
                    LOGGER.debug('No result for URL: %s', url)
                    errors.append(url)
    return errors, counter


def url_processing_pipeline(args, input_urls, sleeptime):
    '''Aggregated functions to show a list and download and process an input list'''
    input_urls = url_processing_checks(args.blacklist, input_urls)
    # print list without further processing
    if args.list:
        for url in input_urls:
            write_result(url, args)  # print('\n'.join(input_urls))
        return None
    # build domain-aware processing list
    domain_dict = dict()
    while input_urls:
        url = input_urls.pop()
        domain_name = extract_domain(url)
        if domain_name not in domain_dict:
            domain_dict[domain_name] = []
        domain_dict[domain_name].append(url)
    # initialize file counter if necessary
    if len(input_urls) > MAX_FILES_PER_DIRECTORY:
        counter = 0
    else:
        counter = None
    if len(domain_dict) <= 5:
        errors, counter = single_threaded_processing(domain_dict, dict(), args, sleeptime, counter)
    else:
        errors, counter = multi_threaded_processing(domain_dict, args, sleeptime, counter)
    LOGGER.debug('%s URLs could not be found', len(errors))
    # option to retry
    if args.archived is True:
        domain_dict = dict()
        domain_dict['archive.org'] = ['https://web.archive.org/web/20/' + e for e in errors]
        archived_errors, _ = single_threaded_processing(domain_dict, dict(), args, sleeptime, counter)
        LOGGER.debug('%s archived URLs out of %s could not be found', len(archived_errors), len(errors))


def file_processing_pipeline(args):
    '''Define batches for parallel file processing and perform the extraction'''
    #if not args.outputdir:
    #    sys.exit('ERROR: please specify an output directory along with the input directory')
    # iterate through file list
    # init
    filebatch = []
    filecounter = None
    processing_cores = args.parallel or FILE_PROCESSING_CORES
    # loop
    for filename in generate_filelist(args.inputdir):
        filebatch.append(filename)
        if len(filebatch) > MAX_FILES_PER_DIRECTORY:
            if filecounter is None:
                filecounter = 0
            # multiprocessing for the batch
            with Pool(processes=processing_cores) as pool:
                pool.map(partial(file_processing, args=args, counter=filecounter), filebatch)
            filecounter += len(filebatch)
            filebatch = []
    # update counter
    if filecounter is not None:
        filecounter += len(filebatch)
    # multiprocessing for the rest
    with Pool(processes=processing_cores) as pool:
        pool.map(partial(file_processing, args=args, counter=filecounter), filebatch)


def examine(htmlstring, args, url=None):
    """Generic safeguards and triggers"""
    result = None
    # safety check
    if htmlstring is None:
        sys.stderr.write('ERROR: empty document\n')
    elif len(htmlstring) > MAX_FILE_SIZE:
        sys.stderr.write('ERROR: file too large\n')
    elif len(htmlstring) < MIN_FILE_SIZE:
        sys.stderr.write('ERROR: file too small\n')
    # proceed
    else:
        # put timeout signal in place
        if args.timeout is True:
            signal.signal(signal.SIGALRM, handler)
            signal.alarm(PROCESSING_TIMEOUT)
        try:
            result = extract(htmlstring, url=url, no_fallback=args.fast,
                             include_comments=args.nocomments, include_tables=args.notables,
                             include_formatting=args.formatting,
                             with_metadata=args.with_metadata,
                             output_format=args.output_format, tei_validation=args.validate,
                             target_language=args.target_language, deduplicate=args.deduplicate)
        # ugly but efficient
        except Exception as err:
            sys.stderr.write('ERROR: ' + str(err) + '\nDetails: ' + str(sys.exc_info()[0]) + '\n')
        # deactivate
        if args.timeout is True:
            signal.alarm(0)
    return result
