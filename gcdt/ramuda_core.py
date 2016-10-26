# -*- coding: utf-8 -*-

"""ramuda.
Script to deploy Lambda functions to AWS
"""

from __future__ import print_function
import sys
import os, shutil
import subprocess
import uuid
import time
from datetime import datetime, timedelta
import json

from botocore.exceptions import ClientError as ClientError
from clint.textui import colored

from gcdt import monitoring
from gcdt.ramuda_utils import make_zip_file_bytes, json2table, s3_upload, \
    lambda_exists, create_sha256, get_remote_code_hash, unit, \
    aggregate_datapoints, check_buffer_exceeds_limit, list_of_dict_equals, \
    create_aws_s3_arn, get_bucket_from_s3_arn, get_rule_name_from_event_arn, \
    build_filter_rules
from gcdt.logger import setup_logger

log = setup_logger(logger_name='ramuda_core')
ALIAS_NAME = 'ACTIVE'
ENSURE_OPTIONS = ['absent', 'exists']


def _create_alias(boto_session, function_name, function_version,
                  alias_name=ALIAS_NAME):
    client_lambda = boto_session.client('lambda')
    response = client_lambda.create_alias(
        FunctionName=function_name,
        Name=alias_name,
        FunctionVersion=function_version,

    )
    return response['AliasArn']


def _update_alias(boto_session, function_name, function_version,
                  alias_name=ALIAS_NAME):
    client_lambda = boto_session.client('lambda')
    response = client_lambda.update_alias(
        FunctionName=function_name,
        Name=alias_name,
        FunctionVersion=function_version,

    )
    return response['AliasArn']


def _alias_exists(boto_session, function_name, alias_name):
    client_lambda = boto_session.client('lambda')
    try:
        client_lambda.get_alias(
            FunctionName=function_name,
            Name=alias_name
        )
        return True
    except Exception:
        return False


def _get_alias_version(boto_session, function_name, alias_name):
    # this is used for testing - it returns the version
    client_lambda = boto_session.client('lambda')
    try:
        response = client_lambda.get_alias(
            FunctionName=function_name,
            Name=alias_name
        )
        return response['FunctionVersion']
    except Exception:
        return


def _get_version_from_response(func):
    version = func['Version']
    return int(version) if version.isdigit() else 0


def _get_previous_version(boto_session, function_name, alias_name):
    client_lambda = boto_session.client('lambda')
    response = client_lambda.get_alias(
        FunctionName=function_name,
        Name=alias_name
    )
    current_version = response['FunctionVersion']
    if current_version != '$LATEST':
        return str(int(current_version) - 1)

    max_version = 0
    marker = None
    request_more_versions = True
    while request_more_versions:
        kwargs = {'Marker': marker} if marker else {}
        response = client_lambda.list_versions_by_function(
            FunctionName=function_name, **kwargs)
        if 'Marker' not in response:
            request_more_versions = False
        else:
            marker = response['Marker']
        versions = map(_get_version_from_response, response['Versions'])
        versions.append(max_version)
        max_version = max(versions)
    return str(max(0, max_version - 1))


def _deploy_alias(boto_session, function_name, function_version,
                  alias_name=ALIAS_NAME):
    if _alias_exists(boto_session, function_name, alias_name):
        _update_alias(boto_session, function_name, function_version, alias_name)
    else:
        _create_alias(boto_session, function_name, function_version, alias_name)


def _lambda_add_time_schedule_event_source(boto_session, rule_name,
                                           rule_description,
                                           schedule_expression, lambda_arn):
    client_event = boto_session.client('events')
    client_event.put_rule(
        Name=rule_name,
        ScheduleExpression=schedule_expression,
        Description=rule_description,
    )
    rule_response = client_event.describe_rule(Name=rule_name)
    if rule_response is not None:
        client_event.put_targets(
            Rule=rule_name,
            Targets=[
                {
                    'Id': '1',
                    'Arn': lambda_arn,
                },
            ]
        )

    return rule_response['Arn']


def _lambda_add_invoke_permission(boto_session, function_name,
                                  source_principal,
                                  source_arn, alias_name=ALIAS_NAME):
    # https://www.getoto.net/noise/2015/08/20/better-together-amazon-ecs-and-aws-lambda/
    # http://docs.aws.amazon.com/cli/latest/reference/lambda/add-permission.html
    client_lambda = boto_session.client('lambda')
    response = client_lambda.add_permission(
        FunctionName=function_name,
        StatementId=str(uuid.uuid1()),
        Action='lambda:InvokeFunction',
        Principal=source_principal,
        SourceArn=source_arn,
        Qualifier=alias_name
    )
    return response


