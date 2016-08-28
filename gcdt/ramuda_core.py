# -*- coding: utf-8 -*-

"""ramuda.
Script to deploy Lambda functions to AWS
"""

from __future__ import print_function
import sys
import subprocess
import uuid
import time
from datetime import datetime, timedelta
import boto3
from botocore.exceptions import ClientError as ClientError
from clint.textui import colored
from gcdt import monitoring
from gcdt.ramuda_utils import make_zip_file_bytes, json2table, s3_upload, \
    lambda_exists, create_sha256, get_remote_code_hash, unit, \
    aggregate_datapoints, check_buffer_exceeds_limit
from gcdt.logger import setup_logger

log = setup_logger(logger_name='ramuda_core')
ALIAS_NAME = 'ACTIVE'


def _create_alias(function_name, function_version, alias_name=ALIAS_NAME):
    client = boto3.client('lambda')
    response = client.create_alias(
        FunctionName=function_name,
        Name=alias_name,
        FunctionVersion=function_version,

    )
    return response['AliasArn']


def _update_alias(function_name, function_version, alias_name=ALIAS_NAME):
    client = boto3.client('lambda')
    response = client.update_alias(
        FunctionName=function_name,
        Name=alias_name,
        FunctionVersion=function_version,

    )
    return response['AliasArn']


def _alias_exists(function_name, alias_name):
    client = boto3.client('lambda')
    try:
        client.get_alias(
            FunctionName=function_name,
            Name=alias_name
        )
        return True
    except Exception:
        return False


def _get_alias_version(function_name, alias_name):
    # this is used for testing - it returns the version
    client = boto3.client('lambda')
    try:
        response = client.get_alias(
            FunctionName=function_name,
            Name=alias_name
        )
        return response['FunctionVersion']
    except Exception:
        return


def _deploy_alias(function_name, function_version, alias_name=ALIAS_NAME):
    if _alias_exists(function_name, alias_name):
        _update_alias(function_name, function_version, alias_name)
    else:
        _create_alias(function_name, function_version, alias_name)


def _lambda_add_time_schedule_event_source(rule_name, rule_description,
                                           schedule_expression, lambda_arn):
    client = boto3.client('events')
    client.put_rule(
        Name=rule_name,
        ScheduleExpression=schedule_expression,
        Description=rule_description,
    )
    rule_response = client.describe_rule(Name=rule_name)
    if rule_response is not None:
        client.put_targets(
            Rule=rule_name,
            Targets=[
                {
                    'Id': '1',
                    'Arn': lambda_arn,
                },
            ]
        )

    return rule_response['Arn']


def _lambda_add_invoke_permission(function_name, source_principal, source_arn,
                                  alias_name=ALIAS_NAME):
    # https://www.getoto.net/noise/2015/08/20/better-together-amazon-ecs-and-aws-lambda/
    # http://docs.aws.amazon.com/cli/latest/reference/lambda/add-permission.html
    client = boto3.client('lambda')
    response = client.add_permission(
        FunctionName=function_name,
        StatementId=str(uuid.uuid1()),
        Action='lambda:InvokeFunction',
        Principal=source_principal,
        SourceArn=source_arn,
        Qualifier=alias_name
    )
    return response


def _lambda_add_s3_event_source(arn, event, bucket, prefix, suffix):
    """Use only prefix OR suffix

    :param arn:
    :param event:
    :param bucket:
    :param prefix:
    :param suffix:
    :return:
    """
    filter_rule = None
    json_data = {
        'LambdaFunctionConfigurations': [{
            'LambdaFunctionArn': arn,
            'Id': str(uuid.uuid1()),
            'Events': [event]
        }]
    }

    if prefix is not None and suffix is not None:
        raise Exception('Only select suffix or prefix')

    if prefix is not None:
        filter_rule = {
            'Name': 'prefix',
            'Value': prefix
        }

    if suffix is not None:
        filter_rule = {
            'Name': 'suffix',
            'Value': suffix
        }

    json_data['LambdaFunctionConfigurations'][0].update({
        'Filter': {
            'Key': {
                'FilterRules': [
                    filter_rule
                ]
            }
        }
    })

    # http://docs.aws.amazon.com/cli/latest/reference/s3api/put-bucket-notification-configuration.html
    # http://docs.aws.amazon.com/AmazonS3/latest/dev/NotificationHowTo.html
    s3 = boto3.resource('s3')
    bucket_notification = s3.BucketNotification(bucket)
    response = bucket_notification.put(
        NotificationConfiguration=json_data
    )
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


