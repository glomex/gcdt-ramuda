# -*- coding: utf-8 -*-
from __future__ import print_function
import os
import logging
from StringIO import StringIO
from collections import OrderedDict
from tempfile import NamedTemporaryFile
import textwrap
import json
import time
from s3transfer.subscribers import BaseSubscriber
from nose.tools import assert_true, assert_false, assert_not_in, assert_in, \
    assert_equal, assert_regexp_matches
import pytest
from testfixtures import LogCapture
from .helpers import create_tempfile, get_size, temp_folder, cleanup_tempfiles
from gcdt.ramuda_core import _install_dependencies_with_pip, bundle_lambda
from gcdt.ramuda_utils import get_packages_to_ignore, cleanup_folder, unit, \
    aggregate_datapoints, json2table, create_sha256, ProgressPercentage, \
    list_of_dict_equals, create_aws_s3_arn, get_rule_name_from_event_arn, get_bucket_from_s3_arn, \
    build_filter_rules
from gcdt.logger import setup_logger

log = setup_logger(logger_name='ramuda_test')


def here(p): return os.path.join(os.path.dirname(__file__), p)


@pytest.mark.slow
def test_get_packages_to_ignore(temp_folder, cleanup_tempfiles):
    requirements_txt = create_tempfile('boto3\npyhocon\n')
    # typical .ramudaignore file:
    ramuda_ignore = create_tempfile(textwrap.dedent("""\
        boto3*
        botocore*
        python-dateutil*
        six*
        docutils*
        jmespath*
        futures*
    """))
    # schedule the temp_files for cleanup:
    cleanup_tempfiles.extend([requirements_txt, ramuda_ignore])
    _install_dependencies_with_pip(requirements_txt, temp_folder[0])

    packages = os.listdir(temp_folder[0])
    log.info('packages in test folder:')
    for package in packages:
        log.debug(package)

    matches = get_packages_to_ignore(temp_folder[0], ramuda_ignore)
    log.info('matches in test folder:')
    for match in sorted(matches):
        log.debug(match)
    assert_true('boto3/__init__.py' in matches)
    assert_false('pyhocon' in matches)


@pytest.mark.slow
def test_cleanup_folder(temp_folder, cleanup_tempfiles):
    requirements_txt = create_tempfile('boto3\npyhocon\n')
    # typical .ramudaignore file:
    ramuda_ignore = create_tempfile(textwrap.dedent("""\
        boto3*
        botocore*
        python-dateutil*
        six*
        docutils*
        jmespath*
        futures*
    """))
    cleanup_tempfiles.extend([requirements_txt, ramuda_ignore])
    log.info(_install_dependencies_with_pip(
        here('resources/sample_lambda/requirements.txt'), temp_folder[0]))

    log.debug('test folder size: %s' % get_size(temp_folder[0]))
    cleanup_folder(temp_folder[0], ramuda_ignore)
    log.debug('test folder size: %s' % get_size(temp_folder[0]))
    packages = os.listdir(temp_folder[0])
    log.debug(packages)
    assert_not_in('boto3', packages)
    assert_in('pyhocon', packages)


@pytest.mark.slow
def test_install_dependencies_with_pip(temp_folder, cleanup_tempfiles):
    requirements_txt = create_tempfile('werkzeug\n')
    cleanup_tempfiles.append(requirements_txt)
    log.info(_install_dependencies_with_pip(
        requirements_txt,
        temp_folder[0]))
    packages = os.listdir(temp_folder[0])
    for package in packages:
        log.debug(package)
    assert_true('werkzeug' in packages)