def _lambda_add_s3_event_source(boto_session, arn, event, bucket, prefix,
                                suffix):
    """Use only prefix OR suffix

    :param arn:
    :param event:
    :param bucket:
    :param prefix:
    :param suffix:
    :return:
    """
    filter_rules = []
    json_data = {
        'LambdaFunctionConfigurations': [{
            'LambdaFunctionArn': arn,
            'Id': str(uuid.uuid1()),
            'Events': [event]
        }]
    }

    filter_rules = build_filter_rules(prefix, suffix)

    json_data['LambdaFunctionConfigurations'][0].update({
        'Filter': {
            'Key': {
                'FilterRules': filter_rules
            }
        }
    })
    # http://docs.aws.amazon.com/cli/latest/reference/s3api/put-bucket-notification-configuration.html
    # http://docs.aws.amazon.com/AmazonS3/latest/dev/NotificationHowTo.html
    resource_s3 = boto_session.resource('s3')
    client_s3 = boto_session.client('s3')

    bucket_notification = resource_s3.BucketNotification(bucket)
    bucket_configurations = client_s3.get_bucket_notification_configuration(
        Bucket=bucket)
    bucket_configurations.pop('ResponseMetadata')

    if 'LambdaFunctionConfigurations' in bucket_configurations:
        bucket_configurations['LambdaFunctionConfigurations'].append(
            json_data['LambdaFunctionConfigurations'][0]
        )
    else:
        bucket_configurations['LambdaFunctionConfigurations'] = json_data[
            'LambdaFunctionConfigurations']

    response = put_s3_lambda_notifications(bucket_configurations,
                                           bucket_notification)

    bucket_notification.reload()

    bucket_configurations = client_s3.get_bucket_notification_configuration(
        Bucket=bucket)
    return json2table(response)


# @make_spin(Default, 'Installing dependencies...')
def _install_dependencies_with_pip(requirements_file, destination_folder):
    """installs dependencies from a pip requirements_file to a local
    destination_folder

    :param requirements_file path to valid requirements_file
    :param destination_folder a foldername relative to the current working
    directory
    :return: exit_code
    """
    # TODO: not convinced that subprocess is the best way to call a python tool
    cmd = ['pip', 'install', '-r', requirements_file, '-t', destination_folder]

    try:
        result = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        # return result  # nobody used the results so far...
    except subprocess.CalledProcessError as e:
        sys.stderr.write(
            '\033[01;31mError running command: %s resulted in the ' % e.cmd +
            'following error: \033[01;32m %s' % e.output)
        return 1
    return 0


def list_functions(boto_session, out=sys.stdout):
    """List the deployed lambda functions and print configuration.

    :return: exit_code
    """
    client_lambda = boto_session.client('lambda')
    response = client_lambda.list_functions()
    for function in response['Functions']:
        print(function['FunctionName'], file=out)
        print('\t' 'Memory: ' + str(function['MemorySize']), file=out)
        print('\t' 'Timeout: ' + str(function['Timeout']), file=out)
        print('\t' 'Role: ' + str(function['Role']), file=out)
        print('\t' 'Current Version: ' + str(function['Version']), file=out)
        print('\t' 'Last Modified: ' + str(function['LastModified']), file=out)
        print('\t' 'CodeSha256: ' + str(function['CodeSha256']), file=out)

        print('\n', file=out)
    return 0


def deploy_lambda(boto_session, function_name, role, handler_filename,
                  handler_function,
                  folders, description, timeout, memory, subnet_ids=None,
                  security_groups=None, artifact_bucket=None,
                  fail_deployment_on_unsuccessful_ping=False,
                  slack_token=None, slack_channel='systemmessages'):
    """Create or update a lambda function.

    :param boto_session:
    :param function_name:
    :param role:
    :param handler_filename:
    :param handler_function:
    :param folders:
    :param description:
    :param timeout:
    :param memory:
    :param subnet_ids:
    :param security_groups:
    :param artifact_bucket:
    :param fail_deployment_on_unsuccessful_ping:
    :param slack_token:
    :param slack_channel:
    :return: exit_code
    """
    if lambda_exists(boto_session, function_name):
        function_version = _update_lambda(boto_session, function_name,
                                          handler_filename,
                                          handler_function, folders, role,
                                          description, timeout, memory,
                                          subnet_ids, security_groups,
                                          artifact_bucket=artifact_bucket,
                                          slack_token=slack_token,
                                          slack_channel=slack_channel)
    else:
        exit_code = _install_dependencies_with_pip('requirements.txt',
                                                   './vendored')
        if exit_code:
            return 1
        # create zipfile before calling into _create_lambda!
        zipfile = make_zip_file_bytes(handler=handler_filename,
                                      paths=folders)
        if check_buffer_exceeds_limit(zipfile):
            return 1
        # TODO: check this!
        log.info('buffer size: %0.2f MB' % float(len(zipfile) / 1000000.0))
        function_version = _create_lambda(boto_session, function_name, role,
                                          handler_filename, handler_function,
                                          folders, description, timeout,
                                          memory, subnet_ids, security_groups,
                                          artifact_bucket, zipfile,
                                          slack_token=slack_token,
                                          slack_channel=slack_channel)
    pong = ping(boto_session, function_name, version=function_version)
    if 'alive' in pong:
        print(colored.green('Great you\'re already accepting a ping ' +
                            'in your Lambda function'))
    elif fail_deployment_on_unsuccessful_ping and not 'alive' in pong:
        print(colored.green('Pinging your lambda function failed'))
        # we do not deploy alias and fail command
        return 1
    else:
        print(colored.red('Please consider adding a reaction to a ' +
                          'ping event to your lambda function'))
    _deploy_alias(boto_session, function_name, function_version)
    return 0


