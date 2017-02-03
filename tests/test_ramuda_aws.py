# -*- coding: utf-8 -*-
from __future__ import print_function
import os
import shutil
import textwrap
import time
from StringIO import StringIO
import logging

from pyhocon import ConfigFactory
import pytest
from nose.tools import assert_equal, assert_greater_equal, assert_less, \
    assert_in, assert_not_in, assert_regexp_matches, assert_true
from testfixtures import LogCapture

from gcdt.logger import setup_logger
from gcdt.ramuda_core import delete_lambda, deploy_lambda, ping, \
    _lambda_add_time_schedule_event_source, \
    wire, unwire, _lambda_add_invoke_permission, list_functions, \
    _update_lambda_configuration, get_metrics, rollback, _get_alias_version, \
    bundle_lambda
from gcdt.ramuda_utils import list_lambda_versions, make_zip_file_bytes, \
    create_sha256, get_remote_code_hash
from .helpers import cleanup_tempfiles, temp_folder
from . import helpers, here
from .helpers import temp_folder, check_npm
from .helpers_aws import create_role_helper, delete_role_helper, \
    create_lambda_helper, create_lambda_role_helper, check_preconditions, \
    temp_bucket, boto_session, settings_requirements


log = setup_logger(logger_name='ramuda_test_aws')
# TODO: speedup tests by reusing lambda functions where possible
# TODO: move AWS resource helpers to helpers_aws.py


# TODO remove after refactoring
@pytest.fixture(scope='function')  # 'function' or 'module'
def temp_bucket(boto_session):
    # create a bucket
    temp_string = helpers.random_string()
    bucket_name = 'unittest-lambda-s3-event-source-%s' % temp_string
    create_bucket(boto_session, bucket_name)
    yield bucket_name
    # cleanup
    delete_bucket(boto_session, bucket_name)


# bucket helpers (parts borrowed from tenkai)
def create_bucket(session, bucket):
    client = session.client('s3')
    client.create_bucket(
        Bucket=bucket,
        CreateBucketConfiguration={
            'LocationConstraint': 'eu-west-1'
        }
    )


def delete_bucket(session, bucket):
    log.debug('deleting bucket %s' % bucket)
    if bucket.startswith('unittest-'):
        s3 = session.resource('s3')
        # delete all objects first
        bu = s3.Bucket(bucket)
        log.debug('deleting keys')
        for key in bu.objects.all():
            log.debug('deleting key: %s' % key)
            key.delete()
        log.debug('deleting bucket')
        # now we can delete the bucket
        bu.delete()

# end TODO


