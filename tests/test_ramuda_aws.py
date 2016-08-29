# -*- coding: utf-8 -*-
from __future__ import print_function

import os
import textwrap
import time
from StringIO import StringIO

import boto3
from nose.plugins.attrib import attr
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
from .helpers import check_preconditions, random_string, with_setup_args
from .helpers_aws import create_bucket, delete_bucket, create_role_helper, \
    delete_role_helper, create_lambda_helper, create_lambda_role_helper

log = setup_logger(logger_name='ramuda_test_aws')
# TODO: refactor tests to clean up lambda functions in case of failure
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


def _setup():
    check_preconditions()  # check whether required AWS env variables are set?
    cwd = (os.getcwd())
    folder = here('.')
    os.chdir(folder)
    temp_files = []
    # create settings_dev.conf
    os.environ['ENV'] = 'DEV'  # make sure we do not run that on prod code!
    settings_file = os.path.join(folder, 'settings_dev.conf')
    with open(settings_file, 'w') as settings:
        setting_string = textwrap.dedent("""\
            sample_lambda {
                cw_name = "dp-dev-sample"
            }""")
        settings.write(setting_string)
    temp_files.append(settings_file)
    requirements_txt = os.path.join(folder, 'requirements.txt')
    with open(requirements_txt, 'w') as req:
        req.write('pyhocon==0.3.28\n')
    temp_files.append(requirements_txt)
    # ./vendored folder
    # folder = mkdtemp()
    if not os.path.exists('./vendored'):
        # reuse ./vendored folder to save us some time during pip install...
        os.makedirs('./vendored')
    return {'cwd': cwd, 'temp_files': temp_files, 'temp_roles': []}


def _teardown(cwd, temp_files=[], temp_roles=[]):
    os.chdir(cwd)
    # shutil.rmtree(folder)  # reuse ./vendored folder
    for t in temp_files:
        os.unlink(t)
    for r in temp_roles:
        delete_role_helper(r)


@attr('aws')
@with_setup_args(_setup, _teardown)
def test_create_lambda(cwd, temp_files, temp_roles):
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
    temp_roles = [role['RoleName']]

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

    delete_lambda(lambda_name)
    return {'temp_roles': temp_roles}


@attr('aws')
@with_setup_args(_setup, _teardown)
def test_create_lambda_with_s3(cwd, temp_files, temp_roles):
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
    temp_roles = [role['RoleName']]

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

    delete_lambda(lambda_name)
    return {'temp_roles': temp_roles}


@attr('aws')
@with_setup_args(_setup, _teardown)
def test_update_lambda(cwd, temp_files, temp_roles):
    log.info('running test_update_lambda')
    temp_string = random_string()
    lambda_name = 'jenkins_test_%s' % temp_string
    role_name = 'unittest_%s_lambda' % temp_string
    # create the function
    role_arn = create_lambda_role_helper(role_name)
    create_lambda_helper(lambda_name, role_arn,
                         './resources/sample_lambda/handler.py')
    # update the function
    # TODO: do not recreate the role!
    create_lambda_helper(lambda_name, role_arn,
                         './resources/sample_lambda/handler_v2.py')

    # delete the function
    delete_lambda(lambda_name)
    return {'temp_roles': [role_name]}


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

@attr('aws')
@with_setup_args(_setup, _teardown)
def test_schedule_event_source(cwd, temp_files, temp_roles):
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
    create_lambda_helper(lambda_name, role_arn,
                         './resources/sample_lambda/handler_counter.py',
                         lambda_handler='handler_counter.handle')

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

    delete_lambda(lambda_name)
    return {'temp_roles': [role_name]}


@attr('aws', 'slow')
@with_setup_args(_setup, _teardown)
def test_wire_unwire_lambda_with_s3(cwd, temp_files, temp_roles):
    log.info('running test_wire_unwire_lambda_with_s3')

    # create a lambda function
    temp_string = random_string()
    lambda_name = 'jenkins_test_%s' % temp_string
    role_name = 'unittest_%s_lambda' % temp_string
    role_arn = create_lambda_role_helper(role_name)
    create_lambda_helper(lambda_name, role_arn,
                         './resources/sample_lambda/handler_counter.py',
                         lambda_handler='handler_counter.handle')

    # create a bucket
    bucket_name = 'unittest-lambda-s3-event-source-%s' % temp_string
    create_bucket(bucket_name)

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

    # cleanup
    delete_bucket(bucket_name)
    delete_lambda(lambda_name)
    return {'temp_roles': [role_name]}


@attr('aws')
@with_setup_args(_setup, _teardown)
def test_lambda_add_invoke_permission(cwd, temp_files, temp_roles):
    log.info('running test_lambda_add_invoke_permission')

    # create a lambda function
    temp_string = random_string()
    lambda_name = 'jenkins_test_%s' % temp_string
    role_name = 'unittest_%s_lambda' % temp_string
    role_arn = create_lambda_role_helper(role_name)
    create_lambda_helper(lambda_name, role_arn,
                         './resources/sample_lambda/handler_counter.py',
                         lambda_handler='handler_counter.handle')

    # create a bucket
    bucket_name = 'unittest-lambda-s3-bucket-%s' % temp_string
    create_bucket(bucket_name)

    s3_arn = 'arn:aws:s3:::' + bucket_name
    response = _lambda_add_invoke_permission(
        lambda_name, 's3.amazonaws.com', s3_arn)

    # {"Statement":"{\"Condition\":{\"ArnLike\":{\"AWS:SourceArn\":\"arn:aws:s3:::unittest-lambda-s3-bucket-coedce\"}},\"Action\":[\"lambda:InvokeFunction\"],\"Resource\":\"arn:aws:lambda:eu-west-1:188084614522:function:jenkins_test_coedce:ACTIVE\",\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"s3.amazonaws.com\"},\"Sid\":\"07c77fac-68ff-11e6-97f8-c4850848610b\"}"}

    assert_not_in('Error', response)
    assert_in('lambda:InvokeFunction', response['Statement'])

    # cleanup
    delete_bucket(bucket_name)
    delete_lambda(lambda_name)
    return {'temp_roles': [role_name]}