def _create_lambda(boto_session, function_name, role, handler_filename,
                   handler_function,
                   folders, description, timeout, memory,
                   subnet_ids=None, security_groups=None,
                   artifact_bucket=None, zipfile=None, slack_token=None,
                   slack_channel='systemmessages'):
    log.debug('create lambda function: %s' % function_name)
    # move to caller!
    # _install_dependencies_with_pip('requirements.txt', './vendored')
    client_lambda = boto_session.client('lambda')
    # print ('creating function %s with role %s handler %s folders %s timeout %s memory %s') % (
    # function_name, role, handler_filename, str(folders), str(timeout), str(memory))

    if not artifact_bucket:
        log.debug('create without artifact bucket...')
        response = client_lambda.create_function(
            FunctionName=function_name,
            Runtime='python2.7',
            Role=role,
            Handler=handler_function,
            Code={
                'ZipFile': zipfile
            },
            Description=description,
            Timeout=int(timeout),
            MemorySize=int(memory),
            Publish=True
        )
    elif artifact_bucket and zipfile:
        log.debug('create with artifact bucket...')
        # print 'uploading bundle to s3'
        dest_key, e_tag, version_id = \
            s3_upload(boto_session, artifact_bucket, zipfile, function_name)
        # print dest_key, e_tag, version_id
        response = client_lambda.create_function(
            FunctionName=function_name,
            Runtime='python2.7',
            Role=role,
            Handler=handler_function,
            Code={
                'S3Bucket': artifact_bucket,
                'S3Key': dest_key,
                'S3ObjectVersion': version_id
            },
            Description=description,
            Timeout=int(timeout),
            MemorySize=int(memory),
            Publish=True
        )
    else:
        log.debug('no zipfile and no artifact_bucket -> nothing to do!')
        # no zipfile and no artifact_bucket -> nothing to do!
        return

    function_version = response['Version']
    print(json2table(response))
    # FIXME: 23.08.2016 WHY update configuration after create?
    # timing issue:
    # http://jenkins.dev.dp.glomex.cloud/job/packages/job/gcdt_pull_request/32/console
    #       1) we need to wait till the function is available for update
    #          is there a better way than sleep?
    time.sleep(15)
    #       2) I believe this was implemented as shortcut to set subnet, and sg
    #          a way better way is to set this is using the using VPCConfig argument!
    _update_lambda_configuration(
        boto_session, function_name, role, handler_function, description,
        timeout, memory, subnet_ids, security_groups
    )
    message = 'ramuda bot: created new lambda function: %s ' % function_name
    monitoring.slack_notification(slack_channel, message, slack_token)
    return function_version


def _update_lambda(boto_session, function_name, handler_filename,
                   handler_function, folders,
                   role, description, timeout, memory, subnet_ids=None,
                   security_groups=None, artifact_bucket=None,
                   slack_token=None, slack_channel='systemmessages'):
    log.debug('update lambda function: %s' % function_name)
    _update_lambda_function_code(boto_session, function_name, handler_filename,
                                 folders, artifact_bucket=artifact_bucket)
    function_version = \
        _update_lambda_configuration(
            boto_session, function_name, role, handler_function,
            description, timeout, memory, subnet_ids, security_groups
        )
    message = 'ramuda bot: updated lambda function: %s ' % function_name
    monitoring.slack_notification(slack_channel, message, slack_token)
    return function_version


def bundle_lambda(handler_filename, folders):
    """Prepare a zip file for the lambda function and dependencies.

    :param handler_filename:
    :param folders:
    :return: exit_code
    """
    exit_code = _install_dependencies_with_pip('requirements.txt', './vendored')
    if exit_code:
        return 1
    zip_bytes = make_zip_file_bytes(
        handler=handler_filename, paths=folders)
    if check_buffer_exceeds_limit(zip_bytes):
        return 1
    with open('bundle.zip', 'wb') as zfile:
        zfile.write(zip_bytes)
    print('Finished - a bundle.zip is waiting for you...')
    return 0