def list_functions(out=sys.stdout):
    """List the deployed lambda functions and print configuration.

    :return: exit_code
    """
    client = boto3.client('lambda')
    response = client.list_functions()
    for function in response['Functions']:
        print(function['FunctionName'], file=out)
        print('\t' 'Memory: ' + str(function['MemorySize']), file=out)
        print('\t' 'Timeout: ' + str(function['Timeout']), file=out)
        print('\t' 'Role: ' + str(function['Role']), file=out)
        print('\t' 'Current Version: ' + str(function['Version']), file=out)
        print('\t' 'Last Modified: ' + str(function['LastModified']), file=out)
        print('\t' 'CodeSha256: ' + str(function['CodeSha256']), file=out)

        print('\n', file=out)
        # print json.dumps(response, indent=4)
    return 0


def deploy_lambda(function_name, role, handler_filename, handler_function,
                  folders, description, timeout, memory, subnet_ids=None,
                  security_groups=None, artifact_bucket=None):
    """Create or update a lambda function.

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
    :return: exit_code
    """

    if lambda_exists(function_name):
        function_version = _update_lambda(function_name, handler_filename,
                                          handler_function, folders, role,
                                          description, timeout, memory,
                                          subnet_ids, security_groups,
                                          artifact_bucket=artifact_bucket)
        pong = ping(function_name, version=function_version)
        if 'alive' in pong:
            print(colored.green('Great you\'re already accepting a ping ' +
                                'in your Lambda function'))
        else:
            print(colored.red('Please consider adding a reaction to a ' +
                              'ping event to your lambda function'))
        _deploy_alias(function_name, function_version)
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
        function_version = _create_lambda(function_name, role,
                                          handler_filename, handler_function,
                                          folders, description, timeout,
                                          memory, subnet_ids, security_groups,
                                          artifact_bucket, zipfile)

        pong = ping(function_name, version=function_version)
        if 'alive' in pong:
            print(colored.green('Great you\'re already accepting a ping ' +
                                'in your Lambda function'))
        else:
            print(colored.red('Please consider adding a reaction to a ' +
                              'ping event to your lambda function'))
        _deploy_alias(function_name, function_version)
    return 0


