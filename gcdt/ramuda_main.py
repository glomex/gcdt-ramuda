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
from .ramuda_core import list_functions, get_metrics, deploy_lambda, \
    wire, bundle_lambda, unwire, delete_lambda, rollback, ping, info, \
    cleanup_bundle
from .gcdt_cmd_dispatcher import cmd
from . import gcdt_lifecycle


# TODO introduce own config for account detection
# TODO re-upload on requirements.txt changes
# TODO manage log groups
# TODO fill description with git commit, jenkins build or local info
# TODO wire to specific alias
# TODO retain only n versions

# creating docopt parameters and usage help
DOC = '''Usage:
        ramuda clean
        ramuda bundle [-v]
        ramuda deploy [-v]
        ramuda list
        ramuda metrics <lambda>
        ramuda info
        ramuda wire [-v]
        ramuda unwire [-v]
        ramuda delete [-v] -f <lambda>
        ramuda rollback [-v] <lambda> [<version>]
        ramuda ping [-v] <lambda> [<version>]
        ramuda version

Options:
-h --help           show this
-v --verbose        show debug messages
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
    awsclient = context.get('_awsclient')
    return list_functions(awsclient)


@cmd(spec=['deploy'])
def deploy_cmd(**tooldata):
    context = tooldata.get('context')
    conf = tooldata.get('config')
    awsclient = context.get('_awsclient')
    #fail_deployment_on_unsuccessful_ping = read_gcdt_user_config_value(
    #    'ramuda.failDeploymentOnUnsuccessfulPing', False)
    fail_deployment_on_unsuccessful_ping = False
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
        prebundle_scripts=prebundle_scripts,
        runtime=runtime
    )
    return exit_code


@cmd(spec=['metrics', '<lambda>'])
def metrics_cmd(lambda_name, **tooldata):
    context = tooldata.get('context')
    awsclient = context.get('_awsclient')
    return get_metrics(awsclient, lambda_name)


@cmd(spec=['delete', '-f', '<lambda>'])
def delete_cmd(force, lambda_name, **tooldata):
    context = tooldata.get('context')
    awsclient = context.get('_awsclient')
    conf = read_config_if_exists(awsclient, 'lambda')
    function_name = conf.get('lambda.name', None)
    if function_name == str(lambda_name):
        s3_event_sources = conf.get('lambda.events.s3Sources', [])
        time_event_sources = conf.get('lambda.events.timeSchedules', [])
        exit_code = delete_lambda(awsclient, lambda_name,
                                  s3_event_sources,
                                  time_event_sources)
    else:
        exit_code = delete_lambda(
            awsclient, lambda_name, [], [])
    return exit_code


@cmd(spec=['info'])
def info_cmd(**tooldata):
    context = tooldata.get('context')
    conf = tooldata.get('config')
    awsclient = context.get('_awsclient')
    function_name = conf.get('lambda.name')
    s3_event_sources = conf.get('lambda.events.s3Sources', [])
    time_event_sources = conf.get('lambda.events.timeSchedules', [])
    return info(awsclient, function_name, s3_event_sources,
                time_event_sources)


@cmd(spec=['wire'])
def wire_cmd(**tooldata):
    context = tooldata.get('context')
    conf = tooldata.get('config')
    awsclient = context.get('_awsclient')
    function_name = conf.get('lambda.name')
    s3_event_sources = conf.get('lambda.events.s3Sources', [])
    time_event_sources = conf.get('lambda.events.timeSchedules', [])
    exit_code = wire(awsclient, function_name, s3_event_sources,
                     time_event_sources)
    return exit_code


@cmd(spec=['unwire'])
def unwire_cmd(**tooldata):
    context = tooldata.get('context')
    conf = tooldata.get('config')
    awsclient = context.get('_awsclient')
    function_name = conf.get('lambda.name')
    s3_event_sources = conf.get('lambda.events.s3Sources', [])
    time_event_sources = conf.get('lambda.events.timeSchedules', [])
    exit_code = unwire(awsclient, function_name, s3_event_sources,
                       time_event_sources)
    return exit_code


@cmd(spec=['bundle'])
def bundle_cmd(**tooldata):
    context = tooldata.get('context')
    conf = tooldata.get('config')
    awsclient = context.get('_awsclient')
    runtime = conf.get('lambda.runtime', 'python2.7')
    handler_filename = conf.get('lambda.handlerFile')
    folders_from_file = conf.get('bundling.folders')
    prebundle_scripts = conf.get('bundling.preBundle', None)
    return bundle_lambda(awsclient, handler_filename,
                         folders_from_file, prebundle_scripts, runtime)


@cmd(spec=['rollback', '<lambda>', '<version>'])
def rollback_cmd(lambda_name, version, **tooldata):
    context = tooldata.get('context')
    awsclient = context.get('_awsclient')
    if version:
        exit_code = rollback(awsclient, lambda_name, 'ACTIVE',
                             version)
    else:
        exit_code = rollback(awsclient, lambda_name, 'ACTIVE')
    return exit_code


@cmd(spec=['ping', '<lambda>', '<version>'])
def ping_cmd(lambda_name, version=None, **tooldata):
    version = None
    context = tooldata.get('context')
    awsclient = context.get('_awsclient')
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


def main():
    sys.exit(gcdt_lifecycle.main(DOC, 'ramuda',
                                 dispatch_only=['version', 'clean']))


if __name__ == '__main__':
    main()