def _update_lambda_function_code(boto_session, function_name, handler_filename,
                                 folders, artifact_bucket=None):
    client_lambda = boto_session.client('lambda')
    exit_code = _install_dependencies_with_pip('requirements.txt', './vendored')
    if exit_code:
        return 1
    # client_lambda = boto3.client('lambda')
    # we need the zipfile to create the local_hash!
    zipfile = make_zip_file_bytes(handler=handler_filename, paths=folders)
    if check_buffer_exceeds_limit(zipfile):
        return 1
    local_hash = create_sha256(zipfile)
    # print ('getting remote hash')

    # print local_hash
    remote_hash = get_remote_code_hash(boto_session, function_name)
    # print remote_hash
    if local_hash == remote_hash:
        print('Code hasn\'t changed - won\'t upload code bundle')
    else:
        if not artifact_bucket:
            log.info('no stack bucket found')
            response = client_lambda.update_function_code(
                FunctionName=function_name,
                ZipFile=zipfile,
                Publish=True
            )
            print(json2table(response))
        else:
            # print 'uploading bundle to s3'
            # reuse the zipfile we already created!
            dest_key, e_tag, version_id = \
                s3_upload(boto_session, artifact_bucket, zipfile, function_name)
            # print dest_key, e_tag, version_id
            response = client_lambda.update_function_code(
                FunctionName=function_name,
                S3Bucket=artifact_bucket,
                S3Key=dest_key,
                S3ObjectVersion=version_id,
                Publish=True
            )
            print(json2table(response))
    return 0


def _update_lambda_configuration(boto_session, function_name, role,
                                 handler_function,
                                 description, timeout, memory, subnet_ids=None,
                                 security_groups=None):
    client_lambda = boto_session.client('lambda')
    if subnet_ids and security_groups:
        # print ('found vpc config')
        response = client_lambda.update_function_configuration(
            FunctionName=function_name,
            Role=role,
            Handler=handler_function,
            Description=description,
            Timeout=timeout,
            MemorySize=memory,
            VpcConfig={
                'SubnetIds': subnet_ids,
                'SecurityGroupIds': security_groups
            }
        )
        print(json2table(response))
    else:
        response = client_lambda.update_function_configuration(
            FunctionName=function_name,
            Role=role,
            Handler=handler_function,
            Description=description,
            Timeout=timeout,
            MemorySize=memory)

        print(json2table(response))
    function_version = response['Version']
    return function_version


def get_metrics(boto_session, name, out=sys.stdout):
    """Print out cloudformation metrics for a lambda function.

    :param boto_session
    :param name: name of the lambda function
    :return: exit_code
    """
    metrics = ['Duration', 'Errors', 'Invocations', 'Throttles']
    client_cw = boto_session.client('cloudwatch')
    for metric in metrics:
        response = client_cw.get_metric_statistics(
            Namespace='AWS/Lambda',
            MetricName=metric,
            Dimensions=[
                {
                    'Name': 'FunctionName',
                    'Value': name
                },
            ],
            StartTime=datetime.now() + timedelta(days=-1),
            EndTime=datetime.now(),
            Period=3600,
            Statistics=[
                'Sum',
            ],
            Unit=unit(metric)
        )
        print('\t%s %s' % (metric,
                           repr(aggregate_datapoints(response['Datapoints']))),
              file=out)
    return 0


def rollback(boto_session, function_name, alias_name=ALIAS_NAME, version=None,
             slack_token=None, slack_channel='systemmessages'):
    """Rollback a lambda function to a given version.

    :param boto_session:
    :param function_name:
    :param alias_name:
    :param version:
    :param slack_token:
    :param slack_channel:
    :return: exit_code
    """
    if version:
        print('rolling back to version {}'.format(version))
        message = 'ramuda bot: rolled back lambda function: {} to version %s'.format(
            function_name, version)
    else:
        print('rolling back to previous version')
        version = _get_previous_version(boto_session, function_name, alias_name)
        if version == '0':
            print('unable to find previous version of lambda function')
            return 1

        print('new version is %s' % str(version))
        message = 'ramuda bot: rolled back lambda function: {} to previous version'.format(
            function_name)

    _update_alias(boto_session, function_name, version, alias_name)
    monitoring.slack_notification(slack_channel, message, slack_token)
    return 0


def delete_lambda(boto_session, function_name, s3_event_sources=[],
                  time_event_sources=[],
                  slack_token=None, slack_channel='systemmessages'):
    # FIXME: mutable default arguments!
    """Delete a lambda function.

    :param boto_session:
    :param function_name:
    :param s3_event_sources:
    :param time_event_sources:
    :param slack_token:
    :param slack_channel:
    :return: exit_code
    """
    unwire(boto_session, function_name, s3_event_sources=s3_event_sources,
           time_event_sources=time_event_sources,
           alias_name=ALIAS_NAME)
    client_lambda = boto_session.client('lambda')
    response = client_lambda.delete_function(FunctionName=function_name)

    # TODO remove event source first and maybe also needed for permissions
    print(json2table(response))
    message = 'ramuda bot: deleted lambda function: %s' % function_name
    monitoring.slack_notification(slack_channel, message, slack_token)
    return 0


