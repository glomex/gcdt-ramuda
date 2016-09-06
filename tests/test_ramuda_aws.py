# -*- coding: utf-8 -*-
from __future__ import print_function

import os
import textwrap
import time
from StringIO import StringIO

import boto3
import pytest
#from nose.plugins.attrib import attr
from nose.tools import assert_equal, assert_greater_equal, \
    assert_in, assert_not_in, assert_regexp_matches
from pyhocon import ConfigFactory

from gcdt.logger import setup_logger
from gcdt.ramuda_core import delete_lambda, deploy_lambda, \
    _lambda_add_time_schedule_event_source, \
    wire, unwire, _lambda_add_invoke_permission, list_functions, \
    _update_lambda_configuration, get_metrics, rollback, _get_alias_version
from gcdt.ramuda_utils import list_lambda_versions, make_zip_file_bytes, \
    create_sha256, get_remote_code_hash
from .helpers import random_string
from .helpers_aws import create_bucket, delete_bucket, create_role_helper, \
    delete_role_helper, create_lambda_helper, create_lambda_role_helper

log = setup_logger(logger_name='ramuda_test_aws')
# TODO: speedup tests by reusing lambda functions where possible
# TODO: move AWS resource helpers to helpers_aws.py


def here(p): return os.path.join(os.path.dirname(__file__), p)


