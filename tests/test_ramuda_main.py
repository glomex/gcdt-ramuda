# -*- coding: utf-8 -*-
from __future__ import unicode_literals, print_function
import os
import logging
import textwrap

import pytest
from nose.tools import assert_regexp_matches
from pyhocon import ConfigFactory

from gcdt.ramuda_main import version_cmd, clean_cmd, list_cmd, deploy_cmd, \
    delete_cmd, metrics_cmd, ping_cmd

from .helpers_aws import check_preconditions, get_tooldata
from .helpers_aws import create_role_helper
from .helpers_aws import awsclient  # fixtures!
from .test_ramuda_aws import vendored_folder, temp_lambda, cleanup_lambdas  # fixtures!
from .test_ramuda_aws import cleanup_roles  # fixtures!
from .helpers import temp_folder  # fixtures !
from . import helpers, here

# note: xzy_main tests have a more "integrative" character so focus is to make
# sure that the gcdt parts fit together not functional coverage of the parts.
log = logging.getLogger(__name__)


def test_version_cmd(capsys):
    version_cmd()
    out, err = capsys.readouterr()
    assert out.startswith('gcdt version')


def test_clean_cmd(temp_folder):
    os.environ['ENV'] = 'DEV'
    paths_to_clean = ['vendored', 'bundle.zip']
    for path in paths_to_clean:
        if path.find('.') != -1:
            open(path, 'a').close()
        else:
            os.mkdir(path)
    clean_cmd()
    for path in paths_to_clean:
        assert not os.path.exists(path)


@pytest.mark.aws
@check_preconditions
def test_list_cmd(awsclient, vendored_folder, temp_lambda, capsys):
    log.info('running test_list_cmd')
    tooldata = get_tooldata(awsclient, 'ramuda', 'list', config={})

    lambda_name = temp_lambda[0]
    role_name = temp_lambda[1]

    list_cmd(**tooldata)
    out, err = capsys.readouterr()

    expected_regex = ".*%s\\n\\tMemory: 128\\n\\tTimeout: 300\\n\\tRole: arn:aws:iam::\d{12}:role\/%s\\n\\tCurrent Version: \$LATEST.*" \
                     % (lambda_name, role_name)

    assert_regexp_matches(out.strip(), expected_regex)


@pytest.mark.aws
@check_preconditions
def test_deploy_delete_cmds(awsclient, vendored_folder, cleanup_roles):
    log.info('running test_create_lambda')
    temp_string = helpers.random_string()
    lambda_name = 'jenkins_test_' + temp_string
    log.info(lambda_name)
    role = create_role_helper(
        awsclient,
        'unittest_%s_lambda' % temp_string,
        policies=[
            'arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole',
            'arn:aws:iam::aws:policy/AWSLambdaExecute']
    )
    cleanup_roles.append(role['RoleName'])

    config_string = textwrap.dedent("""\
        lambda {
            name = 'tbd'
            description = "lambda test for ramuda"
            role = 'tbd'
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
    conf['lambda']['role'] = role['Arn']
    conf['lambda']['name'] = lambda_name

    tooldata = get_tooldata(awsclient, 'ramuda', 'list', config=conf)
    deploy_cmd(**tooldata)

    tooldata['config']['command'] = 'delete'
    delete_cmd(True, lambda_name, **tooldata)


@pytest.mark.aws
@check_preconditions
def test_metrics_cmd(awsclient, vendored_folder, temp_lambda, capsys):
    log.info('running test_metrics_cmd')
    tooldata = get_tooldata(awsclient, 'ramuda', 'metrics', config={})

    lambda_name = temp_lambda[0]
    metrics_cmd(lambda_name, **tooldata)
    out, err = capsys.readouterr()
    assert_regexp_matches(out.strip(),
                          'Duration 0\\n\\tErrors 0\\n\\tInvocations [0,1]{1}\\n\\tThrottles 0')


@pytest.mark.aws
@check_preconditions
def test_ping_cmd(awsclient, vendored_folder, temp_lambda, capsys):
    log.info('running test_ping_cmd')
    tooldata = get_tooldata(awsclient, 'ramuda', 'ping', config={})
        #config_base_name='settings_large',
        #location=here('./resources/sample_lambda/'))

    lambda_name = temp_lambda[0]
    ping_cmd(lambda_name, **tooldata)
    out, err = capsys.readouterr()
    assert '"alive"' in out