def info(boto_session, function_name, s3_event_sources=None,
         time_event_sources=None, alias_name=ALIAS_NAME):
    if not lambda_exists(boto_session, function_name):
        print(colored.red('The function you try to display doesn\'t ' +
                          'exist... Bailing out...'))
        return 1

    client_lambda = boto_session.client('lambda')
    lambda_function = client_lambda.get_function(FunctionName=function_name)
    lambda_alias = client_lambda.get_alias(FunctionName=function_name,
                                           Name=alias_name)
    lambda_arn = lambda_alias['AliasArn']

    if lambda_function is not None:
        print(json2table(lambda_function['Configuration']).encode('utf-8'))
        print(json2table(lambda_alias).encode('utf-8'))
        print("\n### PERMISSIONS ###\n")

        try:
            result = client_lambda.get_policy(FunctionName=function_name,
                                              Qualifier=alias_name)

            policy = json.loads(result['Policy'])
            for statement in policy['Statement']:
                print('{} ({}) -> {}'.format(
                    statement['Condition']['ArnLike']['AWS:SourceArn'],
                    statement['Principal']['Service'],
                    statement['Resource']
                ))
        except ClientError as e:
            if e.response['Error']['Code'] == 'ResourceNotFoundException':
                print("No permissions found!")
            else:
                raise e

        print("\n### EVENT SOURCES ###\n")

        # S3 Events
        client_s3 = boto_session.resource('s3')
        client_s3_alt = boto_session.client('s3')
        for s3_event_source in s3_event_sources:
            bucket_name = s3_event_source.get('bucket')
            print('- \tS3: %s' % bucket_name)

            bucket_notification = client_s3.BucketNotification(bucket_name)
            bucket_notification.load()
            filter_rules = build_filter_rules(
                s3_event_source.get('prefix', None),
                s3_event_source.get('suffix', None))
            response = client_s3_alt.get_bucket_notification_configuration(
                Bucket=bucket_name)
            if 'LambdaFunctionConfigurations' in response:
                relevant_configs, irrelevant_configs = \
                    filter_bucket_notifications_with_arn(
                        response['LambdaFunctionConfigurations'],
                        lambda_arn, filter_rules
                    )
                if len(relevant_configs) > 0:
                    for config in relevant_configs:
                        print('\t\t{}:'.format(config['Events'][0]))
                        for rule in config['Filter']['Key']['FilterRules']:
                            print('\t\t{}: {}'.format(rule['Name'],
                                                      rule['Value']))
                else:
                    print('\tNot attached')

                    # TODO Beautify
                    # wrapper = TextWrapper(initial_indent='\t', subsequent_indent='\t')
                    # output = "\n".join(wrapper.wrap(json.dumps(config, indent=True)))
                    # print(json.dumps(config, indent=True))
            else:
                print('\tNot attached')

        # CloudWatch Event
        client_event = boto_session.client('events')
        for time_event in time_event_sources:
            rule_name = time_event.get('ruleName')
            print('- \tCloudWatch: %s' % rule_name)
            try:
                rule_response = client_event.describe_rule(Name=rule_name)
                target_list = client_event.list_targets_by_rule(
                    Rule=rule_name,
                )["Targets"]
                if target_list:
                    print("\t\tSchedule expression: {}".format(
                        rule_response['ScheduleExpression']))
                for target in target_list:
                    print(
                        '\t\tId: {} -> {}'.format(target['Id'], target['Arn']))
            except ClientError as e:
                if e.response['Error']['Code'] == 'ResourceNotFoundException':
                    print('\tNot attached!')
                else:
                    raise e


def filter_bucket_notifications_with_arn(lambda_function_configurations,
                                         lambda_arn, filter_rules=False):
    matching_notifications = []
    not_matching_notifications = []
    for notification in lambda_function_configurations:
        if notification["LambdaFunctionArn"] == lambda_arn:
            if filter_rules:
                existing_filter = notification['Filter']['Key']['FilterRules']
                if list_of_dict_equals(filter_rules, existing_filter):
                    matching_notifications.append(notification)
                else:
                    not_matching_notifications.append(notification)
            else:
                matching_notifications.append(notification)
        else:
            not_matching_notifications.append(notification)
    return matching_notifications, not_matching_notifications