@attr('aws')
@with_setup_args(_setup, _teardown)
def test_list_functions(cwd, temp_files, temp_roles):
    log.info('running test_list_functions')

    # create a lambda function
    temp_string = random_string()
    lambda_name = 'jenkins_test_%s' % temp_string
    role_name = 'unittest_%s_lambda' % temp_string
    role_arn = create_lambda_role_helper(role_name)
    create_lambda_helper(lambda_name, role_arn,
                         './resources/sample_lambda/handler_counter.py',
                         lambda_handler='handler_counter.handle')
    out = StringIO()
    list_functions(out)

    expected_regex = ".*%s\\n\\tMemory: 128\\n\\tTimeout: 300\\n\\tRole: arn:aws:iam::\d{12}:role\/%s\\n\\tCurrent Version: \$LATEST.*" \
                     % (lambda_name, role_name)

    assert_regexp_matches(out.getvalue().strip(), expected_regex)

    # cleanup
    delete_lambda(lambda_name)
    return {'temp_roles': [role_name]}


@attr('aws')
@with_setup_args(_setup, _teardown)
def test_update_lambda_configuration(cwd, temp_files, temp_roles):
    log.info('running test_update_lambda_configuration')

    # create a lambda function
    temp_string = random_string()
    lambda_name = 'jenkins_test_%s' % temp_string
    role_name = 'unittest_%s_lambda' % temp_string
    role_arn = create_lambda_role_helper(role_name)
    create_lambda_helper(lambda_name, role_arn,
                         './resources/sample_lambda/handler_counter.py',
                         lambda_handler='handler_counter.handle')

    handler_function = './resources/sample_lambda/handler_counter.py'
    lambda_description = 'lambda created for unittesting ramuda deployment'

    iam = boto3.client('iam')
    role_arn = iam.get_role(RoleName=role_name)['Role']['Arn']
    timeout = 300
    memory_size = 256

    function_version = _update_lambda_configuration(lambda_name, role_arn,
                                                    handler_function,
                                                    lambda_description, timeout,
                                                    memory_size)

    assert_equal(function_version, '$LATEST')

    # cleanup
    delete_lambda(lambda_name)
    return {'temp_roles': [role_name]}


@attr('aws')
@with_setup_args(_setup, _teardown)
def test_get_metrics(cwd, temp_files, temp_roles):
    log.info('running test_get_metrics')

    # create a lambda function
    temp_string = random_string()
    lambda_name = 'jenkins_test_%s' % temp_string
    role_name = 'unittest_%s_lambda' % temp_string
    role_arn = create_lambda_role_helper(role_name)
    create_lambda_helper(lambda_name, role_arn,
                         './resources/sample_lambda/handler_counter.py',
                         lambda_handler='handler_counter.handle')

    out = StringIO()
    get_metrics(lambda_name, out)
    assert_regexp_matches(out.getvalue().strip(), \
        'Duration 0\\n\\tErrors 0\\n\\tInvocations [0,1]{1}\\n\\tThrottles 0')

    # cleanup
    delete_lambda(lambda_name)
    return {'temp_roles': [role_name]}


@attr('aws')
@with_setup_args(_setup, _teardown)
def test_rollback(cwd, temp_files, temp_roles):
    log.info('running test_rollback')

    # create a lambda function
    temp_string = random_string()
    lambda_name = 'jenkins_test_%s' % temp_string
    role_name = 'unittest_%s_lambda' % temp_string
    role_arn = create_lambda_role_helper(role_name)
    create_lambda_helper(lambda_name, role_arn,
                         './resources/sample_lambda/handler_counter.py',
                         lambda_handler='handler_counter.handle')

    alias_version = _get_alias_version(lambda_name, 'ACTIVE')
    assert_equal(alias_version, '1')

    # update the function
    # TODO: do not recreate the role!
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

    # cleanup
    delete_lambda(lambda_name)
    return {'temp_roles': [role_name]}


@attr('aws')
@with_setup_args(_setup, _teardown)
def test_get_remote_code_hash(cwd, temp_files, temp_roles):
    log.info('running test_get_remote_code_hash')

    handler_filename = './resources/sample_lambda/handler_counter.py'
    folders_from_file = [
        {'source': './vendored', 'target': '.'},
        {'source': './impl', 'target': 'impl'}
    ]

    # get local hash
    zipfile = make_zip_file_bytes(handler=handler_filename,
                                  paths=folders_from_file)
    expected_hash = create_sha256(zipfile)

    # create a lambda function
    temp_string = random_string()
    lambda_name = 'jenkins_test_%s' % temp_string
    role_name = 'unittest_%s_lambda' % temp_string
    role_arn = create_lambda_role_helper(role_name)
    create_lambda_helper(lambda_name, role_arn,
                         handler_filename,
                         lambda_handler='handler_counter.handle')

    remote_hash = get_remote_code_hash(lambda_name)
    assert_equal(remote_hash, expected_hash)

    # cleanup
    delete_lambda(lambda_name)
    return {'temp_roles': [role_name]}