def get_size(start_path='.'):
    total_size = 0
    for dirpath, dirnames, filenames in os.walk(start_path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            total_size += os.path.getsize(fp)
    return total_size


# TODO: if we use this we need to move some of the following code to
# TODO: helpers_was.py!


@pytest.fixture(scope='function')  # 'function' or 'module'
def vendored_folder():
    # provide a temp folder and cleanup after test
    # this also changes into the folder and back to cwd during cleanup
    cwd = (os.getcwd())
    folder = here('.')
    os.chdir(folder)  # reuse ./vendored folder => cd tests/
    settings_requirements()
    yield
    # cleanup
    os.chdir(cwd)  # cd to original folder
    # reuse ./vendored folder


@pytest.fixture(scope='function')  # 'function' or 'module'
def temp_lambda(boto_session):
    # provide a lambda function and cleanup after test suite
    temp_string = helpers.random_string()
    lambda_name = 'jenkins_test_%s' % temp_string
    role_name = 'unittest_%s_lambda' % temp_string
    # create the function
    role_arn = create_lambda_role_helper(boto_session, role_name)
    create_lambda_helper(boto_session, lambda_name, role_arn,
                         './resources/sample_lambda/handler.py',
                         lambda_handler='handler.handle')
    yield lambda_name, role_name, role_arn
    # cleanup
    delete_lambda(boto_session, lambda_name)
    delete_role_helper(boto_session, role_name)


@pytest.fixture(scope='function')  # 'function' or 'module'
def cleanup_roles(boto_session):
    items = []
    yield items
    # cleanup
    for i in items:
        delete_role_helper(boto_session, i)


@pytest.fixture(scope='function')  # 'function' or 'module'
def cleanup_lambdas(boto_session):
    items = []
    yield items
    # cleanup
    for i in items:
        delete_lambda(boto_session, i)


@pytest.mark.aws
@check_preconditions
def test_create_lambda(boto_session, vendored_folder, cleanup_lambdas,
                       cleanup_roles):
    log.info('running test_create_lambda')
    temp_string = helpers.random_string()
    lambda_name = 'jenkins_test_' + temp_string
    log.info(lambda_name)
    role = create_role_helper(
        boto_session,
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
                    ruleName = "infra-dev-sample-lambda-jobr-T1",
                    ruleDescription = "run every 5 min from 0-5",
                    scheduleExpression = "cron(0/5 0-5 ? * * *)"
                },
                {
                    ruleName = "infra-dev-sample-lambda-jobr-T2",
                    ruleDescription = "run every 5 min from 8-23:59",
                    scheduleExpression = "cron(0/5 8-23:59 ? * * *)"
                }]
            }

            vpc {
                subnetIds = [
                    "subnet-d5ffb0b1", "subnet-d5ffb0b1", "subnet-d5ffb0b1",
                    "subnet-e9db9f9f"]
                securityGroups = ["sg-660dd700"]
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

    deploy_lambda(
        boto_session=boto_session,
        function_name=lambda_name,
        role=role_arn,
        handler_filename=handler_filename,
        handler_function=lambda_handler,
        folders=folders_from_file,
        description=lambda_description,
        timeout=timeout,
        memory=memory_size,
        artifact_bucket=artifact_bucket
    )
    # TODO improve this (by using a waiter??)
    cleanup_lambdas.append(lambda_name)


@pytest.mark.aws
@check_preconditions
@check_npm
def test_create_lambda_nodejs(boto_session, temp_folder, cleanup_lambdas,
                              cleanup_roles):
    log.info('running test_create_lambda_nodejs')
    # copy package.json and settings_dev.conf from sample
    shutil.copy(
        here('./resources/sample_lambda_nodejs/index.js'), temp_folder[0])
    shutil.copy(
        here('./resources/sample_lambda_nodejs/package.json'), temp_folder[0])
    shutil.copy(
        here('./resources/sample_lambda_nodejs/settings_dev.conf'), temp_folder[0])
    temp_string = helpers.random_string()
    lambda_name = 'jenkins_test_' + temp_string
    log.info(lambda_name)
    role = create_role_helper(
        boto_session,
        'unittest_%s_lambda' % temp_string,
        policies=[
            'arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole',
            'arn:aws:iam::aws:policy/AWSLambdaExecute']
    )
    cleanup_roles.append(role['RoleName'])

    config_string = textwrap.dedent("""\
        lambda {
            runtime = "nodejs4.3"
            name = "infra-dev-sample-lambda-jobr1"
            description = "lambda test for ramuda"
            role = 'unused'
            handlerFunction = "index.handler"
            handlerFile = "index.js"
            timeout = 300
            memorySize = 256

            events {
                s3Sources = [{
                    bucket = "jobr-test",
                    type = "s3:ObjectCreated:*" , suffix=".gz"
                }]
                timeSchedules = [{
                    ruleName = "infra-dev-sample-lambda-jobr-T1",
                    ruleDescription = "run every 5 min from 0-5",
                    scheduleExpression = "cron(0/5 0-5 ? * * *)"
                },
                {
                    ruleName = "infra-dev-sample-lambda-jobr-T2",
                    ruleDescription = "run every 5 min from 8-23:59",
                    scheduleExpression = "cron(0/5 8-23:59 ? * * *)"
                }]
            }

            vpc {
                subnetIds = [
                    "subnet-d5ffb0b1", "subnet-d5ffb0b1", "subnet-d5ffb0b1",
                    "subnet-e9db9f9f"]
                securityGroups = ["sg-660dd700"]
            }
        }

        bundling {
            zip = "bundle.zip"
            folders = [
                { source = "./node_modules", target = "node_modules" }
            ]
        }

        deployment {
            region = "eu-west-1"
        }
        """
                                    )
    conf = ConfigFactory.parse_string(config_string)
    runtime = conf.get('lambda.runtime')
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

    deploy_lambda(
        boto_session=boto_session,
        function_name=lambda_name,
        role=role_arn,
        handler_filename=handler_filename,
        handler_function=lambda_handler,
        folders=folders_from_file,
        description=lambda_description,
        timeout=timeout,
        memory=memory_size,
        artifact_bucket=artifact_bucket,
        runtime=runtime
    )
    # TODO improve this (by using a waiter??)
    cleanup_lambdas.append(lambda_name)


@pytest.mark.aws
@check_preconditions
def test_create_lambda_with_s3(boto_session, vendored_folder, cleanup_lambdas,
                               cleanup_roles):
    log.info('running test_create_lambda_with_s3')
    account = os.getenv('ACCOUNT')
    temp_string = helpers.random_string()
    lambda_name = 'jenkins_test_' + temp_string
    log.info(lambda_name)
    role = create_role_helper(
        boto_session,
        'unittest_%s_lambda' % temp_string,
        policies=[
            'arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole',
            'arn:aws:iam::aws:policy/AWSLambdaExecute']
    )
    cleanup_roles.append(role['RoleName'])

    config_string = textwrap.dedent("""\
        lambda {
            name = "dp-dev-sample-lambda-jobr1"
            description = "lambda nodejs test for ramuda"
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
                    ruleName = "infra-dev-sample-lambda-jobr-T1",
                    ruleDescription = "run every 5 min from 0-5",
                    scheduleExpression = "cron(0/5 0-5 ? * * *)"
                },{
                    ruleName = "infra-dev-sample-lambda-jobr-T2",
                    ruleDescription = "run every 5 min from 8-23:59",
                    scheduleExpression = "cron(0/5 8-23:59 ? * * *)"
                }]
            }


            vpc {
                subnetIds = [
                    "subnet-d5ffb0b1", "subnet-d5ffb0b1", "subnet-d5ffb0b1",
                    "subnet-e9db9f9f"]
                securityGroups = ["sg-660dd700"]
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

    deploy_lambda(
        boto_session=boto_session,
        function_name=lambda_name,
        role=role_arn,
        handler_filename=handler_filename,
        handler_function=lambda_handler,
        folders=folders_from_file,
        description=lambda_description,
        timeout=timeout,
        memory=memory_size,
        artifact_bucket=artifact_bucket
    )
    cleanup_lambdas.append(lambda_name)


@pytest.mark.aws
@check_preconditions
def test_update_lambda(boto_session, vendored_folder, cleanup_lambdas,
                       cleanup_roles):
    log.info('running test_update_lambda')
    temp_string = helpers.random_string()
    lambda_name = 'jenkins_test_%s' % temp_string
    role_name = 'unittest_%s_lambda' % temp_string
    # create the function
    role_arn = create_lambda_role_helper(boto_session, role_name)
    cleanup_roles.append(role_name)
    create_lambda_helper(boto_session, lambda_name, role_arn,
                         './resources/sample_lambda/handler.py')
    # update the function
    create_lambda_helper(boto_session, lambda_name, role_arn,
                         './resources/sample_lambda/handler_v2.py')
    cleanup_lambdas.append(lambda_name)


def _get_count(boto_session, function_name, alias_name='ACTIVE', version=None):
    """Send a count request to a lambda function.

    :param boto_session:
    :param function_name:
    :param alias_name:
    :param version:
    :return: count retrieved from lambda call
    """
    client_lambda = boto_session.client('lambda')
    payload = '{"ramuda_action": "count"}'

    if version:
        response = client_lambda.invoke(
            FunctionName=function_name,
            InvocationType='RequestResponse',
            Payload=payload,
            Qualifier=version
        )
    else:
        response = client_lambda.invoke(
            FunctionName=function_name,
            InvocationType='RequestResponse',
            Payload=payload,
            Qualifier=alias_name
        )

    #print type(response['Payload'])
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
def test_schedule_event_source(boto_session, vendored_folder, cleanup_lambdas,
                               cleanup_roles):
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
    temp_string = helpers.random_string()
    lambda_name = 'jenkins_test_%s' % temp_string
    role_name = 'unittest_%s_lambda' % temp_string
    role_arn = create_lambda_role_helper(boto_session, role_name)
    cleanup_roles.append(role_name)
    create_lambda_helper(boto_session, lambda_name, role_arn,
                         './resources/sample_lambda/handler_counter.py',
                         lambda_handler='handler_counter.handle')
    cleanup_lambdas.append(lambda_name)

    # lookup lambda arn
    lambda_client = boto_session.client('lambda')
    # lambda_function = lambda_client.get_function(FunctionName=function_name)
    alias_name = 'ACTIVE'
    lambda_arn = lambda_client.get_alias(FunctionName=lambda_name,
                                         Name=alias_name)['AliasArn']
    # create scheduled event source
    rule_arn = _lambda_add_time_schedule_event_source(
        boto_session, rule_name, rule_description, schedule_expression,
        lambda_arn
    )
    _lambda_add_invoke_permission(
        boto_session, lambda_name, 'events.amazonaws.com', rule_arn)

    time.sleep(180)  # wait for at least 2 invocations

    count = _get_count(boto_session, lambda_name)
    assert_greater_equal(int(count), 2)


@pytest.mark.aws
@pytest.mark.slow
@check_preconditions
def test_wire_unwire_lambda_with_s3(boto_session, vendored_folder,
                                    cleanup_lambdas, cleanup_roles,
                                    temp_bucket):
    log.info('running test_wire_unwire_lambda_with_s3')

    # create a lambda function
    temp_string = helpers.random_string()
    lambda_name = 'jenkins_test_%s' % temp_string
    role_name = 'unittest_%s_lambda' % temp_string
    role_arn = create_lambda_role_helper(boto_session, role_name)
    cleanup_roles.append(role_name)
    create_lambda_helper(boto_session, lambda_name, role_arn,
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
    exit_code = wire(boto_session, lambda_name, s3_event_sources,
                     time_event_sources)
    assert_equal(exit_code, 0)

    # put a file into the bucket
    boto_session.client('s3').put_object(
        ACL='public-read',
        Body=b'this is some content',
        Bucket=bucket_name,
        Key='test_file.gz',
    )

    # validate function call
    time.sleep(20)  # sleep till the event arrived
    assert_equal(int(_get_count(boto_session, lambda_name)), 1)

    # unwire the function
    exit_code = unwire(boto_session, lambda_name, s3_event_sources,
                       time_event_sources)
    assert_equal(exit_code, 0)

    # put in another file
    boto_session.client('s3').put_object(
        ACL='public-read',
        Body=b'this is some content',
        Bucket=bucket_name,
        Key='test_file_2.gz',
    )

    # validate function not called
    time.sleep(10)
    assert_equal(int(_get_count(boto_session, lambda_name)), 1)


@pytest.mark.aws
@check_preconditions
def test_lambda_add_invoke_permission(boto_session, vendored_folder,
                                      temp_bucket, cleanup_lambdas,
                                      cleanup_roles):
    log.info('running test_lambda_add_invoke_permission')

    # create a lambda function
    temp_string = helpers.random_string()
    print(temp_string)
    lambda_name = 'jenkins_test_%s' % temp_string
    role_name = 'unittest_%s_lambda' % temp_string
    role_arn = create_lambda_role_helper(boto_session, role_name)
    cleanup_roles.append(role_name)
    create_lambda_helper(boto_session, lambda_name, role_arn,
                         './resources/sample_lambda/handler_counter.py',
                         lambda_handler='handler_counter.handle')
    cleanup_lambdas.append(lambda_name)
    bucket_name = temp_bucket

    s3_arn = 'arn:aws:s3:::' + bucket_name
    response = _lambda_add_invoke_permission(
        boto_session, lambda_name, 's3.amazonaws.com', s3_arn)

    # {"Statement":"{\"Condition\":{\"ArnLike\":{\"AWS:SourceArn\":\"arn:aws:s3:::unittest-lambda-s3-bucket-coedce\"}},\"Action\":[\"lambda:InvokeFunction\"],\"Resource\":\"arn:aws:lambda:eu-west-1:188084614522:function:jenkins_test_coedce:ACTIVE\",\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"s3.amazonaws.com\"},\"Sid\":\"07c77fac-68ff-11e6-97f8-c4850848610b\"}"}

    assert_not_in('Error', response)
    assert_in('lambda:InvokeFunction', response['Statement'])
    # TODO add more asserts!!


@pytest.mark.aws
@check_preconditions
def test_list_functions(boto_session, vendored_folder, temp_lambda):
    log.info('running test_list_functions')

    lambda_name = temp_lambda[0]
    role_name = temp_lambda[1]

    out = StringIO()
    list_functions(boto_session, out)

    expected_regex = ".*%s\\n\\tMemory: 128\\n\\tTimeout: 300\\n\\tRole: arn:aws:iam::\d{12}:role\/%s\\n\\tCurrent Version: \$LATEST.*" \
                     % (lambda_name, role_name)

    assert_regexp_matches(out.getvalue().strip(), expected_regex)


@pytest.mark.aws
@check_preconditions
def test_update_lambda_configuration(boto_session, vendored_folder, temp_lambda):
    log.info('running test_update_lambda_configuration')

    lambda_name = temp_lambda[0]
    role_arn = temp_lambda[2]
    handler_function = './resources/sample_lambda/handler_counter.py'
    lambda_description = 'lambda created for unittesting ramuda deployment'

    timeout = 300
    memory_size = 256
    function_version = _update_lambda_configuration(boto_session, lambda_name,
                                                    role_arn, handler_function,
                                                    lambda_description, timeout,
                                                    memory_size)
    assert_equal(function_version, '$LATEST')


@pytest.mark.aws
@check_preconditions
def test_get_metrics(boto_session, vendored_folder, temp_lambda):
    log.info('running test_get_metrics')

    out = StringIO()
    get_metrics(boto_session, temp_lambda[0], out)
    assert_regexp_matches(out.getvalue().strip(),
        'Duration 0\\n\\tErrors 0\\n\\tInvocations [0,1]{1}\\n\\tThrottles 0')


@pytest.mark.aws
@check_preconditions
def test_rollback(boto_session, vendored_folder, temp_lambda):
    log.info('running test_rollback')

    lambda_name = temp_lambda[0]
    role_arn = temp_lambda[2]
    alias_version = _get_alias_version(boto_session, lambda_name, 'ACTIVE')
    assert_equal(alias_version, '1')

    # update the function
    create_lambda_helper(boto_session, lambda_name, role_arn,
                         './resources/sample_lambda/handler_v2.py')

    # now we use function_version 2!
    alias_version = _get_alias_version(boto_session, lambda_name, 'ACTIVE')
    assert_equal(alias_version, '$LATEST')

    exit_code = rollback(boto_session, lambda_name, alias_name='ACTIVE')
    assert_equal(exit_code, 0)

    # we rolled back to function_version 1
    alias_version = _get_alias_version(boto_session, lambda_name, 'ACTIVE')
    assert_equal(alias_version, '1')

    # try to rollback when previous version does not exist
    exit_code = rollback(boto_session, lambda_name, alias_name='ACTIVE')
    assert_equal(exit_code, 1)

    # version did not change
    alias_version = _get_alias_version(boto_session, lambda_name, 'ACTIVE')
    assert_equal(alias_version, '1')

    # roll back to the latest version
    exit_code = rollback(boto_session, lambda_name, alias_name='ACTIVE', version='$LATEST')
    assert_equal(exit_code, 0)

    # latest version of lambda is used
    alias_version = _get_alias_version(boto_session, lambda_name, 'ACTIVE')
    assert_equal(alias_version, '$LATEST')

    # TODO: create more versions >5
    # TODO: do multiple rollbacks >5
    # TODO: verify version + active after rollback
    # TODO: verify invocations meet the right lamda_function version

    # here we have the test for ramuda_utils.list_lambda_versions
    response = list_lambda_versions(boto_session, lambda_name)
    assert_equal(response['Versions'][0]['Version'], '$LATEST')
    assert_equal(response['Versions'][1]['Version'], '1')
    assert_equal(response['Versions'][2]['Version'], '2')


# excluded vendored folder from bundle since we get different hash codes
# from different platforms so we can not record this
# TODO: this is a defect in ramuda (see #145, #158)!
'''
@pytest.mark.aws
@check_preconditions
def test_get_remote_code_hash(boto_session, vendored_folder, temp_lambda):
    log.info('running test_get_remote_code_hash')

    handler_filename = './resources/sample_lambda/handler.py'
    folders_from_file = [
        {'source': './vendored', 'target': '.'},
        {'source': './resources/sample_lambda/impl', 'target': 'impl'}
    ]

    # get local hash
    zipfile = make_zip_file_bytes(handler=handler_filename,
                                  paths=folders_from_file)
    expected_hash = create_sha256(zipfile)

    lambda_name = temp_lambda[0]
    time.sleep(10)
    remote_hash = get_remote_code_hash(boto_session, lambda_name)
    assert_equal(remote_hash, expected_hash)
'''


@pytest.mark.aws
@check_preconditions
def test_ping(boto_session, vendored_folder, temp_lambda):
    log.info('running test_ping')

    lambda_name = temp_lambda[0]
    role_arn = temp_lambda[2]

    # test the ping
    response = ping(boto_session, lambda_name)
    assert response == '"alive"'

    # update the function
    create_lambda_helper(boto_session, lambda_name, role_arn,
                         './resources/sample_lambda/handler_no_ping.py',
                         lambda_handler='handler_no_ping.handle')

    # test has no ping
    response = ping(boto_session, lambda_name)
    assert response == '{"ramuda_action": "ping"}'


@pytest.mark.aws
@check_preconditions
def test_prebundle(boto_session, temp_folder, cleanup_lambdas, cleanup_roles):
    log.info('running test_prebundle')

    temp_string = helpers.random_string()
    lambda_name = 'jenkins_test_%s' % temp_string
    role_name = 'unittest_%s_lambda' % temp_string
    role_arn = create_lambda_role_helper(boto_session, role_name)
    cleanup_roles.append(role_name)

    script = lambda r: here('resources/sample_lambda_with_prebundle/{}.sh'.format(r))
    with open(here('resources/sample_lambda_with_prebundle/lambda.conf.tpl')) as template:
        config_string = template.read() % (
            script('create_requirements'),
            script('create_handler'),
            script('create_settings')
        )
    conf = ConfigFactory.parse_string(config_string)

    deploy_lambda(
        boto_session=boto_session,
        role=role_arn,
        function_name=lambda_name,
        handler_filename=conf.get('lambda.handlerFile'),
        handler_function=conf.get('lambda.handlerFunction'),
        description=conf.get('lambda.description'),
        timeout=conf.get('lambda.timeout'),
        memory=conf.get('lambda.memorySize'),
        folders=conf.get('bundling.folders'),
        prebundle_scripts=conf.get('bundling.preBundle')
    )
    cleanup_lambdas.append(lambda_name)

    response = ping(boto_session, lambda_name)
    assert response == '"alive"'


@pytest.mark.aws
@check_preconditions
def test_bundle_lambda(temp_folder, boto_session):
    folders_from_file = [
        {'source': './vendored', 'target': '.'},
        {'source': './impl', 'target': 'impl'}
    ]
    prebundle_scripts = [here('resources/sample_lambda_with_prebundle/sample_script.sh')]
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
    exit_code = bundle_lambda(boto_session, './handler.py', folders_from_file, prebundle_scripts)
    assert_equal(exit_code, 0)

    assert_true(os.path.isfile('test_ramuda_prebundle.txt'))

    zipped_size = os.path.getsize('bundle.zip')
    unzipped_size = get_size('vendored') + get_size('impl') + os.path.getsize('handler.py')
    assert_less(zipped_size, unzipped_size)


@pytest.mark.slow
@pytest.mark.aws
@check_preconditions
def test_bundle_lambda_exceeds_limit(temp_folder, boto_session):
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
        exit_code = bundle_lambda(boto_session, './handler.py',
                                  folders_from_file)
        l.check(
            ('ramuda_utils', 'ERROR',
             'Deployment bundles must not be bigger than 50MB'),
            ('ramuda_utils', 'ERROR',
             'See http://docs.aws.amazon.com/lambda/latest/dg/limits.html')
        )

    assert_equal(exit_code, 1)
