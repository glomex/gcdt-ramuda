from __future__ import print_function
import sys
import boto3
import io
from functools import wraps
from zipfile import ZipFile, ZipInfo
import os
from tabulate import tabulate
import hashlib
import base64
from clint.textui import colored
from pyhocon import config_tree
from glomex_utils.config_reader import read_config
import shutil
import pathspec
from pyhocon import ConfigFactory
from boto3.s3.transfer import S3Transfer
import time
import warnings
import threading
from gcdt.logger import setup_logger
from gcdt import utils

log = setup_logger(logger_name='ramuda_utils')
boto_session = boto3.session.Session()


def files_to_zip(path):
    for root, dirs, files in os.walk(path):
        for f in files:
            full_path = os.path.join(root, f)
            archive_name = full_path[len(path) + len(os.sep):]
            # print 'full_path, archive_name' + full_path, archive_name
            yield full_path, archive_name


def make_zip_file_bytes(paths, handler, settings='settings'):
    """Create the bundle zip file.

    :param paths:
    :param handler:
    :param settings:
    :return: exit_code
    """
    log.debug('creating zip file...')
    buf = io.BytesIO()
    """
    folders = [
        { source = './vendored', target = '.' },
        { source = './impl', target = '.' }
    ]
    as ConfigTree:
    [ConfigTree([('source', './vendored'), ('target', '.')]),
     ConfigTree([('source', './impl'), ('target', './impl')])]
    """
    # automatically add vendored directory
    vendored = config_tree.ConfigTree()
    vendored.put('source', './vendored')
    vendored.put('target', '.')
    cleanup_folder('./vendored')
    paths.append(vendored)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        with ZipFile(buf, 'w') as z:
            z.debug = 0
            for path in paths:
                path_to_zip = path.get('source')
                target = path.get('target', path_to_zip)
                # print 'path to zip ' + path_to_zip
                # print 'target is ' + target
                for full_path, archive_name in files_to_zip(path=path_to_zip):
                    # print 'full_path ' + full_path
                    archive_target = target + '/' + archive_name
                    # print 'archive target ' + archive_target
                    z.write(full_path, archive_target)

            # give settings.conf -rw-r--r-- permissions
            settings_file = ZipInfo('settings.conf')
            settings_file.external_attr = 0644 << 16L
            z.writestr(settings_file, read_config(config_base_name='settings',
                                                  lookups=['stack'],
                                                  output_format='hocon'))
            z.write(handler, os.path.basename(handler))
    # print z.printdir()

    # moved the check to check_buffer_exceeds_limit!
    return buf.getvalue()


def check_buffer_exceeds_limit(buf):
    """Check if size is bigger than 50MB.

    :return: True/False returns True if bigger than 50MB.
    """
    buffer_mbytes = float(len(buf) / 1000000.0)
    log.debug('buffer has size %0.2f MB' % buffer_mbytes)
    if buffer_mbytes >= 50.0:
        log.error('Deployment bundles must not be bigger than 50MB')
        log.error('See http://docs.aws.amazon.com/lambda/latest/dg/limits.html')
        return True
    return False


# TODO: move this to utils
# TODO: maybe this should return True/False
def are_credentials_still_valid():
    """Check whether the credentials have expired.

    :return: exit_code
    """
    client = boto3.client('lambda')
    try:
        client.list_functions()
    except Exception as e:
        print(colored.red('Your credentials have expired... Please ' +
                          'renew and try again!'))
        return 1
    return 0


