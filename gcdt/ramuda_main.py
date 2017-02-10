#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""ramuda.
Script to deploy Lambda functions to AWS
"""

from __future__ import unicode_literals, print_function
import sys

from clint.textui import colored

from .config_reader import read_config_if_exists
from . import utils
from .logger import setup_logger
from .ramuda_core import list_functions, get_metrics, deploy_lambda, \
    wire, bundle_lambda, unwire, delete_lambda, rollback, ping, info, \
    cleanup_bundle
from .utils import read_gcdt_user_config, read_gcdt_user_config_value
from .monitoring import datadog_event_detail
from .gcdt_cmd_dispatcher import cmd
from . import gcdt_lifecycle

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

'''
def get_user_config():
    slack_token, slack_channel = read_gcdt_user_config(
        compatibility_mode='kumo')
    if not slack_token and not isinstance(slack_token, basestring):
        sys.exit(1)
    else:
        return slack_token, slack_channel
'''

@cmd(spec=['version'])
def version_cmd():
    utils.version()


@cmd(spec=['clean'])
def clean_cmd():
    return cleanup_bundle()


@cmd(spec=['list'])
def list_cmd(**tooldata):
    context = tooldata.get('context')
    awsclient = context.get('awsclient')
    return list_functions(awsclient)


@cmd(spec=['deploy'])
def deploy_cmd(**tooldata):
    context = tooldata.get('context')
    conf = tooldata.get('config')
    awsclient = context.get('awsclient')
    #slack_token, slack_channel = get_user_config()
    fail_deployment_on_unsuccessful_ping = read_gcdt_user_config_value(
        'ramuda.failDeploymentOnUnsuccessfulPing', False)
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
        awsclient, lambda_name, role_arn, handler_filename,
        lambda_handler, folders_from_file,
        lambda_description, timeout,
        memory_size, subnet_ids=subnet_ids,
        security_groups=security_groups,
        artifact_bucket=artifact_bucket,
        fail_deployment_on_unsuccessful_ping=
        fail_deployment_on_unsuccessful_ping,
        slack_token=context['slack_token'],
        slack_channel=context['slack_channel'],
        prebundle_scripts=prebundle_scripts,
        runtime=runtime
    )
    event = 'ramuda bot: deployed lambda function: %s ' % lambda_name
    datadog_event_detail(context, event)
    return exit_code


@cmd(spec=['metrics', '<lambda>'])
def metrics_cmd(lambda_name, **tooldata):
    context = tooldata.get('context')
    awsclient = context.get('awsclient')
    return get_metrics(awsclient, lambda_name)


@cmd(spec=['delete', '-f', '<lambda>'])
def delete_cmd(force, lambda_name, **tooldata):
    context = tooldata.get('context')
    awsclient = context.get('awsclient')
    #slack_token, slack_channel = get_user_config()
    conf = read_config_if_exists(awsclient, 'lambda')
    function_name = conf.get('lambda.name', None)
    if function_name == str(lambda_name):
        s3_event_sources = conf.get('lambda.events.s3Sources', [])
        time_event_sources = conf.get('lambda.events.timeSchedules', [])
        exit_code = delete_lambda(awsclient, lambda_name,
                                  s3_event_sources,
                                  time_event_sources,
                                  slack_token=context['slack_token'],
                                  slack_channel=context['slack_channel'])
    else:
        exit_code = delete_lambda(
            awsclient, lambda_name, [], [],
            slack_token=context['slack_token'],
            slack_channel=context['slack_channel'])
    event = 'ramuda bot: deleted lambda function: %s' % function_name
    datadog_event_detail(context, event)
    return exit_code


@cmd(spec=['info'])
def info_cmd(**tooldata):
    context = tooldata.get('context')
    conf = tooldata.get('config')
    awsclient = context.get('awsclient')
    # conf = read_lambda_config(awsclient)
    function_name = conf.get('lambda.name')
    s3_event_sources = conf.get('lambda.events.s3Sources', [])
    time_event_sources = conf.get('lambda.events.timeSchedules', [])
    return info(awsclient, function_name, s3_event_sources,
                time_event_sources)


@cmd(spec=['wire'])
def wire_cmd(**tooldata):
    context = tooldata.get('context')
    conf = tooldata.get('config')
    awsclient = context.get('awsclient')
    #slack_token, slack_channel = get_user_config()
    # conf = read_lambda_config(awsclient)
    function_name = conf.get('lambda.name')
    s3_event_sources = conf.get('lambda.events.s3Sources', [])
    time_event_sources = conf.get('lambda.events.timeSchedules', [])
    exit_code = wire(awsclient, function_name, s3_event_sources,
                     time_event_sources,
                     slack_token=context['slack_token'],
                     slack_channel=context['slack_channel'])
    event = ('ramuda bot: wiring lambda function: ' +
             '%s with alias %s' % (function_name, 'ACTIVE'))
    datadog_event_detail(context, event)
    return exit_code


@cmd(spec=['unwire'])
def unwire_cmd(**tooldata):
    context = tooldata.get('context')
    conf = tooldata.get('config')
    awsclient = context.get('awsclient')
    #slack_token, slack_channel = get_user_config()
    # conf = read_lambda_config(awsclient)
    function_name = conf.get('lambda.name')
    s3_event_sources = conf.get('lambda.events.s3Sources', [])
    time_event_sources = conf.get('lambda.events.timeSchedules', [])
    exit_code = unwire(awsclient, function_name, s3_event_sources,
                       time_event_sources,
                       slack_token=context['slack_token'],
                       slack_channel=context['slack_channel'])
    event = ('ramuda bot: UN-wiring lambda function: %s ' % function_name +
             'with alias %s' % 'ACTIVE')
    datadog_event_detail(context, event)
    return exit_code


@cmd(spec=['bundle'])
def bundle_cmd(**tooldata):
    context = tooldata.get('context')
    conf = tooldata.get('config')
    awsclient = context.get('awsclient')
    # conf = read_lambda_config(awsclient)
    runtime = conf.get('lambda.runtime', 'python2.7')
    handler_filename = conf.get('lambda.handlerFile')
    folders_from_file = conf.get('bundling.folders')
    prebundle_scripts = conf.get('bundling.preBundle', None)
    return bundle_lambda(awsclient, handler_filename,
                         folders_from_file, prebundle_scripts, runtime)


@cmd(spec=['rollback', '<lambda>', '<version>'])
def rollback_cmd(lambda_name, version, **tooldata):
    context = tooldata.get('context')
    # conf = tooldata.get('config')
    awsclient = context.get('awsclient')
    # are_credentials_still_valid(awsclient)
    #slack_token, slack_channel = get_user_config()
    if version:
        exit_code = rollback(awsclient, lambda_name, 'ACTIVE',
                             version,
                             slack_token=context['slack_token'],
                             slack_channel=context['slack_channel'])
        event = ('ramuda bot: rolled back lambda function: ' +
                 '%s to version %s' % (
                     lambda_name, version))
        datadog_event_detail(context, event)
    else:
        exit_code = rollback(awsclient, lambda_name, 'ACTIVE',
                             slack_token=context['slack_token'],
                             slack_channel=context['slack_channel'])
        event = ('ramuda bot: rolled back lambda function: %s to ' +
                 'previous version') % lambda_name
        datadog_event_detail(context, event)
    return exit_code


@cmd(spec=['ping', '<lambda>', '<version>'])
def ping_cmd(lambda_name, version=None, **tooldata):
    version = None
    context = tooldata.get('context')
    awsclient = context.get('awsclient')
    if version:
        response = ping(awsclient, lambda_name,
                        version=version)
    else:
        response = ping(awsclient, lambda_name)
    if response == '"alive"':
        print('Cool, your lambda function did respond to ping with %s.' %
              response)
    else:
        print(colored.red('Your lambda function did not respond to ping.'))
        return 1


if __name__ == '__main__':
    sys.exit(gcdt_lifecycle.main(DOC, 'ramuda',
                                 dispatch_only=['version', 'clean']))
