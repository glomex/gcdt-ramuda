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
from config_reader import get_config_name

def files_to_zip(path):
    for root, dirs, files in os.walk(path):
        for f in files:
            full_path = os.path.join(root, f)
            archive_name = full_path[len(path) + len(os.sep):]
            # print "full_path, archive_name" + full_path, archive_name
            yield full_path, archive_name


def make_zip_file_bytes(paths, handler, settings=get_config_name("settings")):
    print settings
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
    paths.append(vendored)
    with ZipFile(buf, 'w') as z:
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
        z.write(handler, handler)
    # print z.printdir()

    buffer_mbytes = float(len(buf.getvalue()) / 10000000)
    # print "buffer has size " + str(buffer_mbytes) + " mb"
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
            table.append([k, str(v)])
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