# TODO: is this used?
def check_aws_credentials():
    """
    A decorator that will check for valid credentials
    """

    def wrapper(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            exit_code = are_credentials_still_valid()
            if exit_code:
                # TODO: remove exit()
                sys.exit()
            result = func(*args, **kwargs)
            return result

        return wrapped

    return wrapper


def lambda_exists(lambda_name):
    client = boto3.client('lambda')
    try:
        client.get_function(FunctionName=lambda_name)
    except Exception as e:
        return False
    else:
        return True


def unit(name):
    if name is 'Duration':
        return 'Milliseconds'
    else:
        return 'Count'


def aggregate_datapoints(datapoints):
    sum = 0.0
    for dp in datapoints:
        sum += dp['Sum']
    return int(sum)


def list_lambda_versions(function_name):
    client = boto3.client('lambda')
    """response = client.get_alias(
        FunctionName=function_name,
        Name='$LATEST'
    )
    print response
    """
    response = client.list_versions_by_function(
        FunctionName=function_name,
    )
    log.debug(response)
    return response


def json2table(json):
    filter_terms = ['ResponseMetadata']
    table = []
    try:
        for k, v in filter(lambda (k, v): k not in filter_terms,
                           json.iteritems()):
            table.append([k, str(v)])
        return tabulate(table, tablefmt='fancy_grid')
    except Exception as e:
        return json


def create_sha256(code):
    checksum = base64.b64encode(hashlib.sha256(code).digest())
    return checksum


def get_remote_code_hash(function_name):
    client = boto3.client('lambda')
    response = client.get_function_configuration(FunctionName=function_name)
    return response['CodeSha256']


def get_packages_to_ignore(folder, ramuda_ignore_file):
    if not ramuda_ignore_file:
        ramuda_ignore_file = os.path.expanduser('~') + '/' + '.ramudaignore'
    # we try to read ignore patterns from the standard .ramudaignore file
    # if we can't find one we don't ignore anything
    # from https://pypi.python.org/pypi/pathspec
    try:
        with open(ramuda_ignore_file, 'r') as fh:
            spec = pathspec.PathSpec.from_lines(pathspec.GitIgnorePattern, fh)

        matches = []
        for match in spec.match_tree(folder):
            matches.append(match)
        return matches
    except Exception as e:
        print(e)
        return []


def cleanup_folder(path, ramuda_ignore_file=None):
    # this cleans up the ./vendored (path) folder
    # exclude locally installed packages from lambda container
    print('path: %s' % path)
    matches = get_packages_to_ignore(path, ramuda_ignore_file)
    result_set = set()
    for package in matches:
        split_dir = package.split('/')[0]
        result_set.add(split_dir)
        # print ('added %s to result set' % split_dir)
    for dir in result_set:
        object = path + '/' + dir
        if os.path.isdir(object):
            print('deleting directory %s' % object)
            shutil.rmtree(path + '/' + dir, ignore_errors=False)
        else:
            # print ('deleting file %s') % object
            os.remove(path + '/' + dir)


def read_ramuda_config(config_file=None):
    """Read .kumo config file from user home.

    :return: pyhocon configuration, exit_code
    """
    if not config_file:
        config_file = os.path.expanduser('~') + '/' + '.kumo'
    try:
        config = ConfigFactory.parse_file(config_file)
        return config, 0
    except Exception as e:
        print(e)
        print(colored.red('Cannot find file .ramuda in your home directory %s'
                          % config_file))
        print(colored.red('Please run "ramuda configure"'))
        return None, 1


class ProgressPercentage(object):
    def __init__(self, filename):
        self._filename = filename
        self._size = float(os.path.getsize(filename))
        self._seen_so_far = 0
        self._lock = threading.Lock()
        self._time = time.time()
        self._time_max = 360

    def __call__(self, bytes_amount):
        # To simplify we'll assume this is hooked up
        # to a single filename.
        with self._lock:
            self._seen_so_far += bytes_amount

            percentage = (self._seen_so_far / self._size) * 100
            elapsed_time = (time.time() - self._time)
            time_left = self._time_max - elapsed_time
            bytes_per_second = self._seen_so_far / elapsed_time
            if (self._size / bytes_per_second > time_left) and time_left < 330:
                print('bad connection')
                raise Exception
            sys.stdout.write(' elapsed time %ss, time left %ss, bps %s' %
                             (str(int(elapsed_time)), str(int(time_left)),
                              str(int(bytes_per_second))))
            sys.stdout.flush()
            sys.stdout.write(
                '\r%s  %s / %s  (%.2f%%)' % (
                    self._filename, self._seen_so_far, self._size,
                    percentage))
            sys.stdout.flush()


@utils.retries(3)
def s3_upload(deploy_bucket, zipfile, lambda_name):
    region = boto_session.region_name
    resource_s3 = boto_session.resource('s3')
    client = boto3.client('s3')
    transfer = S3Transfer(client)
    bucket = deploy_bucket
    git_hash = utils.get_git_revision_short_hash()
    checksum = create_sha256(zipfile)

    # ramuda/eu-west-1/function_name/git_hash.zip
    dest_key = 'ramuda/%s/%s/%s.zip' % (region, lambda_name, git_hash)

    with open('/tmp/' + git_hash, 'wb') as source_file:
        source_file.write(zipfile)

    source_file = '/tmp/' + git_hash
    # print 'uploading to S3'
    start = time.time()
    transfer.upload_file(source_file, bucket, dest_key,
                         callback=ProgressPercentage(source_file))
    end = time.time()
    # print 'uploading took:'
    # print(end - start)

    response = client.head_object(Bucket=bucket, Key=dest_key)
    # print '\n'
    # print response['ETag']
    # print response['VersionId']
    print(dest_key)
    return dest_key, response['ETag'], response['VersionId']
