from __future__ import print_function
import sys

# sys.path.append("../gcdt/")

from unittest import TestCase, main
# import unittest
# import io
# import os
# from zipfile import ZipFile
from gcdt.ramuda_tool import install_dependencies_with_pip, delete_lambda, deploy_lambda
# from gcdt.ramuda_tool import update_lambda_function_code, get_metrics, create_lambda,\
# deploy_alias, update_lambda, rollback

from ramuda_utils import are_credentials_still_valid, lambda_exists, get_packages_to_ignore, cleanup_folder
import boto3
# from pyspin.spin import make_spin, Default
import shutil
import random
import string
from pyhocon import ConfigFactory
from gcdt.logger import log_json, setup_logger
# from gcdt.iam import IAMRoleAndPolicies
# from gcdt.kumo_util import StackLookup
import json

log = setup_logger(logger_name='RamudaTestCase')

import os


def get_size(start_path='.'):
    total_size = 0
    for dirpath, dirnames, filenames in os.walk(start_path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            total_size += os.path.getsize(fp)
    return total_size


# TODO
# setup random function name
# tear down function
# testspinner
# rename to test_butaris


class RamudaTestCase(TestCase):
    temp_string = ''.join([random.choice(string.ascii_lowercase)
                           for n in xrange(6)])
    temporary_function_name = 'jenkins_test_' + temp_string

    ###

    def create_role(self, name, policies=None):
        iam = boto3.client('iam')
        """ Create a role with an optional inline policy """
        policydoc = {
            'Version': '2012-10-17',
            'Statement': [
                {'Effect': 'Allow', 'Principal': {'Service': ['lambda.amazonaws.com']},
                 'Action': ['sts:AssumeRole']},
            ]
        }
        roles = [r['RoleName'] for r in iam.list_roles()['Roles']]
        if name in roles:
            print('IAM role %s exists' % name)
            role = iam.get_role(RoleName=name)['Role']
        else:
            print('Creating IAM role %s' % name)
            role = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(policydoc))['Role']

        # attach managed policy
        if policies is not None:
            for p in policies:
                iam.attach_role_policy(RoleName=role['RoleName'], PolicyArn=p)
        return role

    def setUp(self):
        os.environ['ENV'] = 'DEV'
        shutil.rmtree(os.getcwdu() + '/resources/vendored', ignore_errors=True)
        shutil.rmtree(os.getcwdu() + '/vendored', ignore_errors=True)
        os.mkdir(os.getcwdu() + '/resources/vendored')
        log.info(self.temporary_function_name)
        with open('settings_dev.conf', 'w') as settings:
            setting_string = """sample_lambda {
                            cw_name = "dp-dev-sample"
                            }"""
            settings.write(setting_string)
        os.mkdir(os.getcwdu() + '/vendored')

    def tearDown(self):
        shutil.rmtree(os.getcwdu() + '/resources/vendored')
        shutil.rmtree(os.getcwdu() + '/vendored')
        os.remove('settings_dev.conf')

    def test_install_dependencies_with_pip(self):
        log.info(install_dependencies_with_pip(os.getcwdu() + '/resources/requirements.txt',
                                               os.getcwdu() + '/resources/vendored'))
        packages = os.listdir(os.getcwdu() + '/resources/vendored')
        for package in packages:
            log.debug(package)
        self.assertTrue('werkzeug' in packages)
        self.assertTrue('troposphere' in packages)
        self.assertTrue('boto3' in packages)

    def test_create_lambda(self):
        log.info('running test_create_lambda')
        role = self.create_role(self.temp_string + '_lambda',
                                policies=['arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole',
                                          'arn:aws:iam::aws:policy/AWSLambdaExecute'])

        config_string = """lambda {
                                    name = "dp-dev-sample-lambda-jobr1"
                                    description = "lambda test for ramuda"
                                    role = 'unused'
                                    handlerFunction = "handler.handle"
                                    handlerFile = "./resources/sample_lambda/handler.py"
                                    timeout = 300
                                    memorySize = 256

                                    events {
                                    s3Sources = [
                                        { bucket = "jobr-test", type = "s3:ObjectCreated:*" , suffix=".gz"}
                                    ]
                                    timeSchedules = [
                                       {
                                           ruleName = "dp-dev-sample-lambda-jobr-T1",
                                           ruleDescription = "run every 5 min from 0-5",
                                           scheduleExpression = "cron(0/5 0-5 ? * * *)"
                                       },
                                       {
                                           ruleName = "dp-dev-sample-lambda-jobr-T2",
                                           ruleDescription = "run every 5 min from 8-23:59",
                                           scheduleExpression = "cron(0/5 8-23:59 ? * * *)"
                                       }
                                   ]
                                  }


                                    vpc  {
                                      subnetIds = ["subnet-87685dde", "subnet-9f39ccfb", "subnet-166d7061"]
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
        conf = ConfigFactory.parse_string(config_string)
        lambda_name = self.temporary_function_name
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

        delete_lambda(self.temporary_function_name)

    def test_create_lambda_with_s3(self):
        log.info('running test_create_lambda_withs3')
        role = self.create_role(self.temp_string + '_lambda',
                                policies=['arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole',
                                          'arn:aws:iam::aws:policy/AWSLambdaExecute'])

        config_string = """lambda {
                                        name = "dp-dev-sample-lambda-jobr1"
                                        description = "lambda test for ramuda"
                                        role = "arn:aws:iam::644239850139:role/7f-selfassign/dp-dev-CommonLambdaRole-J0BHM7LHBTG3"
                                        handlerFunction = "handler.handle"
                                        handlerFile = "./resources/sample_lambda/handler.py"
                                        timeout = 300
                                        memorySize = 256

                                        events {
                                        s3Sources = [
                                            { bucket = "jobr-test", type = "s3:ObjectCreated:*" , suffix=".gz"}
                                        ]
                                        timeSchedules = [
                                           {
                                               ruleName = "dp-dev-sample-lambda-jobr-T1",
                                               ruleDescription = "run every 5 min from 0-5",
                                               scheduleExpression = "cron(0/5 0-5 ? * * *)"
                                           },
                                           {
                                               ruleName = "dp-dev-sample-lambda-jobr-T2",
                                               ruleDescription = "run every 5 min from 8-23:59",
                                               scheduleExpression = "cron(0/5 8-23:59 ? * * *)"
                                           }
                                       ]
                                      }


                                        vpc  {
                                          subnetIds = ["subnet-87685dde", "subnet-9f39ccfb", "subnet-166d7061"]
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
                                        artifactBucket = "7finity-dp-dev-deployment"

                                    }
                                """
        conf = ConfigFactory.parse_string(config_string)
        lambda_name = self.temporary_function_name
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

        delete_lambda(self.temporary_function_name)

    def test_get_packages_to_ignore(self):
        test_folder = os.getcwdu() + '/resources/vendored'
        log.info(install_dependencies_with_pip(os.getcwdu() + '/resources/requirements.txt',
                                               test_folder))
        packages = os.listdir(test_folder)
        log.info('packages in test folder:')
        for package in packages:
            log.debug(package)
        matches = get_packages_to_ignore(test_folder)
        log.info('matches in test folder:')
        for match in sorted(matches):
            log.debug(match)
        self.assertTrue('boto3/__init__.py' in matches)
        self.assertFalse('werkzeug' in matches)

    def test_cleanup_folder(self):
        test_folder = os.getcwdu() + '/resources/vendored'
        log.info(install_dependencies_with_pip(os.getcwdu() + '/resources/requirements.txt',
                                               test_folder))

        log.info(get_size(test_folder))
        cleanup_folder(test_folder)
        log.info(get_size(test_folder))
        packages = os.listdir(test_folder)
        log.debug(packages)
        self.assertTrue('boto3' not in packages)


if __name__ == "__main__":
    main()
