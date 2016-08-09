from __future__ import print_function
import os
import logging
from tempfile import mkdtemp
import textwrap
from nose.tools import assert_true, assert_false, assert_not_in, assert_in, \
    assert_equal
import nose
from testfixtures import LogCapture
from helpers import with_setup_args, create_tempfile, get_size
from gcdt.ramuda_core import _install_dependencies_with_pip, bundle_lambda
from gcdt.ramuda_utils import get_packages_to_ignore, cleanup_folder
from gcdt.logger import setup_logger

log = setup_logger(logger_name='RamudaTestCase')


def here(p): return os.path.join(os.path.dirname(__file__), p)


def _setup():
    # setup a temporary folder
    cwd = os.getcwd()
    test_folder = mkdtemp()
    os.chdir(test_folder)
    temp_files = []
    # create settings_dev.conf
    # prepare dummy template for test
    return {'cwd': cwd, 'test_folder': test_folder, 'temp_files': temp_files}


def _teardown(cwd, test_folder, temp_files):
    # rm temporary folder
    os.chdir(cwd)
    #shutil.rmtree(test_folder)
    for t in temp_files:
        os.unlink(t)


@with_setup_args(_setup, _teardown)
def test_get_packages_to_ignore(cwd, test_folder, temp_files):
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
    temp_files.extend([requirements_txt, ramuda_ignore])
    _install_dependencies_with_pip(requirements_txt, test_folder)

    packages = os.listdir(test_folder)
    log.info('packages in test folder:')
    for package in packages:
        log.debug(package)

    matches = get_packages_to_ignore(test_folder, ramuda_ignore)
    log.info('matches in test folder:')
    for match in sorted(matches):
        log.debug(match)
    assert_true('boto3/__init__.py' in matches)
    assert_false('pyhocon' in matches)
    return {'temp_files': temp_files}


@with_setup_args(_setup, _teardown)
def test_cleanup_folder(cwd, test_folder, temp_files):
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
    temp_files.extend([requirements_txt, ramuda_ignore])
    log.info(_install_dependencies_with_pip(
        here('resources/sample_lambda/requirements.txt'), test_folder))

    log.info(get_size(test_folder))
    cleanup_folder(test_folder, ramuda_ignore)
    log.info(get_size(test_folder))
    packages = os.listdir(test_folder)
    log.debug(packages)
    assert_not_in('boto3', packages)
    assert_in('pyhocon', packages)
    return {'temp_files': temp_files}


@with_setup_args(_setup, _teardown)
def test_install_dependencies_with_pip(cwd, test_folder, temp_files):
    requirements_txt = create_tempfile('werkzeug\n')
    temp_files.append(requirements_txt)
    log.info(_install_dependencies_with_pip(
        requirements_txt,
        test_folder))
    packages = os.listdir(test_folder)
    for package in packages:
        log.debug(package)
    assert_true('werkzeug' in packages)
    return {'temp_files': temp_files}


@with_setup_args(_setup, _teardown)
def test_bundle_lambda(cwd, test_folder, temp_files):
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


@with_setup_args(_setup, _teardown)
def test_bundle_lambda_exceeds_limit(cwd, test_folder, temp_files):
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
        #bigfile.write(os.urandom(510000000))
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