def _ensure_s3_event(boto_session, s3_event_source, function_name, alias_name,
                     target_lambda_arn, ensure="exists"):
    if ensure not in ENSURE_OPTIONS:
        print("{} is invalid ensure option, should be {}".format(ensure,
                                                                 ENSURE_OPTIONS))

    client_s3 = boto_session.client('s3')

    bucket_name = s3_event_source.get('bucket')
    event_type = s3_event_source.get('type')
    prefix = s3_event_source.get('prefix', None)
    suffix = s3_event_source.get('suffix', None)

    rule_exists = False
    filter_rules = build_filter_rules(prefix, suffix)

    bucket_configurations = client_s3.get_bucket_notification_configuration(
        Bucket=bucket_name)
    bucket_configurations.pop('ResponseMetadata')
    matching_notifications, not_matching_notifications = filter_bucket_notifications_with_arn(
        bucket_configurations.get('LambdaFunctionConfigurations', []),
        target_lambda_arn, filter_rules)

    for config in matching_notifications:
        if config['Events'][0] == event_type:
            if filter_rules:
                if list_of_dict_equals(filter_rules,
                                       config['Filter']['Key']['FilterRules']):
                    rule_exists = True
            else:
                rule_exists = True
    # permissions_exists
    permission_exists = False
    if rule_exists:
        policies = _get_lambda_policies(boto_session, function_name, alias_name)
        if policies:
            for statement in policies['Statement']:
                if statement['Principal']['Service'] == 's3.amazonaws.com':
                    permission_bucket = get_bucket_from_s3_arn(
                        statement['Condition']['ArnLike']['AWS:SourceArn'])
                    if permission_bucket == bucket_name:
                        permission_exists = statement['Sid']
                        break

    if not rule_exists and not permission_exists:
        if ensure == "exists":
            print(colored.magenta(
                "\tWiring rule {}: {}".format(bucket_name, event_type)))
            for rule in filter_rules:
                print(colored.magenta(
                    '\t\t{}: {}'.format(rule['Name'], rule['Value'])))
            _wire_s3_to_lambda(boto_session, s3_event_source, function_name,
                               target_lambda_arn)
        elif ensure == "absent":
            return 0
    if rule_exists and permission_exists:
        if ensure == "absent":
            print(colored.magenta(
                "\tRemoving rule {}: {}".format(bucket_name, event_type)))
            for rule in filter_rules:
                print(colored.magenta(
                    '\t\t{}: {}'.format(rule['Name'], rule['Value'])))
            _remove_permission(function_name, permission_exists, alias_name,
                               boto_session.client('lambda'))
            _remove_events_from_s3_bucket(bucket_name, target_lambda_arn,
                                          filter_rules)


def _ensure_cloudwatch_event(boto_session, time_event, function_name,
                             alias_name, lambda_arn, ensure='exists'):
    if not ensure in ENSURE_OPTIONS:
        print("{} is invalid ensure option, should be {}".format(ensure,
                                                                 ENSURE_OPTIONS))
        sys.exit(1)
    rule_name = time_event.get('ruleName')
    rule_description = time_event.get('ruleDescription')
    schedule_expression = time_event.get('scheduleExpression')
    client_event = boto_session.client('events')

    rule_exists = False
    schedule_expression_match = False
    not_matching_schedule_expression = None
    try:
        rule_response = client_event.describe_rule(Name=rule_name)
        rule_exists = True
        if rule_response['ScheduleExpression'] == schedule_expression:
            schedule_expression_match = True
        else:
            not_matching_schedule_expression = rule_response[
                'ScheduleExpression']
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            pass
        else:
            raise e

    permission_exists = False
    if rule_exists:
        policies = _get_lambda_policies(boto_session, function_name, alias_name)
        if policies:
            for statement in policies['Statement']:
                if statement['Principal']['Service'] == 'events.amazonaws.com':
                    event_source_arn = get_rule_name_from_event_arn(
                        statement['Condition']['ArnLike']['AWS:SourceArn'])
                    if rule_name == event_source_arn:
                        permission_exists = statement['Sid']
                        break

    if not rule_exists and not permission_exists:
        if ensure == 'exists':
            print(colored.magenta(
                "\tWiring Cloudwatch event {}\n\t\t{}".format(rule_name,
                                                              schedule_expression)))
            rule_arn = _lambda_add_time_schedule_event_source(
                boto_session, rule_name, rule_description, schedule_expression,
                lambda_arn)
            _lambda_add_invoke_permission(
                boto_session, function_name, 'events.amazonaws.com', rule_arn)
        elif ensure == 'absent':
            return 0
    if rule_exists and permission_exists:
        if ensure == 'exists':
            if schedule_expression_match:
                return 0
            else:
                print(colored.magenta(
                    "\t Updating Cloudwatch event {}\n\t\tOld: {}\n\t\tTo: {}".format(
                        rule_name,
                        not_matching_schedule_expression,
                        schedule_expression)))
                rule_arn = _lambda_add_time_schedule_event_source(
                    boto_session, rule_name, rule_description,
                    schedule_expression, lambda_arn)
        if ensure == 'absent':
            print(colored.magenta("\tRemoving rule {}\n\t\t{}".format(rule_name,
                                                                      schedule_expression)))
            _remove_permission(boto_session, function_name, statement['Sid'],
                               alias_name)
            _remove_cloudwatch_rule_event(boto_session, rule_name, lambda_arn)


def _wire_s3_to_lambda(boto_session, s3_event_source, function_name,
                       target_lambda_arn):
    bucket_name = s3_event_source.get('bucket')
    event_type = s3_event_source.get('type')
    prefix = s3_event_source.get('prefix', None)
    suffix = s3_event_source.get('suffix', None)
    s3_arn = create_aws_s3_arn(bucket_name)

    _lambda_add_invoke_permission(boto_session, function_name,
                                  's3.amazonaws.com', s3_arn)
    _lambda_add_s3_event_source(boto_session, target_lambda_arn, event_type,
                                bucket_name, prefix, suffix)


