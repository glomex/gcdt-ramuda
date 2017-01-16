#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""ramuda.
Script to deploy Lambda functions to AWS
"""

from __future__ import print_function
import sys

import boto3
from docopt import docopt
from clint.textui import colored

from .config_reader import read_lambda_config, read_config_if_exists
from gcdt import utils
from gcdt.logger import setup_logger
from gcdt.ramuda_core import list_functions, get_metrics, deploy_lambda, \
    wire, bundle_lambda, unwire, delete_lambda, rollback, ping, info, \
    cleanup_bundle
from gcdt.utils import read_gcdt_user_config, get_context, get_command, \
    read_gcdt_user_config_value
from gcdt.monitoring import datadog_notification, datadog_error, \
    datadog_event_detail

log = setup_logger(logger_name='ramuda')

# TODO introduce own config for account detection
# TODO re-upload on requirements.txt changes
# TODO manage log groups
# TODO silence slacker
# TODO fill description with git commit, jenkins build or local info
# TODO wire to specific alias
# TODO retain only n versions

# creating docopt parameters and usage help
DOC = '''Usage:
        ramuda clean
        ramuda bundle
        ramuda deploy
        ramuda list
        ramuda metrics <lambda>
        ramuda info
        ramuda wire
        ramuda unwire
        ramuda delete  -f <lambda>
        ramuda rollback  <lambda> [<version>]
        ramuda ping <lambda> [<version>]
        ramuda version

Options:
-h --help           show this
'''


def are_credentials_still_valid(boto_session):
    """Wrapper to bail out on invalid credentials."""
    from gcdt.ramuda_utils import are_credentials_still_valid as acsv
    exit_code = acsv(boto_session)
    if exit_code:
        sys.exit(1)


def get_user_config():
    slack_token, slack_channel = read_gcdt_user_config(
        compatibility_mode='kumo')
    if not slack_token and not isinstance(slack_token, basestring):
        sys.exit(1)
    else:
        return slack_token, slack_channel


def main():
    exit_code = 0
    arguments = docopt(DOC)
    if arguments['version']:
        utils.version()
        sys.exit(0)

    boto_session = boto3.session.Session()
    context = get_context('ramuda', get_command(arguments))
    datadog_notification(context)
    if arguments['clean']:
        cleanup_bundle()
    elif arguments['list']:
        are_credentials_still_valid(boto_session)
        exit_code = list_functions(boto_session)
        log.debug('debug_test')
        log.info('info_test')
    elif arguments['metrics']:
        are_credentials_still_valid(boto_session)
        exit_code = get_metrics(boto_session, arguments['<lambda>'])
    elif arguments['deploy']:
        are_credentials_still_valid(boto_session)
        slack_token, slack_channel = get_user_config()
        fail_deployment_on_unsuccessful_ping = read_gcdt_user_config_value(
            'ramuda.failDeploymentOnUnsuccessfulPing', False)
        conf = read_lambda_config()
        lambda_name = conf.get('lambda.name')
        lambda_description = conf.get('lambda.description')
        role_arn = conf.get('lambda.role')
        lambda_handler = conf.get('lambda.handlerFunction')
        handler_filename = conf.get('lambda.handlerFile')
        timeout = int(conf.get_string('lambda.timeout'))
        memory_size = int(conf.get_string('lambda.memorySize'))
        folders_from_file = conf.get('bundling.folders')
        prebundle_scripts = conf.get('bundling.preBundle', None)
        subnet_ids = conf.get('lambda.vpc.subnetIds', None)
        security_groups = conf.get('lambda.vpc.securityGroups', None)
        artifact_bucket = conf.get('deployment.artifactBucket', None)
        runtime = conf.get('lambda.runtime', 'python2.7')
        exit_code = deploy_lambda(
            boto_session, lambda_name, role_arn, handler_filename,
            lambda_handler, folders_from_file,
            lambda_description, timeout,
            memory_size, subnet_ids=subnet_ids,
            security_groups=security_groups,
            artifact_bucket=artifact_bucket,
            fail_deployment_on_unsuccessful_ping=
            fail_deployment_on_unsuccessful_ping,
            slack_token=slack_token,
            slack_channel=slack_channel,
            prebundle_scripts=prebundle_scripts,
            runtime=runtime
        )
        event = 'ramuda bot: deployed lambda function: %s ' % lambda_name
        datadog_event_detail(context, event)
    elif arguments['delete']:
        are_credentials_still_valid(boto_session)
        slack_token, slack_channel = get_user_config()
        conf = read_config_if_exists('lambda')
        function_name = conf.get('lambda.name', None)
        if function_name == str(arguments['<lambda>']):
            s3_event_sources = conf.get('lambda.events.s3Sources', [])
            time_event_sources = conf.get('lambda.events.timeSchedules', [])
            exit_code = delete_lambda(boto_session, arguments['<lambda>'],
                                      s3_event_sources,
                                      time_event_sources,
                                      slack_token=slack_token,
                                      slack_channel=slack_channel)
        else:
            exit_code = delete_lambda(
                boto_session, arguments['<lambda>'], [], [],
                slack_token=slack_token, slack_channel=slack_channel)
        event = 'ramuda bot: deleted lambda function: %s' % function_name
        datadog_event_detail(context, event)
    elif arguments['info']:
        are_credentials_still_valid(boto_session)
        conf = read_lambda_config()
        function_name = conf.get('lambda.name')
        s3_event_sources = conf.get('lambda.events.s3Sources', [])
        time_event_sources = conf.get('lambda.events.timeSchedules', [])
        exit_code = info(boto_session, function_name, s3_event_sources,
                         time_event_sources)
    elif arguments['wire']:
        are_credentials_still_valid(boto_session)
        slack_token, slack_channel = get_user_config()
        conf = read_lambda_config()
        function_name = conf.get('lambda.name')
        s3_event_sources = conf.get('lambda.events.s3Sources', [])
        time_event_sources = conf.get('lambda.events.timeSchedules', [])
        exit_code = wire(boto_session, function_name, s3_event_sources,
                         time_event_sources,
                         slack_token=slack_token, slack_channel=slack_channel)
        event = ('ramuda bot: wiring lambda function: ' +
                 '%s with alias %s' % (function_name, 'ACTIVE'))
        datadog_event_detail(context, event)
    elif arguments['unwire']:
        are_credentials_still_valid(boto_session)
        slack_token, slack_channel = get_user_config()
        conf = read_lambda_config()
        function_name = conf.get('lambda.name')
        s3_event_sources = conf.get('lambda.events.s3Sources', [])
        time_event_sources = conf.get('lambda.events.timeSchedules', [])
        exit_code = unwire(boto_session, function_name, s3_event_sources,
                           time_event_sources,
                           slack_token=slack_token, slack_channel=slack_channel)
        event = ('ramuda bot: UN-wiring lambda function: %s ' % function_name +
                 'with alias %s' % 'ACTIVE')
        datadog_event_detail(context, event)
    elif arguments['bundle']:
        conf = read_lambda_config()
        runtime = conf.get('lambda.runtime', 'python2.7')
        handler_filename = conf.get('lambda.handlerFile')
        folders_from_file = conf.get('bundling.folders')
        prebundle_scripts = conf.get('bundling.preBundle', None)
        exit_code = bundle_lambda(handler_filename, folders_from_file,
                                  prebundle_scripts, runtime)
    elif arguments['rollback']:
        are_credentials_still_valid(boto_session)
        slack_token, slack_channel = get_user_config()
        if arguments['<version>']:
            exit_code = rollback(boto_session, arguments['<lambda>'], 'ACTIVE',
                                 arguments['<version>'],
                                 slack_token=slack_token,
                                 slack_channel=slack_channel)
            event = ('ramuda bot: rolled back lambda function: ' +
                     '%s to version %s' % (
                        arguments['<lambda>'], arguments['<version>']))
            datadog_event_detail(context, event)
        else:
            exit_code = rollback(boto_session, arguments['<lambda>'], 'ACTIVE',
                                 slack_token=slack_token,
                                 slack_channel=slack_channel)
            event = ('ramuda bot: rolled back lambda function: %s to ' +
                     'previous version') % arguments['<lambda>']
            datadog_event_detail(context, event)
    elif arguments['ping']:
        are_credentials_still_valid(boto_session)
        if arguments['<version>']:
            response = ping(boto_session, arguments['<lambda>'],
                            version=arguments['<version>'])
        else:
            response = ping(boto_session, arguments['<lambda>'])
        if not response == '"alive"':
            exit_code = 1
            print(colored.red('Your lambda function did not respond to ping.'))

    if exit_code:
        datadog_error(context)
    sys.exit(exit_code)


if __name__ == '__main__':
    main()
