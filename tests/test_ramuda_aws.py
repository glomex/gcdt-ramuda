from __future__ import print_function
import os
import json
import random
import string
import time
import boto3
import textwrap
from pyhocon import ConfigFactory
import nose
from nose.tools import assert_true
from helpers import with_setup_args
from gcdt.ramuda_core import delete_lambda, deploy_lambda
from gcdt.logger import setup_logger
from helpers import check_preconditions

log = setup_logger(logger_name='RamudaTestCase')


def here(p): return os.path.join(os.path.dirname(__file__), p)


def get_size(start_path='.'):
    total_size = 0
    for dirpath, dirnames, filenames in os.walk(start_path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            total_size += os.path.getsize(fp)
    return total_size


# TODO: cleanup role after testrun!
def _create_role(name, policies=None):
    """ Create a role with an optional inline policy """
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

    # TODO: on 20160816 we had multiple times that the role can not be assigned
    # we suspect that this is a timing issue with AWS lambda
    # get_role to make sure role is available for lambda
    # response = iam.list_attached_role_policies(RoleName=name)
    # log.info('created role: %s' % name)
    # log.info(response)
    # ClientError: An error occurred (InvalidParameterValueException) when
    # calling the CreateFunction operation: The role defined for the function
    # cannot be assumed by Lambda.
    # current assumption is that the role is not propagated to lambda
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
    return {'cwd': cwd, 'temp_files': temp_files}


def _teardown(cwd, temp_files):
    os.chdir(cwd)
    # shutil.rmtree(folder)  # reuse it
    for t in temp_files:
        os.unlink(t)


@with_setup_args(_setup, _teardown)
def test_create_lambda(cwd, temp_files):
    log.info('running test_create_lambda')
    temp_string = ''.join([random.choice(string.ascii_lowercase)
                           for n in xrange(6)])
    lambda_name = 'jenkins_test_' + temp_string
    log.info(lambda_name)
    role = _create_role(
        'unittest_%s_lambda' % temp_string,
        policies=[
            'arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole',
            'arn:aws:iam::aws:policy/AWSLambdaExecute']
    )

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


@with_setup_args(_setup, _teardown)
def test_create_lambda_with_s3(cwd, temp_files):
    log.info('running test_create_lambda_with_s3')
    account = os.getenv('ACCOUNT')
    temp_string = ''.join([random.choice(string.ascii_lowercase)
                           for n in xrange(6)])
    lambda_name = 'jenkins_test_' + temp_string
    log.info(lambda_name)
    role = _create_role(
        'unittest_%s_lambda' % temp_string,
        policies=[
            'arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole',
            'arn:aws:iam::aws:policy/AWSLambdaExecute'])

    config_string = textwrap.dedent("""\
        lambda {
            name = "dp-dev-sample-lambda-jobr1"
            description = "lambda test for ramuda"
            #role = "arn:aws:iam::644239850139:role/7f-selfassign/dp-dev-CommonLambdaRole-J0BHM7LHBTG3"
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


@with_setup_args(_setup, _teardown)
def test_update_lambda(cwd, temp_files):
    log.info('running test_update_lambda')
    temp_string = ''.join([random.choice(string.ascii_lowercase)
                           for n in xrange(6)])
    lambda_name = 'jenkins_test_' + temp_string
    log.info('deploying %s' % lambda_name)
    role = _create_role(
        'unittest_%s_lambda' % temp_string,
        policies=[
            'arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole',
            'arn:aws:iam::aws:policy/AWSLambdaExecute']
    )

    lambda_description = 'lambda created for unittesting ramuda deployment'
    # role_arn = 'arn:aws:iam::188084614522:role/unittest_winluj_lambda'
    role_arn = role['Arn']
    lambda_handler = 'handler.handle'
    handler_filename = './resources/sample_lambda/handler.py'
    timeout = 300
    memory_size = 256
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

    # update the function
    log.info('updating %s' % lambda_name)
    handler_filename = './resources/sample_lambda/handler_v2.py'
    lambda_description = 'lambda update for unittesting ramuda deployment'
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