def test_bundle_lambda(temp_folder):
    folders_from_file = [
        {'source': './vendored', 'target': '.'},
        {'source': './impl', 'target': 'impl'}
    ]
    os.environ['ENV'] = 'DEV'
    os.mkdir('./vendored')
    os.mkdir('./impl')
    with open('./requirements.txt', 'w') as req:
        req.write('pyhocon\n')
    with open('./handler.py', 'w') as req:
        req.write('# this is my lambda handler\n')
    with open('./settings_dev.conf', 'w') as req:
        req.write('\n')
    # write 1MB file -> this gets us a zip file that is within the 50MB limit
    with open('./impl/bigfile', 'wb') as bigfile:
        print(bigfile.name)
        bigfile.write(os.urandom(1000000))  # 1 MB
    exit_code = bundle_lambda('./handler.py', folders_from_file)
    assert_equal(exit_code, 0)


@pytest.mark.slow
def test_bundle_lambda_exceeds_limit(temp_folder):
    folders_from_file = [
        {'source': './vendored', 'target': '.'},
        {'source': './impl', 'target': 'impl'}
    ]
    os.environ['ENV'] = 'DEV'

    os.mkdir('./vendored')
    os.mkdir('./impl')
    with open('./requirements.txt', 'w') as req:
        req.write('pyhocon\n')
    with open('./handler.py', 'w') as req:
        req.write('# this is my lambda handler\n')
    with open('./settings_dev.conf', 'w') as req:
        req.write('\n')
    # write 51MB file -> this gets us a zip file that exceeds the 50MB limit
    with open('./impl/bigfile', 'wb') as bigfile:
        print(bigfile.name)
        bigfile.write(os.urandom(51100000))  # 51 MB

    # capture ERROR logging:
    with LogCapture(level=logging.ERROR) as l:
        exit_code = bundle_lambda('./handler.py', folders_from_file)
        l.check(
            ('ramuda_utils', 'ERROR',
             'Deployment bundles must not be bigger than 50MB'),
            ('ramuda_utils', 'ERROR',
             'See http://docs.aws.amazon.com/lambda/latest/dg/limits.html')
        )

    assert_equal(exit_code, 1)


def test_unit():
    assert_equal(unit('Duration'), 'Milliseconds')
    assert_equal(unit('Else'), 'Count')


def test_aggregate_datapoints():
    assert_equal(aggregate_datapoints(
        [{'Sum': 0.1}, {'Sum': 0.1}, {'Sum': 0.1}, {'Sum': 0.1}, {'Sum': 0.1},
         {'Sum': 0.1}]), 0)
    assert_equal(aggregate_datapoints(
        [{'Sum': 1.1}, {'Sum': 1.1}, {'Sum': 1.1}, {'Sum': 1.1}]), 4)


def test_json2table():
    data = {
        'sth': 'here',
        'number': 1.1,
        'ResponseMetadata': 'bla'
    }
    expected = u'\u2552\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2564\u2550\u2550\u2550\u2550\u2550\u2550\u2555\n\u2502 sth    \u2502 here \u2502\n\u251c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u253c\u2500\u2500\u2500\u2500\u2500\u2500\u2524\n\u2502 number \u2502 1.1  \u2502\n\u2558\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2567\u2550\u2550\u2550\u2550\u2550\u2550\u255b'
    actual = json2table(data)
    assert_equal(actual, expected)


def test_json2table_create_lambda_response():
    response = OrderedDict([
        ('CodeSha256', 'CwEvufZaAmNgUnlA6yTJGi8p8MNR+mNcCNYPOIwsTNM='),
        ('FunctionName', 'jenkins-gcdt-lifecycle-for-ramuda'),
        ('CodeSize', 430078),
        ('MemorySize', 256),
        ('FunctionArn', 'arn:aws:lambda:eu-west-1:644239850139:function:jenkins-gcdt-lifecycle-for-ramuda'),
        ('Version', '13'),
        ('Role', 'arn:aws:iam::644239850139:role/lambda/dp-dev-store-redshift-cdn-lo-LambdaCdnRedshiftLoad-DD2S84CZFGT4'),
        ('Timeout', 300),
        ('LastModified', '2016-08-23T15:27:07.658+0000'),
        ('Handler', 'handler.handle'),
        ('Runtime', 'python2.7'),
        ('Description', 'lambda test for ramuda')
    ])

    expected_file = here('resources/expected/expected_json2table.txt')
    with open(expected_file) as efile:
        expected = efile.read()
    actual = json2table(response).encode('utf-8')
    assert_equal(actual, expected)