def get_size(start_path='.'):
    total_size = 0
    for dirpath, dirnames, filenames in os.walk(start_path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            total_size += os.path.getsize(fp)
    return total_size


# TODO: if we use this we need to move some of the following code to
# TODO: helpers_was.py!


def precond_check():
    """Make sure the default AWS profile is set so the test can run on AWS."""
    if os.getenv('USER', None) != 'jenkins' and \
            not os.getenv('AWS_DEFAULT_PROFILE', None):
        print("AWS_DEFAULT_PROFILE variable not set! Test is skipped.")
        return True
    if not os.getenv('ENV', None):
        print("ENV environment variable not set! Test is skipped.")
        return True
    if not os.getenv('ACCOUNT', None):
        print("ACCOUNT environment variable not set! Test is skipped.")
        return True

    return False


# skipif helper check_preconditions
check_preconditions = pytest.mark.skipif(precond_check(),
    reason="Set environment variables to run tests on AWS (see README.md).")


@pytest.fixture(scope='function')  # 'function' or 'module'
def temp_folder():
    # provide a temp folder and cleanup after test
    # this also changes into the folder and back to cwd during cleanup
    cwd = (os.getcwd())
    folder = here('.')
    os.chdir(folder)  # reuse ./vendored folder => cd tests/
    yield folder, cwd
    # cleanup
    os.chdir(cwd)  # cd to original folder
    # reuse ./vendored folder


@pytest.fixture(scope='function')  # 'function' or 'module'
def temp_bucket():
    # create a bucket
    temp_string = random_string()
    bucket_name = 'unittest-lambda-s3-event-source-%s' % temp_string
    create_bucket(bucket_name)
    yield bucket_name
    # cleanup
    delete_bucket(bucket_name)


@pytest.fixture(scope='module')  # 'function' or 'module'
def temp_lambda():
    # provide a lambda function and cleanup after test suite
    temp_string = random_string()
    lambda_name = 'jenkins_test_%s' % temp_string
    role_name = 'unittest_%s_lambda' % temp_string
    # create the function
    role_arn = create_lambda_role_helper(role_name)
    create_lambda_helper(lambda_name, role_arn,
                         './resources/sample_lambda/handler.py')
    yield lambda_name, role_name, role_arn
    # cleanup
    delete_lambda(lambda_name)
    delete_role_helper(role_name)


@pytest.fixture(scope='function')  # 'function' or 'module'
def cleanup_tempfiles():
    items = []
    yield items
    # cleanup
    for i in items:
        os.unlink(i)


@pytest.fixture(scope='function')  # 'function' or 'module'
def cleanup_roles():
    items = []
    yield items
    # cleanup
    for i in items:
        delete_role_helper(i)


@pytest.fixture(scope='function')  # 'function' or 'module'
def cleanup_lambdas():
    items = []
    yield items
    # cleanup
    for i in items:
        delete_lambda(i)


@pytest.fixture(scope='function')  # 'function' or 'module'
def cleanup_buckets():
    items = []
    yield items
    # cleanup
    for i in items:
        delete_bucket(i)


@pytest.mark.aws
@check_preconditions
def test_create_lambda(temp_folder, cleanup_lambdas, cleanup_roles):
    log.info('running test_create_lambda')
    temp_string = random_string()
    lambda_name = 'jenkins_test_' + temp_string
    log.info(lambda_name)
    role = create_role_helper(
        'unittest_%s_lambda' % temp_string,
        policies=[
            'arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole',
            'arn:aws:iam::aws:policy/AWSLambdaExecute']
    )
    cleanup_roles.append(role['RoleName'])

    config_string = textwrap.dedent("""\
        lambda {
            name = "dp-dev-sample-lambda-jobr1"
            description = "lambda test for ramuda"
            role = 'unused'
            handlerFunction = "handler.handle"
            handlerFile = "./resources/sample_lambda/handler.py"
            timeout = 300
            memorySize = 256

            events {
                s3Sources = [{
                    bucket = "jobr-test",
                    type = "s3:ObjectCreated:*" , suffix=".gz"
                }]
                timeSchedules = [{
                    ruleName = "dp-dev-sample-lambda-jobr-T1",
                    ruleDescription = "run every 5 min from 0-5",
                    scheduleExpression = "cron(0/5 0-5 ? * * *)"
                },
                {
                    ruleName = "dp-dev-sample-lambda-jobr-T2",
                    ruleDescription = "run every 5 min from 8-23:59",
                    scheduleExpression = "cron(0/5 8-23:59 ? * * *)"
                }]
            }

            vpc {
                subnetIds = ["subnet-87685dde", "subnet-9f39ccfb",
                    "subnet-166d7061"]
                securityGroups = ["sg-ae6850ca"]
            }
        }

        bundling {
            zip = "bundle.zip"
            folders = [
                { source = "./vendored", target = "." },
                { source = "./impl", target = "impl" }
            ]
        }

        deployment {
            region = "eu-west-1"
        }
        """
                                    )
    conf = ConfigFactory.parse_string(config_string)
    lambda_description = conf.get('lambda.description')
    # print (role)
    role_arn = role['Arn']
    lambda_handler = conf.get('lambda.handlerFunction')
    handler_filename = conf.get('lambda.handlerFile')
    timeout = int(conf.get_string('lambda.timeout'))
    memory_size = int(conf.get_string('lambda.memorySize'))
    zip_name = conf.get('bundling.zip')
    folders_from_file = conf.get('bundling.folders')
    subnet_ids = conf.get('lambda.vpc.subnetIds', None)
    security_groups = conf.get('lambda.vpc.securityGroups', None)
    region = conf.get('deployment.region')
    artifact_bucket = conf.get('deployment.artifactBucket', None)

    deploy_lambda(function_name=lambda_name,
                  role=role_arn,
                  handler_filename=handler_filename,
                  handler_function=lambda_handler,
                  folders=folders_from_file,
                  description=lambda_description,
                  timeout=timeout,
                  memory=memory_size,
                  artifact_bucket=artifact_bucket)

    cleanup_lambdas.append(lambda_name)


@pytest.mark.aws
@check_preconditions
def test_create_lambda_with_s3(temp_folder, cleanup_lambdas, cleanup_roles):
    log.info('running test_create_lambda_with_s3')
    account = os.getenv('ACCOUNT')
    temp_string = random_string()
    lambda_name = 'jenkins_test_' + temp_string
    log.info(lambda_name)
    role = create_role_helper(
        'unittest_%s_lambda' % temp_string,
        policies=[
            'arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole',
            'arn:aws:iam::aws:policy/AWSLambdaExecute']
    )
    cleanup_roles.append(role['RoleName'])

    config_string = textwrap.dedent("""\
        lambda {
            name = "dp-dev-sample-lambda-jobr1"
            description = "lambda test for ramuda"
            handlerFunction = "handler.handle"
            handlerFile = "./resources/sample_lambda/handler.py"
            timeout = 300
            memorySize = 256

            events {
                s3Sources = [{
                    bucket = "jobr-test",
                    type = "s3:ObjectCreated:*" , suffix=".gz"
                }]
                timeSchedules = [{
                    ruleName = "dp-dev-sample-lambda-jobr-T1",
                    ruleDescription = "run every 5 min from 0-5",
                    scheduleExpression = "cron(0/5 0-5 ? * * *)"
                },{
                    ruleName = "dp-dev-sample-lambda-jobr-T2",
                    ruleDescription = "run every 5 min from 8-23:59",
                    scheduleExpression = "cron(0/5 8-23:59 ? * * *)"
                }]
            }


            vpc {
                subnetIds = ["subnet-87685dde", "subnet-9f39ccfb",
                    "subnet-166d7061"]
                securityGroups = ["sg-ae6850ca"]
            }

        }

        bundling {
            zip = "bundle.zip"
            folders = [
                { source = "./vendored", target = "." },
                { source = "./impl", target = "impl" }
            ]
        }

        deployment {
            region = "eu-west-1"
            artifactBucket = "7finity-%s-dev-deployment"
        }
        """ % account)
    conf = ConfigFactory.parse_string(config_string)
    lambda_description = conf.get('lambda.description')
    role_arn = role['Arn']
    lambda_handler = conf.get('lambda.handlerFunction')
    handler_filename = conf.get('lambda.handlerFile')
    timeout = int(conf.get_string('lambda.timeout'))
    memory_size = int(conf.get_string('lambda.memorySize'))
    zip_name = conf.get('bundling.zip')
    folders_from_file = conf.get('bundling.folders')
    subnet_ids = conf.get('lambda.vpc.subnetIds', None)
    security_groups = conf.get('lambda.vpc.securityGroups', None)
    region = conf.get('deployment.region')
    artifact_bucket = conf.get('deployment.artifactBucket', None)

    deploy_lambda(function_name=lambda_name,
                  role=role_arn,
                  handler_filename=handler_filename,
                  handler_function=lambda_handler,
                  folders=folders_from_file,
                  description=lambda_description,
                  timeout=timeout,
                  memory=memory_size,
                  artifact_bucket=artifact_bucket)

    cleanup_lambdas.append(lambda_name)


@pytest.mark.aws
@check_preconditions
def test_update_lambda(temp_folder, cleanup_lambdas, cleanup_roles):
    log.info('running test_update_lambda')
    temp_string = random_string()
    lambda_name = 'jenkins_test_%s' % temp_string
    role_name = 'unittest_%s_lambda' % temp_string
    # create the function
    role_arn = create_lambda_role_helper(role_name)
    cleanup_roles.append(role_name)
    create_lambda_helper(lambda_name, role_arn,
                         './resources/sample_lambda/handler.py')
    # update the function
    create_lambda_helper(lambda_name, role_arn,
                         './resources/sample_lambda/handler_v2.py')
    cleanup_lambdas.append(lambda_name)


def _get_count(function_name, alias_name='ACTIVE', version=None):
    """Send a count request to a lambda function.

    :param function_name:
    :param alias_name:
    :param version:
    :return: count retrieved from lambda call
    """
    client = boto3.client('lambda')
    payload = '{"ramuda_action": "count"}'

    if version:
        response = client.invoke(
            FunctionName=function_name,
            InvocationType='RequestResponse',
            Payload=payload,
            Qualifier=version
        )
    else:
        response = client.invoke(
            FunctionName=function_name,
            InvocationType='RequestResponse',
            Payload=payload,
            Qualifier=alias_name
        )

    results = response['Payload'].read()  # payload is a 'StreamingBody'
    return results


# sample config: operations/reprocessing/consumer/lambda/lambda_dev.conf
# lambda {
#   name = "dp-dev-operations-reprocessing-consumer"
#   description = "Reprocessing files from SQS queue"
#   role = "lookup:stack:dp-dev-operations-reprocessing:RoleLambdaReprocessingConsumerArn"
#   handlerFunction = "handler.lambda_handler"
#   handlerFile = "handler.py"
#   timeout = 300
#   memorySize = 128
#
#   events {
#     timeSchedules = [
#       {
#         ruleName = "dp-dev-operations-reprocessing-consumer",
#         ruleDescription = "run every 1 min",
#         scheduleExpression = "rate(1 minute)"
#       }
#     s3Sources = [
#       {
#         bucket = "lookup:stack:dp-dev-ingest-sync-prod-dev:BucketAdproxyInput",
#         type = "s3:ObjectCreated:*" , suffix=".gz"
#       }
#     ]
#   }
# }
#
# bundling {
#   zip = "bundle.zip"
#   folders = [
#     {source = "../module", target = "./module"}
#   ]
# }
#
# deployment {
#   region = "eu-west-1"
# }

@pytest.mark.aws
@check_preconditions
def test_schedule_event_source(temp_folder, cleanup_lambdas, cleanup_roles):
    log.info('running test_schedule_event_source')
    # include reading config from settings file
    config_string = '''
        lambda {
            events {
                timeSchedules = [
                    {
                        ruleName = "unittest-dev-lambda-schedule",
                        ruleDescription = "run every 1 minute",
                        scheduleExpression = "rate(1 minute)"
                    }
                ]
            }
        }
    '''
    conf = ConfigFactory.parse_string(config_string)

    # time_event_sources = conf.get('lambda.events.timeSchedules', [])
    # for time_event in time_event_sources:
    time_event = conf.get('lambda.events.timeSchedules', {})[0]
    rule_name = time_event.get('ruleName')
    rule_description = time_event.get('ruleDescription')
    schedule_expression = time_event.get('scheduleExpression')

    # now, I need a lambda function that registers the calls!!
    temp_string = random_string()
    lambda_name = 'jenkins_test_%s' % temp_string
    role_name = 'unittest_%s_lambda' % temp_string
    role_arn = create_lambda_role_helper(role_name)
    cleanup_roles.append(role_name)
    create_lambda_helper(lambda_name, role_arn,
                         './resources/sample_lambda/handler_counter.py',
                         lambda_handler='handler_counter.handle')
    cleanup_lambdas.append(lambda_name)

    # lookup lambda arn
    lambda_client = boto3.client('lambda')
    # lambda_function = lambda_client.get_function(FunctionName=function_name)
    alias_name = 'ACTIVE'
    lambda_arn = lambda_client.get_alias(FunctionName=lambda_name,
                                         Name=alias_name)['AliasArn']
    # create scheduled event source
    rule_arn = _lambda_add_time_schedule_event_source(
        rule_name, rule_description, schedule_expression, lambda_arn)
    _lambda_add_invoke_permission(
        lambda_name, 'events.amazonaws.com', rule_arn)

    time.sleep(150)  # wait for at least 2 invocations

    count = _get_count(lambda_name)
    assert_greater_equal(int(count), 2)


@pytest.mark.aws
@pytest.mark.slow
@check_preconditions
def test_wire_unwire_lambda_with_s3(temp_folder, cleanup_lambdas, cleanup_roles,
                                    temp_bucket):
    log.info('running test_wire_unwire_lambda_with_s3')

    # create a lambda function
    temp_string = random_string()
    lambda_name = 'jenkins_test_%s' % temp_string
    role_name = 'unittest_%s_lambda' % temp_string
    role_arn = create_lambda_role_helper(role_name)
    cleanup_roles.append(role_name)
    create_lambda_helper(lambda_name, role_arn,
                         './resources/sample_lambda/handler_counter.py',
                         lambda_handler='handler_counter.handle')
    cleanup_lambdas.append(lambda_name)

    bucket_name = temp_bucket

    # include reading config from settings!
    config_string = '''
        lambda {
            events {
                s3Sources = [
                    {
                        bucket = "%s",
                        type = "s3:ObjectCreated:*" , suffix=".gz"
                    }
                ]
            }
        }
    ''' % bucket_name
    conf = ConfigFactory.parse_string(config_string)

    # wire the function with the bucket
    s3_event_sources = conf.get('lambda.events.s3Sources', [])
    time_event_sources = conf.get('lambda.events.timeSchedules', [])
    exit_code = wire(lambda_name, s3_event_sources, time_event_sources)
    assert_equal(exit_code, 0)

    # put a file into the bucket
    boto3.client('s3').put_object(
        ACL='public-read',
        Body=b'this is some content',
        Bucket=bucket_name,
        Key='test_file.gz',
    )

    # validate function call
    time.sleep(10)  # sleep till the event arrived
    assert_equal(int(_get_count(lambda_name)), 1)

    # unwire the function
    exit_code = unwire(lambda_name, s3_event_sources, time_event_sources)
    assert_equal(exit_code, 0)

    # put in another file
    boto3.client('s3').put_object(
        ACL='public-read',
        Body=b'this is some content',
        Bucket=bucket_name,
        Key='test_file_2.gz',
    )

    # validate function not called
    time.sleep(10)
    assert_equal(int(_get_count(lambda_name)), 1)


@pytest.mark.aws
@check_preconditions
def test_lambda_add_invoke_permission(temp_folder, temp_bucket, cleanup_lambdas,
                                      cleanup_roles):
    log.info('running test_lambda_add_invoke_permission')

    # create a lambda function
    temp_string = random_string()
    lambda_name = 'jenkins_test_%s' % temp_string
    role_name = 'unittest_%s_lambda' % temp_string
    role_arn = create_lambda_role_helper(role_name)
    cleanup_roles.append(role_name)
    create_lambda_helper(lambda_name, role_arn,
                         './resources/sample_lambda/handler_counter.py',
                         lambda_handler='handler_counter.handle')
    cleanup_lambdas.append(lambda_name)
    bucket_name = temp_bucket

    s3_arn = 'arn:aws:s3:::' + bucket_name
    response = _lambda_add_invoke_permission(
        lambda_name, 's3.amazonaws.com', s3_arn)

    # {"Statement":"{\"Condition\":{\"ArnLike\":{\"AWS:SourceArn\":\"arn:aws:s3:::unittest-lambda-s3-bucket-coedce\"}},\"Action\":[\"lambda:InvokeFunction\"],\"Resource\":\"arn:aws:lambda:eu-west-1:188084614522:function:jenkins_test_coedce:ACTIVE\",\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"s3.amazonaws.com\"},\"Sid\":\"07c77fac-68ff-11e6-97f8-c4850848610b\"}"}

    assert_not_in('Error', response)
    assert_in('lambda:InvokeFunction', response['Statement'])


@pytest.mark.aws
@check_preconditions
def test_list_functions(temp_folder, temp_lambda):
    log.info('running test_list_functions')

    lambda_name = temp_lambda[0]
    role_name = temp_lambda[1]

    out = StringIO()
    list_functions(out)

    expected_regex = ".*%s\\n\\tMemory: 128\\n\\tTimeout: 300\\n\\tRole: arn:aws:iam::\d{12}:role\/%s\\n\\tCurrent Version: \$LATEST.*" \
                     % (lambda_name, role_name)

    assert_regexp_matches(out.getvalue().strip(), expected_regex)


@pytest.mark.aws
@check_preconditions
def test_update_lambda_configuration(temp_folder, temp_lambda):
    log.info('running test_update_lambda_configuration')

    lambda_name = temp_lambda[0]
    role_arn = temp_lambda[2]
    handler_function = './resources/sample_lambda/handler_counter.py'
    lambda_description = 'lambda created for unittesting ramuda deployment'

    timeout = 300
    memory_size = 256
    function_version = _update_lambda_configuration(lambda_name, role_arn,
                                                    handler_function,
                                                    lambda_description, timeout,
                                                    memory_size)
    assert_equal(function_version, '$LATEST')


@pytest.mark.aws
@check_preconditions
def test_get_metrics(temp_folder, temp_lambda):
    log.info('running test_get_metrics')

    out = StringIO()
    get_metrics(temp_lambda[0], out)
    assert_regexp_matches(out.getvalue().strip(), \
        'Duration 0\\n\\tErrors 0\\n\\tInvocations [0,1]{1}\\n\\tThrottles 0')


@pytest.mark.aws
@check_preconditions
def test_rollback(temp_folder, temp_lambda):
    log.info('running test_rollback')

    lambda_name = temp_lambda[0]
    role_arn = temp_lambda[2]
    alias_version = _get_alias_version(lambda_name, 'ACTIVE')
    assert_equal(alias_version, '1')

    # update the function
    create_lambda_helper(lambda_name, role_arn,
                         './resources/sample_lambda/handler_v2.py')

    # now we use function_versoin 2!
    alias_version = _get_alias_version(lambda_name, 'ACTIVE')
    assert_equal(alias_version, '$LATEST')

    rollback(lambda_name, alias_name='ACTIVE', version='1')

    # we rolled back to function_version 1
    alias_version = _get_alias_version(lambda_name, 'ACTIVE')
    assert_equal(alias_version, '1')

    # TODO: create more versions >5
    # TODO: do multiple rollbacks >5
    # TODO: verify version + active after rollback
    # TODO: verify invocations meet the right lamda_function version
    # TODO: rollback to last version!

    # here we have the test for ramuda_utils.list_lambda_versions
    response = list_lambda_versions(lambda_name)
    assert_equal(response['Versions'][0]['Version'], '$LATEST')
    assert_equal(response['Versions'][1]['Version'], '1')
    assert_equal(response['Versions'][2]['Version'], '2')


@pytest.mark.aws
@check_preconditions
def test_get_remote_code_hash(temp_folder, temp_lambda):
    log.info('running test_get_remote_code_hash')

    handler_filename = './resources/sample_lambda/handler.py'
    folders_from_file = [
        {'source': './vendored', 'target': '.'},
        {'source': './impl', 'target': 'impl'}
    ]

    # get local hash
    zipfile = make_zip_file_bytes(handler=handler_filename,
                                  paths=folders_from_file)
    expected_hash = create_sha256(zipfile)
    print('hash: %s' % expected_hash)

    lambda_name = temp_lambda[0]
    remote_hash = get_remote_code_hash(lambda_name)
    assert_equal(remote_hash, expected_hash)
