import sys
import boto3
import io
from zipfile import ZipFile
import os
from tabulate import tabulate
import hashlib
import base64
from clint.textui import colored
from pyhocon import config_tree
from glomex_utils.config_reader import get_config_name
import shutil
import pathspec
from pyhocon import ConfigFactory
import utils
from s3transfer import S3Transfer
import time
import warnings
import threading
from logger import log_json, setup_logger


log = setup_logger(logger_name="ramuda_utils")
boto_session = boto3.session.Session()


def files_to_zip(path):
    for root, dirs, files in os.walk(path):
        for f in files:
            full_path = os.path.join(root, f)
            archive_name = full_path[len(path) + len(os.sep):]
            # print "full_path, archive_name" + full_path, archive_name
            yield full_path, archive_name


def make_zip_file_bytes(paths, handler, settings=get_config_name("settings")):
    log.debug("creating zip file...")
    buf = io.BytesIO()
    """
      folders = [
        { source = "./vendored", target = "." },
        { source = "./impl", target = "." }
    ]
    [ConfigTree([('source', './vendored'), ('target', '.')]), ConfigTree([('source', './impl'), ('target', './impl')])]
    """
    # automatically add vendored directory
    vendored = config_tree.ConfigTree()
    vendored.put("source", "./vendored")
    vendored.put("target", ".")
    cleanup_folder("./vendored")
    paths.append(vendored)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with ZipFile(buf, 'w') as z:
            z.debug = 0
            for path in paths:
                path_to_zip = path.get("source")
                target = path.get("target", path_to_zip)
                # print "path to zip " + path_to_zip
                # print "target is " + target
                for full_path, archive_name in files_to_zip(path=path_to_zip):
                    # print "full_path " + full_path
                    archive_target = target + "/" + archive_name
                    # print "archive target " + archive_target
                    z.write(full_path, archive_target)
            z.write(settings, "settings.conf")
            z.write(handler, os.path.basename(handler))
    # print z.printdir()

    buffer_mbytes = float(len(buf.getvalue()) / 10000000)
    log.debug("buffer has size " + str(buffer_mbytes) + " mb")
    if buffer_mbytes >=50:
        log.error("Deployment bundles must not be bigger than 50MB")
        log.error("See http://docs.aws.amazon.com/lambda/latest/dg/limits.html")
        sys.exit(1)
    return buf.getvalue()


def are_credentials_still_valid():
    client = boto3.client("lambda")
    try:
        client.list_functions()
    except Exception as e:
        print colored.red("Your credentials have expired... Please renew and try again!")
        sys.exit(1)
    else:
        pass


def check_aws_credentials():
    """
    A decorator that will check for valid credentials


    """

    def wrapper(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            are_credentials_still_valid()
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
    if name is "Duration":
        return "Milliseconds"
    else:
        return "Count"


def aggregate_datapoints(datapoints):
    sum = 0.0
    for dp in datapoints:
        sum += dp["Sum"]
    return int(sum)


def list_lambda_versions(function_name):
    client = boto3.client("lambda")
    """response = client.get_alias(
        FunctionName=function_name,
        Name="$LATEST"
    )
    print response
    """
    response = client.list_versions_by_function(
        FunctionName=function_name,
    )
    return response


def json2table(json):
    filter_terms = ["ResponseMetadata"]
    table = []
    try:
        for k, v in filter(lambda (k, v): k not in filter_terms, json.iteritems()):
            table.append([k, "{}".format(v)])
        return tabulate(table, tablefmt="fancy_grid")
    except Exception as e:
        return json


def create_sha256(code):
    checksum = base64.b64encode(hashlib.sha256(code).digest())
    return checksum


def get_remote_code_hash(function_name):
    client = boto3.client("lambda")
    response = client.get_function_configuration(FunctionName=function_name)
    return response["CodeSha256"]


def get_packages_to_ignore(folder):
    homedir = os.path.expanduser('~')
    ramuda_ignore_file = homedir + "/" + ".ramudaignore"
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
        print e
        return []


def cleanup_folder(path):
    matches = get_packages_to_ignore(path)
    result_set = set()
    for package in matches:
        split_dir = package.split("/")[0]
        result_set.add(split_dir)
        #print ("added %s to result set" % split_dir)
    for dir in result_set:
        object = path + "/" + dir
        if os.path.isdir(object):
            #print ("deleting directory %s") % object
            shutil.rmtree(path + "/" + dir, ignore_errors=False)
        else:
            #print ("deleting file %s") % object
            os.remove(path + "/" + dir)


def read_ramuda_config():
    homedir = os.path.expanduser('~')
    ramuda_config_file = homedir + "/" + ".ramuda"
    try:
        config = ConfigFactory.parse_file(ramuda_config_file)
        return config
    except Exception as e:
        print e
        print colored.red("Cannot find file .ramuda in your home directory %s" % ramuda_config_file)
        print colored.red("Please run 'ramuda configure'")
        sys.exit(1)

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
                print ("bad connection")
                raise Exception
            sys.stdout.write((" elapsed time %ss, time left %ss, bps %s") % (str(int(elapsed_time)), str(int(time_left)), str(int(bytes_per_second))))
            sys.stdout.flush()
            sys.stdout.write(
                "\r%s  %s / %s  (%.2f%%)" % (
                    self._filename, self._seen_so_far, self._size,
                    percentage))
            sys.stdout.flush()


@utils.retries(3)
def s3_upload(deploy_bucket, handler_filename, folders, lambda_name):
    region = boto_session.region_name
    resource_s3 = boto_session.resource('s3')
    client = boto3.client('s3')
    transfer = S3Transfer(client)
    bucket = deploy_bucket
    git_hash = utils.get_git_revision_short_hash()

    source_file_buffer = make_zip_file_bytes(handler=handler_filename, paths=folders)

    #checksum = create_sha256(source_file_buffer)

    # ramuda/eu-west-1/function_name/git_hash.zip
    dest_key = "ramuda/%s/%s/%s.zip" % (region, lambda_name, git_hash)

    with open("/tmp/" + git_hash, 'wb') as source_file:
        source_file.write(source_file_buffer)

    source_file = "/tmp/" + git_hash
    #print "uploading to S3"
    start = time.time()
    transfer.upload_file(source_file, bucket, dest_key, callback=ProgressPercentage(source_file))
    end = time.time()
    #print "uploading took:"
    #print(end - start)

    response = client.head_object(Bucket=bucket, Key=dest_key)
    # print "\n"
    # print response["ETag"]
    # print response["VersionId"]
    print dest_key
    return dest_key, response["ETag"], response["VersionId"]