def wire(boto_session, function_name, s3_event_sources=None,
         time_event_sources=None,
         alias_name=ALIAS_NAME, slack_token=None,
         slack_channel='systemmessages'):
    """Wiring a lambda function to events.

    :param boto_session:
    :param function_name:
    :param s3_event_sources:
    :param time_event_sources:
    :param alias_name:
    :param slack_token:
    :param slack_channel:
    :return: exit_code
    """
    if not lambda_exists(boto_session, function_name):
        print(colored.red('The function you try to wire up doesn\'t ' +
                          'exist... Bailing out...'))
        return 1
    client_lambda = boto_session.client('lambda')
    lambda_function = client_lambda.get_function(FunctionName=function_name)
    lambda_arn = client_lambda.get_alias(FunctionName=function_name,
                                         Name=alias_name)['AliasArn']
    print('wiring lambda_arn %s ...' % lambda_arn)

    if lambda_function is not None:
        s3_events_ensure_exists, s3_events_ensure_absent = filter_events_ensure(
            s3_event_sources)
        cloudwatch_events_ensure_exists, cloudwatch_events_ensure_absent = \
            filter_events_ensure(time_event_sources)

        for s3_event_source in s3_events_ensure_absent:
            _ensure_s3_event(boto_session, s3_event_source, function_name,
                             alias_name, lambda_arn, s3_event_source['ensure'])
        for s3_event_source in s3_events_ensure_exists:
            _ensure_s3_event(boto_session, s3_event_source, function_name,
                             alias_name, lambda_arn, s3_event_source['ensure'])

        for time_event in cloudwatch_events_ensure_absent:
            _ensure_cloudwatch_event(boto_session, time_event, function_name,
                                     alias_name, lambda_arn,
                                     time_event['ensure'])
        for time_event in cloudwatch_events_ensure_exists:
            _ensure_cloudwatch_event(boto_session, time_event, function_name,
                                     alias_name, lambda_arn,
                                     time_event['ensure'])
    message = ('ramuda bot: wiring lambda function: ' +
               '%s with alias %s' % (function_name, alias_name))
    monitoring.slack_notification(slack_channel, message, slack_token)
    return 0


def filter_events_ensure(event_sources):
    events_ensure_exists = []
    events_ensure_absent = []
    for event in event_sources:
        if 'ensure' in event:
            if event['ensure'] == 'exists':
                events_ensure_exists.append(event)
            elif event['ensure'] == 'absent':
                events_ensure_absent.append(event)
            else:
                print(colored.red(
                    'Ensure must be one of {}, currently set to {}'.format(
                        ENSURE_OPTIONS, event['ensure'])))
                # FIXME exit in lib code!
                # TODO: make sure it has a test!
                sys.exit(1)
        else:
            event['ensure'] = 'exists'
            events_ensure_exists.append(event)
    return events_ensure_exists, events_ensure_absent


def _get_lambda_policies(boto_session, function_name, alias_name):
    client_lambda = boto_session.client('lambda')
    policies = None
    try:
        result = client_lambda.get_policy(FunctionName=function_name,
                                          Qualifier=alias_name)
        policies = json.loads(result['Policy'])
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            print(colored.red("Permission policies not found"))
        else:
            raise e
    return policies