def test_json2table_exception():
    data = json.dumps({
        'sth': 'here',
        'number': 1.1,
        'ResponseMetadata': 'bla'
    })
    actual = json2table(data)
    assert_equal(actual, data)


def test_create_sha256():
    actual = create_sha256('Meine Oma fährt im Hühnerstall Motorrad')
    expected = 'SM6siXnsKAmQuG5egM0MYKgUU60nLFxUVeEvTcN4OFI='
    assert_equal(actual, expected)


def test_create_s3_arn():
    s3_arn = create_aws_s3_arn('dp-dev-not-existing')
    assert_equal(s3_arn, 'arn:aws:s3:::dp-dev-not-existing')


def test_get_bucket_name_from_s3_arn():
    s3_arn = 'arn:aws:s3:::test-bucket-dp-723'
    bucket_name = get_bucket_from_s3_arn(s3_arn)
    assert_equal(bucket_name, 'test-bucket-dp-723')


def test_get_rule_name_from_event_arn():
    rule_arn = 'arn:aws:events:eu-west-1:111537987451:rule/dp-preprod-test-dp-723-T1_fun2'
    rule_name = get_rule_name_from_event_arn(rule_arn)
    assert_equal(rule_name, 'dp-preprod-test-dp-723-T1_fun2')


def test_list_of_dicts():
    list_1 = [
        {"key1" : "value1"},
        {"key2" : "value2"},
        {"key3" : "value3"},
    ]
    list_2 = [
        {"key1" : "value1"},
        {"key2" : "value2"},
        {"key3" : "value3"},
    ]
    list_3 = [
        {"key1" : "value1"},
        {"key2" : "value2"},
    ]
    equals_1 = list_of_dict_equals(list_1, list_2)
    assert_true(equals_1)
    equals_2 = list_of_dict_equals(list_1, list_3)
    assert_false(equals_2)


def test_build_filter_rules():
    prefix = 'folder'
    suffix = '.gz'
    rules = build_filter_rules(prefix, suffix)

    rules_hardcoded = [
        { 'Name': 'Prefix',
          'Value': 'folder'},
        {'Name': 'Suffix',
         'Value': '.gz'}
    ]
    match = list_of_dict_equals(rules, rules_hardcoded)
    assert_true(match)


def test_progress_percentage(cleanup_tempfiles):
    class ProgressCallbackInvoker(BaseSubscriber):
        """A back-compat wrapper to invoke a provided callback via a subscriber

        :param callback: A callable that takes a single positional argument for
            how many bytes were transferred.
        """
        def __init__(self, callback):
            self._callback = callback

        def on_progress(self, bytes_transferred, **kwargs):
            self._callback(bytes_transferred)

    # create dummy file
    tf = NamedTemporaryFile(delete=False, suffix='.tgz')
    cleanup_tempfiles.append(tf.name)
    open(tf.name, 'w').write('some content here...')
    out = StringIO()
    # instantiate ProgressReporter
    callback = ProgressPercentage(tf.name, out=out)
    subscriber = ProgressCallbackInvoker(callback)
    # 1 byte -> 5%
    time.sleep(0.001)
    subscriber.on_progress(bytes_transferred=1)
    assert_regexp_matches(out.getvalue().strip(),
                          '.*\.tgz  1 / 20\.0  \(5\.00%\)')
    # 11 (1+10) byte -> 55%
    subscriber.on_progress(bytes_transferred=10)
    assert_regexp_matches(out.getvalue().strip(),
                          '.*\.tgz  11 / 20\.0  \(55\.00%\)')