def _create_lambda(function_name, role, handler_filename, handler_function,
                   folders, description, timeout, memory,
                   subnet_ids=None, security_groups=None,
                   artifact_bucket=None, zipfile=None, slack_token=None,
                   slack_channel='systemmessages'):
    log.debug('create lambda function: %s' % function_name)
    # move to caller!
    # _install_dependencies_with_pip('requirements.txt', './vendored')
    client = boto3.client('lambda')
    # print ('creating function %s with role %s handler %s folders %s timeout %s memory %s') % (
    # function_name, role, handler_filename, str(folders), str(timeout), str(memory))

    if not artifact_bucket:
        log.debug('create without artifact bucket...')
        response = client.create_function(
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
            s3_upload(artifact_bucket, zipfile, function_name)
        # print dest_key, e_tag, version_id
        response = client.create_function(
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
    _update_lambda_configuration(function_name, role, handler_function,
                                 description, timeout, memory, subnet_ids,
                                 security_groups)
    message = 'ramuda bot: created new lambda function: %s ' % function_name
    monitoring.slacker_notification(slack_channel, message, slack_token)
    return function_version


def _update_lambda(function_name, handler_filename, handler_function, folders,
                   role, description, timeout, memory, subnet_ids=None,
                   security_groups=None, artifact_bucket=None,
                   slack_token=None, slack_channel='systemmessages'):
    log.debug('update lambda function: %s' % function_name)
    _update_lambda_function_code(function_name, handler_filename, folders,
                                 artifact_bucket=artifact_bucket)
    function_version = \
        _update_lambda_configuration(function_name, role, handler_function,
                                     description, timeout, memory, subnet_ids,
                                     security_groups)
    message = 'ramuda bot: updated lambda function: %s ' % function_name
    monitoring.slacker_notification(slack_channel, message, slack_token)
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


def _update_lambda_function_code(function_name, handler_filename, folders,
                                 artifact_bucket=None):
    exit_code = _install_dependencies_with_pip('requirements.txt', './vendored')
    if exit_code:
        return 1
    client = boto3.client('lambda')
    # we need the zipfile to create the local_hash!
    zipfile = make_zip_file_bytes(handler=handler_filename, paths=folders)
    if check_buffer_exceeds_limit(zipfile):
        return 1
    local_hash = create_sha256(zipfile)
    # print ('getting remote hash')

    # print local_hash
    remote_hash = get_remote_code_hash(function_name)
    # print remote_hash
    if local_hash == remote_hash:
        print('Code hasn\'t changed - won\'t upload code bundle')
    else:
        if not artifact_bucket:
            log.info('no stack bucket found')
            response = client.update_function_code(
                FunctionName=function_name,
                ZipFile=zipfile,
                Publish=True
            )
            print(json2table(response))
        else:
            # print 'uploading bundle to s3'
            # reuse the zipfile we already created!
            dest_key, e_tag, version_id = \
                s3_upload(artifact_bucket, zipfile, function_name)
            # print dest_key, e_tag, version_id
            response = client.update_function_code(
                FunctionName=function_name,
                S3Bucket=artifact_bucket,
                S3Key=dest_key,
                S3ObjectVersion=version_id,
                Publish=True
            )
            print(json2table(response))
    return 0


def _update_lambda_configuration(function_name, role, handler_function,
                                 description, timeout, memory, subnet_ids=None,
                                 security_groups=None):
    client = boto3.client('lambda')
    if subnet_ids and security_groups:
        # print ('found vpc config')
        response = client.update_function_configuration(
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
        response = client.update_function_configuration(
            FunctionName=function_name,
            Role=role,
            Handler=handler_function,
            Description=description,
            Timeout=timeout,
            MemorySize=memory)

        print(json2table(response))
    function_version = response['Version']
    return function_version


def get_metrics(name, out=sys.stdout):
    """Print out cloudformation metrics for a lambda function.

    :param name: name of the lambda function
    :return: exit_code
    """
    metrics = ['Duration', 'Errors', 'Invocations', 'Throttles']
    client = boto3.client('cloudwatch')
    for metric in metrics:
        response = client.get_metric_statistics(
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


def rollback(function_name, alias_name=ALIAS_NAME, version=None,
             slack_token=None, slack_channel='systemmessages'):
    """Rollback a lambda function to a given version.

    :param function_name:
    :param alias_name:
    :param version:
    :param slack_token:
    :param slack_channel:
    :return: exit_code
    """
    if version:
        print('rolling back to version %s' % version)
        # for version in ramuda_utils.list_lambda_versions(function_name)['Versions']:
        #    print version['Version']
        _update_alias(function_name, version, alias_name)
        message = ('ramuda bot: rolled back lambda function: ' +
                   '%s to version %s' % (function_name, version))
        monitoring.slacker_notification(slack_channel, message, slack_token)
    else:
        print('rolling back to previous version')
        client = boto3.client('lambda')
        response = client.get_alias(
            FunctionName=function_name,
            Name=alias_name
        )

        current_version = response['FunctionVersion']
        print('current version is %s' % current_version)
        version = str(int(current_version) - 1)
        print('new version is %s' % str(version))
        _update_alias(function_name, version, alias_name)

        message = ('ramuda bot: rolled back lambda function: %s to ' +
                   'previous version') % function_name
        monitoring.slacker_notification(slack_channel, message, slack_token)
    return 0


def delete_lambda(function_name, slack_token=None, slack_channel='systemmessages'):
    """Delete a lambda function.

    :param function_name:
    :param slack_token:
    :param slack_channel:
    :return: exit_code
    """
    client = boto3.client('lambda')
    response = client.delete_function(FunctionName=function_name)
    print(json2table(response))
    message = 'ramuda bot: deleted lambda function: %s' % function_name
    monitoring.slacker_notification(slack_channel, message, slack_token)
    return 0


def wire(function_name, s3_event_sources=None, time_event_sources=None,
         alias_name=ALIAS_NAME, slack_token=None, slack_channel='systemmessages'):
    """Wiring a lambda function to events.

    :param function_name:
    :param s3_event_sources:
    :param time_event_sources:
    :param alias_name:
    :param slack_token:
    :param slack_channel:
    :return: exit_code
    """
    if not lambda_exists(function_name):
        print(colored.red('The function you try to wire up doesn\'t ' +
                          'exist... Bailing out...'))
        return 1
    lambda_client = boto3.client('lambda')
    lambda_function = lambda_client.get_function(FunctionName=function_name)
    lambda_arn = lambda_client.get_alias(FunctionName=function_name,
                                         Name=alias_name)['AliasArn']
    print('wiring lambda_arn %s ' % lambda_arn)
    if lambda_function is not None:
        for s3_event_source in s3_event_sources:
            bucket_name = s3_event_source.get('bucket')
            event_type = s3_event_source.get('type')
            prefix = s3_event_source.get('prefix', None)
            suffix = s3_event_source.get('suffix', None)
            s3_arn = 'arn:aws:s3:::' + bucket_name
            _lambda_add_invoke_permission(
                function_name, 's3.amazonaws.com', s3_arn)
            _lambda_add_s3_event_source(
                lambda_arn, event_type, bucket_name, prefix, suffix)
        for time_event in time_event_sources:
            rule_name = time_event.get('ruleName')
            rule_description = time_event.get('ruleDescription')
            schedule_expression = time_event.get('scheduleExpression')
            rule_arn = _lambda_add_time_schedule_event_source(
                rule_name, rule_description, schedule_expression, lambda_arn)
            _lambda_add_invoke_permission(
                function_name, 'events.amazonaws.com', rule_arn)
    message = ('ramuda bot: wiring lambda function: ' +
               '%s with alias %s' % (function_name, alias_name))
    monitoring.slacker_notification(slack_channel, message, slack_token)
    return 0


def unwire(function_name, s3_event_sources=None, time_event_sources=None,
           alias_name=ALIAS_NAME, slack_token=None, slack_channel='systemmessages'):
    """Unwire an event from a lambda function.

    :param function_name:
    :param s3_event_sources:
    :param time_event_sources:
    :param alias_name:
    :param slack_token:
    :param slack_channel:
    :return: exit_code
    """
    if not lambda_exists(function_name):
        print(colored.red('The function you try to wire up doesn\'t ' +
                          'exist... Bailing out...'))
        return 1

    client_lambda = boto3.client('lambda')
    lambda_function = client_lambda.get_function(FunctionName=function_name)
    lambda_arn = client_lambda.get_alias(FunctionName=function_name,
                                         Name=alias_name)['AliasArn']
    print('UN-wiring lambda_arn %s ' % lambda_arn)

    if lambda_function is not None:
        # S3 Events
        client_s3 = boto3.resource('s3')
        for s3_event_source in s3_event_sources:
            bucket_name = s3_event_source.get('bucket')
            print('\tS3: %s' % bucket_name)

            bucket_notification = client_s3.BucketNotification(bucket_name)
            response = bucket_notification.put(
                NotificationConfiguration={})

            print(json2table(response).encode('utf-8'))

        # CloudWatch Event
        client_event = boto3.client('events')
        for time_event in time_event_sources:
            rule_name = time_event.get('ruleName')
            print('\tCloudWatch: %s' % rule_name)

            # Delete rule target
            try:
                target_list = client_event.list_targets_by_rule(
                    Rule=rule_name,
                )
            except ClientError as e:
                if e.response['Error']['Code'] == 'ResourceNotFoundException':
                    continue
                else:
                    raise e

            target_id_list = []
            for target in target_list['Targets']:
                target_id_list += [target['Id']]

            client_event.remove_targets(
                Rule=rule_name,
                Ids=target_id_list,
            )

            # Delete rule
            client_event.delete_rule(
                Name=rule_name
            )

    message = ('ramuda bot: UN-wiring lambda function: %s ' % function_name +
               'with alias %s' % alias_name)
    monitoring.slacker_notification(slack_channel, message, slack_token)
    return 0


def ping(function_name, alias_name=ALIAS_NAME, version=None):
    """Send a ping request to a lambda function.

    :param function_name:
    :param alias_name:
    :param version:
    :return: ping response payload
    """
    client = boto3.client('lambda')
    payload = '{"ramuda_action": "ping"}'  # default to ping event

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
    # TODO: ping is a little boring without any output!
    return results