def unwire(boto_session, function_name, s3_event_sources=None,
           time_event_sources=None,
           alias_name=ALIAS_NAME, slack_token=None,
           slack_channel='systemmessages'):
    """Unwire an event from a lambda function.

    :param boto_session:
    :param function_name:
    :param s3_event_sources:
    :param time_event_sources:
    :param alias_name:
    :param slack_token:
    :param slack_channel:
    :return: exit_code
    """
    if not lambda_exists(boto_session, function_name):
        print(colored.red('The function you try to wire up doesn\'t ' +
                          'exist... Bailing out...'))
        return 1

    client_lambda = boto_session.client('lambda')
    lambda_function = client_lambda.get_function(FunctionName=function_name)
    lambda_arn = client_lambda.get_alias(FunctionName=function_name,
                                         Name=alias_name)['AliasArn']
    print('UN-wiring lambda_arn %s ' % lambda_arn)
    policies = None
    try:
        result = client_lambda.get_policy(FunctionName=function_name,
                                          Qualifier=alias_name)
        policies = json.loads(result['Policy'])
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            print("Permission policies not found")
        else:
            raise e

    if lambda_function is not None:
        #### S3 Events
        # for every permission - delete it and corresponding rule (if exists)
        if policies:
            for statement in policies['Statement']:
                if statement['Principal']['Service'] == 's3.amazonaws.com':
                    source_bucket = get_bucket_from_s3_arn(
                        statement['Condition']['ArnLike']['AWS:SourceArn'])
                    print('\tRemoving S3 permission {} invoking {}'.format(
                        source_bucket, lambda_arn))
                    _remove_permission(boto_session, function_name,
                                       statement['Sid'], alias_name)
                    print('\tRemoving All S3 events {} invoking {}'.format(
                        source_bucket, lambda_arn))
                    _remove_events_from_s3_bucket(boto_session, source_bucket,
                                                  lambda_arn)

        # Case: s3 events without permissions active "safety measure"
        for s3_event_source in s3_event_sources:
            bucket_name = s3_event_source.get('bucket')
            _remove_events_from_s3_bucket(boto_session, bucket_name, lambda_arn)

        #### CloudWatch Events
        # for every permission - delete it and corresponding rule (if exists)
        if policies:
            for statement in policies['Statement']:
                if statement['Principal']['Service'] == 'events.amazonaws.com':
                    rule_name = get_rule_name_from_event_arn(
                        statement['Condition']['ArnLike']['AWS:SourceArn'])
                    print(
                        '\tRemoving Cloudwatch permission {} invoking {}'.format(
                            rule_name, lambda_arn))
                    _remove_permission(boto_session, function_name,
                                       statement['Sid'], alias_name)
                    print('\tRemoving Cloudwatch rule {} invoking {}'.format(
                        rule_name, lambda_arn))
                    _remove_cloudwatch_rule_event(boto_session, rule_name,
                                                  lambda_arn)
        # Case: rules without permissions active, "safety measure"
        for time_event in time_event_sources:
            rule_name = time_event.get('ruleName')
            _remove_cloudwatch_rule_event(boto_session, rule_name, lambda_arn)

    message = ('ramuda bot: UN-wiring lambda function: %s ' % function_name +
               'with alias %s' % alias_name)
    monitoring.slack_notification(slack_channel, message, slack_token)
    return 0


def _remove_cloudwatch_rule_event(boto_session, rule_name, target_lambda_arn):
    client_event = boto_session.client('events')
    try:
        target_list = client_event.list_targets_by_rule(
            Rule=rule_name,
        )
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            return
        else:
            raise e
    target_id_list = []

    for target in target_list['Targets']:
        if target['Arn'] == target_lambda_arn:
            target_id_list += [target['Id']]
    # remove targets
    if target_id_list:
        client_event.remove_targets(
            Rule=rule_name,
            Ids=target_id_list,
        )
    # Delete rule only if all targets were associated with target_arn (i.e. only target target_arn function)
    if len(target_id_list) == len(target_list['Targets']) or (
                not target_id_list and not target_list):
        client_event.delete_rule(
            Name=rule_name
        )


def _remove_events_from_s3_bucket(boto_session, bucket_name, target_lambda_arn,
                                  filter_rule=False):
    resource_s3 = boto_session.resource('s3')
    client_s3 = boto_session.client('s3')
    bucket_notification = resource_s3.BucketNotification(bucket_name)
    bucket_configurations = client_s3.get_bucket_notification_configuration(
        Bucket=bucket_name)
    bucket_configurations.pop('ResponseMetadata')
    matching_notifications, not_matching_notifications = \
        filter_bucket_notifications_with_arn(
            bucket_configurations.get('LambdaFunctionConfigurations', []),
            target_lambda_arn, filter_rule
        )
    if not_matching_notifications:
        bucket_configurations[
            'LambdaFunctionConfigurations'] = not_matching_notifications
    else:
        if 'LambdaFunctionConfigurations' in bucket_configurations:
            bucket_configurations.pop('LambdaFunctionConfigurations')

    response = put_s3_lambda_notifications(bucket_configurations,
                                           bucket_notification)
    bucket_configurations_2 = client_s3.get_bucket_notification_configuration(
        Bucket=bucket_name)


def put_s3_lambda_notifications(configurations, bucket_notification):
    response = bucket_notification.put(
        NotificationConfiguration=configurations
    )
    bucket_notification.reload()
    return response


def _remove_permission(boto_session, function_name, statement_id, qualifier):
    client_lambda = boto_session.client('lambda')
    response_remove = client_lambda.remove_permission(
        FunctionName=function_name,
        StatementId=statement_id,
        Qualifier=qualifier
    )


def cleanup_bundle():
    """Deletes files used for creating bundle.
        * vendored/*
        * bundle.zip
    """
    paths = ['./vendored', './bundle.zip']
    for path in paths:
        if os.path.exists(path):
            log.debug("Deleting %s..." % path)
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)


def ping(boto_session, function_name, alias_name=ALIAS_NAME, version=None):
    """Send a ping request to a lambda function.

    :param boto_session:
    :param function_name:
    :param alias_name:
    :param version:
    :return: ping response payload
    """
    client_lambda = boto_session.client('lambda')
    payload = '{"ramuda_action": "ping"}'  # default to ping event

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

    print(response['Payload'])
    print(type(response['Payload']))
    results = response['Payload'].read()  # payload is a 'StreamingBody'
    print(results)
    # TODO: ping is a little boring without any output!
    return results
