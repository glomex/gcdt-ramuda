# -*- coding: utf-8 -*-
from __future__ import print_function
import os
import json
import time
import boto3
import textwrap
from pyhocon import ConfigFactory
import nose
from nose.tools import assert_true, assert_equal, assert_greater_equal
from nose.plugins.attrib import attr
from .helpers import check_preconditions, random_string, with_setup_args
from gcdt.ramuda_core import delete_lambda, deploy_lambda, \
    _lambda_add_time_schedule_event_source, _lambda_add_invoke_permission, \
    wire, unwire

from gcdt.logger import setup_logger

log = setup_logger(logger_name='ramuda_test_aws')
SLACK_TOKEN = '***REMOVED***'


def here(p): return os.path.join(os.path.dirname(__file__), p)


def get_size(start_path='.'):
    total_size = 0
    for dirpath, dirnames, filenames in os.walk(start_path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            total_size += os.path.getsize(fp)
    return total_size


def _delete_role(role_name):
    """Delete the testing role.

    :param role: the temporary role that has been created via _create_role
    """
    # role_name = role['RoleName']
    iam = boto3.client('iam')
    roles = [r['RoleName'] for r in iam.list_roles()['Roles']]
    if role_name in roles:
        # detach all policies first
        policies = iam.list_attached_role_policies(RoleName=role_name)
        for p in policies['AttachedPolicies']:
            response = iam.detach_role_policy(
                RoleName=role_name,
                PolicyArn=p['PolicyArn']
            )

        # delete the role
        response = iam.delete_role(RoleName=role_name)


def _create_role(name, policies=None):
    """Create a role with an optional inline policy """
    iam = boto3.client('iam')
    policy_doc = {
        'Version': '2012-10-17',
        'Statement': [
            {
                'Effect': 'Allow',
                'Principal': {'Service': ['lambda.amazonaws.com']},
                'Action': ['sts:AssumeRole']
            },
        ]
    }
    roles = [r['RoleName'] for r in iam.list_roles()['Roles']]
    if name in roles:
        print('IAM role %s exists' % name)
        role = iam.get_role(RoleName=name)['Role']
    else:
        print('Creating IAM role %s' % name)
        role = iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(policy_doc)
        )['Role']

    # attach managed policy
    if policies is not None:
        for p in policies:
            iam.attach_role_policy(RoleName=role['RoleName'], PolicyArn=p)

    # TODO: on 20160816 we had multiple times that the role could not be assigned
    # we suspect that this is a timing issue with AWS lambda
    # get_role to make sure role is available for lambda
    # response = iam.list_attached_role_policies(RoleName=name)
    # log.info('created role: %s' % name)
    # log.info(response)
    # ClientError: An error occurred (InvalidParameterValueException) when
    # calling the CreateFunction operation: The role defined for the function
    # cannot be assumed by Lambda.
    # current assumption is that the role is not propagated to lambda in time
    time.sleep(10)

    return role


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
        _delete_role(r)


@attr('aws')
@with_setup_args(_setup, _teardown)
def test_create_lambda(cwd, temp_files, temp_roles):
    log.info('running test_create_lambda')
    temp_string = random_string()
    lambda_name = 'jenkins_test_' + temp_string
    log.info(lambda_name)
    role = _create_role(
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
    role = _create_role(
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
    _create_lambda_helper(lambda_name, role_name,
                          './resources/sample_lambda/handler.py')
    # update the function
    # TODO: do not recreate the role!
    _create_lambda_helper(lambda_name, role_name,
                          './resources/sample_lambda/handler_v2.py')

    # delete the function
    delete_lambda(lambda_name)
    return {'temp_roles': [role_name]}


def _create_lambda_helper(lambda_name, role_name, handler_filename,
                          lambda_handler='handler.handle'):
    # caller needs to clean up both lambda and role!
    role = _create_role(
        role_name,
        policies=[
            'arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole',
            'arn:aws:iam::aws:policy/AWSLambdaExecute']
    )

    lambda_description = 'lambda created for unittesting ramuda deployment'
    role_arn = role['Arn']
    # lambda_handler = 'handler.handle'
    timeout = 300
    memory_size = 128
    folders_from_file = [
        {'source': './vendored', 'target': '.'},
        {'source': './impl', 'target': 'impl'}
    ]
    artifact_bucket = None

    # create the function
    deploy_lambda(function_name=lambda_name,
                  role=role_arn,
                  handler_filename=handler_filename,
                  handler_function=lambda_handler,
                  folders=folders_from_file,
                  description=lambda_description,
                  timeout=timeout,
                  memory=memory_size,
                  artifact_bucket=artifact_bucket)


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
    _create_lambda_helper(lambda_name, role_name,
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


@attr('aws')
@with_setup_args(_setup, _teardown)
def test_wire_unwire_lambda_with_s3(cwd, temp_files, temp_roles):
    log.info('running test_wire_unwire_lambda_with_s3')

    # inner helpers
    # bucket helpers (parts borrowed from tenkai)
    def _create_bucket(bucket):
        client = boto3.client('s3')
        client.create_bucket(
            Bucket=bucket,
            CreateBucketConfiguration={
                'LocationConstraint': 'eu-west-1'
            }
        )

    def _delete_bucket(bucket):
        log.debug('deleting bucket %s' % bucket)
        if bucket.startswith('unittest-'):
            s3 = boto3.resource('s3')
            # delete all objects first
            bu = s3.Bucket(bucket)
            log.debug('deleting keys')
            for key in bu.objects.all():
                log.debug('deleting key: %s' % key)
                key.delete()
            log.debug('deleting bucket')
            # now we can delete the bucket
            bu.delete()

    # create a lambda function
    temp_string = random_string()
    lambda_name = 'jenkins_test_%s' % temp_string
    role_name = 'unittest_%s_lambda' % temp_string
    _create_lambda_helper(lambda_name, role_name,
                          './resources/sample_lambda/handler_counter.py',
                          lambda_handler='handler_counter.handle')

    # create a bucket
    bucket_name = 'unittest-lambda-s3-event-source-%s' % temp_string
    _create_bucket(bucket_name)

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
    exit_code = wire(lambda_name, s3_event_sources, time_event_sources,
                     slack_token=SLACK_TOKEN)
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
    exit_code = unwire(lambda_name, s3_event_sources, time_event_sources,
                       slack_token=SLACK_TOKEN)
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
    _delete_bucket(bucket_name)
    delete_lambda(lambda_name)
    return {'temp_roles': [role_name]}
