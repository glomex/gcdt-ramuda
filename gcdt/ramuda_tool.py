#!/usr/bin/env python
#from __future__ import print_function

"""ramuda.
Script to deploy Lambda functions to AWS
"""

import os
import subprocess
import uuid
import boto3
import monitoring
from config_reader import read_lambda_config
from docopt import docopt
import ramuda_utils
from ramuda_utils import read_ramuda_config
from datetime import datetime, timedelta
from clint.textui import colored
import sys
from cookiecutter.main import cookiecutter
import utils

# TODO

# stdin via clint
# introduce own config for environment/account detection
# reupload on requirements.txt changes
# filter requirements
# manage log groups
# silence slacker
# fill description with git commit, jenkins build or local info
# wire to specific alias
# provide -e to deploy
# wire only local folder
# retain only n versions
# fix environment handling

# creating docopt parameters and usage help
doc = """Usage:
        ramuda bundle [--env=<env>]
        ramuda deploy [--env=<env>]
        ramuda list
        ramuda metrics <lambda>
        ramuda wire [--env=<env>]
        ramuda unwire [--env=<env>]
        ramuda delete -f <lambda>
        ramuda rollback <lambda> [<version>]
        ramuda ping <lambda> [<version>]
        ramuda configure
        ramuda scaffold [<lambdaname>]
        ramuda version



-h --help           show this

"""

current_path = os.getcwdu()
ALIAS_NAME="ACTIVE"
RAMUDA_CONFIG = read_ramuda_config()
SLACK_TOKEN = RAMUDA_CONFIG.get("ramuda.slack-token")



def config_from_file(env):
    os.environ['ENV'] = env

    # read config from given name
    return read_lambda_config()


def create_alias(function_name, function_version, alias_name=ALIAS_NAME):
    client = boto3.client("lambda")
    response = client.create_alias(
        FunctionName=function_name,
        Name=alias_name,
        FunctionVersion=function_version,

    )
    return response["AliasArn"]


def update_alias(function_name, function_version, alias_name=ALIAS_NAME):
    client = boto3.client("lambda")
    response = client.update_alias(
        FunctionName=function_name,
        Name=alias_name,
        FunctionVersion=function_version,

    )
    return response["AliasArn"]


def alias_exists(function_name, alias_name):
    client = boto3.client("lambda")
    try:
        response = client.get_alias(
            FunctionName=function_name,
            Name=alias_name
        )
        return True
    except Exception as e:
        return False


def deploy_alias(function_name, function_version, alias_name=ALIAS_NAME):
    if alias_exists(function_name, alias_name):
        update_alias(function_name, function_version, alias_name)
    else:
        create_alias(function_name, function_version, alias_name)


def lambda_add_time_schedule_event_source(rule_name, rule_description, schedule_expression, lambda_arn):
    client = boto3.client("events")
    response = client.put_rule(
        Name=rule_name,
        ScheduleExpression=schedule_expression,
        Description=rule_description,
    )
    rule_response = client.describe_rule(Name=rule_name)
    if rule_response is not None:
        response = client.put_targets(
            Rule=rule_name,
            Targets=[
                {
                    'Id': '1',
                    'Arn': lambda_arn,
                },
            ]
        )
    print (lambda_arn)
    return rule_response["Arn"]


def lambda_add_invoke_permission(function_name, source_principal, source_arn, alias_name=ALIAS_NAME):
    # https://www.getoto.net/noise/2015/08/20/better-together-amazon-ecs-and-aws-lambda/
    # http://docs.aws.amazon.com/cli/latest/reference/lambda/add-permission.html
    client = boto3.client("lambda")
    response = client.add_permission(
        FunctionName=function_name,
        StatementId=str(uuid.uuid1()),
        Action='lambda:InvokeFunction',
        Principal=source_principal,
        SourceArn=source_arn,
        Qualifier=alias_name
    )
    return response


def lambda_add_s3_event_source(arn, event, bucket, prefix, suffix):
    """
    Use only prefix OR suffix
    :param arn:
    :param event:
    :param bucket:
    :param prefix:
    :param suffix:
    """

    filter_rule = None
    json_data = {
        "LambdaFunctionConfigurations": [{
            "LambdaFunctionArn": arn,
            'Id': str(uuid.uuid1()),
            'Events': [event]
        }]
    }
    print json_data
    if prefix is not None and suffix is not None:
        raise Exception("Only select suffix or prefix")

    if prefix is not None:
        filter_rule = {
            "Name": "prefix",
            "Value": prefix
        }

    if suffix is not None:
        filter_rule = {
            "Name": "suffix",
            "Value": suffix
        }

    json_data["LambdaFunctionConfigurations"][0].update({
        "Filter": {
            "Key": {
                "FilterRules": [
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
        NotificationConfiguration=json_data)
    return ramuda_utils.json2table(response)


##################################new stuff###############################


def install_dependencies_with_pip(requirements_file, destination_folder):
    '''
    installs dependencies from a pip requirements_file to a local destination_folder
    :param requirements_file path to valid requirements_file
    :param destination_folder a foldername relative to the current working directory
    '''
    cmd = ["pip", "install", "-r", requirements_file, "-t", destination_folder]

    try:
        result = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as e:
        sys.stderr.write("\033[01;31mError running command: {} resulted in the following error: \033[01;32m {}".format(e.cmd, e.output))

    print result
    return result


def list_functions():
    client = boto3.client('lambda')
    response = client.list_functions()
    print type(response)
    for function in response["Functions"]:
        print function["FunctionName"]
        print "\t" "Memory: " + str(function["MemorySize"])
        print "\t" "Timeout: " + str(function["Timeout"])
        print "\t" "Role: " + str(function["Role"])
        print "\t" "Current Version: " + str(function["Version"])
        print "\t" "Last Modified: " + str(function["LastModified"])
        print "\t" "CodeSha256: " + str(function["CodeSha256"])

        print "\n"
        # print json.dumps(response, indent=4)


def deploy_lambda(function_name, role, handler_filename, handler_function, folders, description, timeout, memory,
                  subnet_ids=None, security_groups=None):
    if ramuda_utils.lambda_exists(function_name):
        function_version = update_lambda(function_name, handler_filename, handler_function, folders, role,
                      description, timeout, memory, subnet_ids, security_groups)
        pong = ping(function_name, version=function_version)
        if "alive" in pong:
            print (colored.green("Great your'e already accepting a ping in your Lambda function"))
            print pong
        else:
            print (colored.red("Please consider adding a reaction to a ping event to your lambda function"))
            print pong
        deploy_alias(function_name, function_version)

    else:
        function_version=create_lambda(function_name, role, handler_filename, handler_function,
                      folders, description, timeout, memory, subnet_ids, security_groups)
        pong = ping(function_name, version=function_version)
        if "alive" in pong:
            print (colored.green("Great your'e already accepting a ping in your Lambda function"))
            print pong
        else:
            print (colored.red("Please consider adding a reaction to a ping event to your lambda function"))
            print pong
        deploy_alias(function_name, function_version)


def create_lambda(function_name, role, handler_filename, handler_function, folders, description, timeout, memory,
                  subnet_ids=None, security_groups=None):
    install_dependencies_with_pip("requirements.txt", "./vendored")
    client = boto3.client('lambda')
    print ("creating function %s with role %s handler %s folders %s timeout %s memory %s") % (
        function_name, role, handler_filename, str(folders), str(timeout), str(memory))
    response = client.create_function(
        FunctionName=function_name,
        Runtime='python2.7',
        Role=role,
        Handler=handler_function,
        Code={
            'ZipFile': ramuda_utils.make_zip_file_bytes(handler=handler_filename, paths=folders)
        },
        Description=description,
        Timeout=int(timeout),
        MemorySize=int(memory),
        Publish=True,

    )
    function_version = response["Version"]
    print ramuda_utils.json2table(response)
    update_lambda_configuration(function_name, role, handler_function,
                                description, timeout, memory, subnet_ids, security_groups)
    message = "ramuda bot: created new lambda function: %s " % (
        function_name)
    monitoring.slacker_notifcation("systemmessages", message, SLACK_TOKEN)
    return function_version

def bundle_lambda(handler_filename, folders):
    install_dependencies_with_pip("requirements.txt", "./vendored")
    zip = ramuda_utils.make_zip_file_bytes(
        handler=handler_filename, paths=folders)
    f = open('bundle.zip', 'wb')
    f.write(zip)
    f.close()
    print("Finished - a bundle.zip is waiting for you...")


def update_lambda(function_name, handler_filename, handler_function, folders, role, description, timeout, memory,
                  subnet_ids=None, security_groups=None):
    update_lambda_function_code(function_name, handler_filename, folders)
    function_version = update_lambda_configuration(function_name, role, handler_function,
                                description, timeout, memory, subnet_ids, security_groups)
    message = ("ramuda bot: updated lambda function: %s ") % (function_name)
    monitoring.slacker_notifcation("systemmessages", message, SLACK_TOKEN)
    return function_version

def update_lambda_function_code(function_name, handler_filename, folders):
    install_dependencies_with_pip("requirements.txt", "./vendored")
    client = boto3.client('lambda')
    zipfile = ramuda_utils.make_zip_file_bytes(
        handler=handler_filename, paths=folders)
    local_hash = ramuda_utils.create_sha256(zipfile)
    remote_hash = ramuda_utils.get_remote_code_hash(function_name)
    if local_hash == remote_hash:
        print ("Code hasn't changed - won't upload code bundle")
    else:
        response = client.update_function_code(
            FunctionName=function_name,
            ZipFile=zipfile,
            Publish=True
        )
        print ramuda_utils.json2table(response)


def update_lambda_configuration(function_name, role, handler_function, description, timeout, memory, subnet_ids=None,
                                security_groups=None):
    client = boto3.client('lambda')
    if subnet_ids and security_groups:
        # print ("found vpc config")
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
        print ramuda_utils.json2table(response)
    else:
        response = client.update_function_configuration(
            FunctionName=function_name,
            Role=role,
            Handler=handler_function,
            Description=description,
            Timeout=timeout,
            MemorySize=memory)

        print ramuda_utils.json2table(response)
    function_version = response["Version"]
    return function_version


def get_metrics(name):
    metrics = ["Duration", "Errors", "Invocations", "Throttles"]
    client = boto3.client("cloudwatch")
    print name
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
            Unit=ramuda_utils.unit(metric)
        )
        print "\t" + metric + " " + repr(ramuda_utils.aggregate_datapoints(response["Datapoints"]))


def rollback(function_name, alias_name=ALIAS_NAME, version=None):
    if version:
        print("rolling back to version %s") % (version)
        # for version in ramuda_utils.list_lambda_versions(function_name)["Versions"]:
        #    print version["Version"]
        update_alias(function_name, version, alias_name)
        message = ("ramuda bot: rolled back lambda function: %s to version %s") % (
            function_name, version)
        monitoring.slacker_notifcation("systemmessages", message, SLACK_TOKEN)

    else:
        print("rolling back to previous version")
        client = boto3.client("lambda")
        response = client.get_alias(
            FunctionName=function_name,
            Name=alias_name
        )

        current_version = response["FunctionVersion"]
        print "current version is %s" % current_version
        version = str(int(current_version) - 1)
        print "new version is %s" % str(version)
        update_alias(function_name, version, alias_name)

        message = ("ramuda bot: rolled back lambda function: %s to previous version") % (
            function_name)
        monitoring.slacker_notifcation("systemmessages", message, SLACK_TOKEN)


def delete_lambda(function_name):
    client = boto3.client("lambda")
    response = client.delete_function(FunctionName=function_name)
    print ramuda_utils.json2table(response)
    message = ("ramuda bot: deleted lambda function: %s") % (function_name)
    monitoring.slacker_notifcation("systemmessages", message, SLACK_TOKEN)


def wire(function_name, s3_event_sources=None, time_event_sources=None, alias_name=ALIAS_NAME):
    if not ramuda_utils.lambda_exists(function_name):
        print colored.red("The function you try to wire up doesn't exist... Bailing out...")
        sys.exit(1)
    lambdaClient = boto3.client('lambda')
    lambda_function = lambdaClient.get_function(FunctionName=function_name)
    lambda_arn = lambdaClient.get_alias(FunctionName=function_name, Name=alias_name)["AliasArn"]
    print "wiring lambda_arn %s " % lambda_arn
    if lambda_function is not None:
        for s3_event_source in s3_event_sources:
            bucket_name = s3_event_source.get("bucket")
            event_type = s3_event_source.get("type")
            prefix = s3_event_source.get("prefix", None)
            suffix = s3_event_source.get("suffix", None)
            s3_arn = "arn:aws:s3:::" + bucket_name
            lambda_add_invoke_permission(
                function_name, "s3.amazonaws.com", s3_arn)
            lambda_add_s3_event_source(
                lambda_arn, event_type, bucket_name, prefix, suffix)
        for time_event in time_event_sources:
            rule_name = time_event.get("ruleName")
            rule_description = time_event.get("ruleDescription")
            schedule_expression = time_event.get("scheduleExpression")
            rule_arn = lambda_add_time_schedule_event_source(
                rule_name, rule_description, schedule_expression, lambda_arn)
            lambda_add_invoke_permission(
                function_name, 'events.amazonaws.com', rule_arn)
    message = ("ramuda bot: wiring lambda function: %s with alias %s") % (function_name,alias_name)
    monitoring.slacker_notifcation("systemmessages", message, SLACK_TOKEN)


def unwire(function_name, s3_event_sources, alias_name=ALIAS_NAME):
    if not ramuda_utils.lambda_exists(function_name):
        print colored.red("The function you try to wire up doesn't exist... Bailing out...")
        sys.exit(1)
    s3 = boto3.resource('s3')
    for s3_event_source in s3_event_sources:
        bucket_name = s3_event_source.get("bucket")
        bucket_notification = s3.BucketNotification(bucket_name)
        response = bucket_notification.put(
            NotificationConfiguration={})
        print ramuda_utils.json2table(response)
    message = ("ramuda bot: unwiring lambda function: %s with alias %s") % (function_name, alias_name)
    monitoring.slacker_notifcation("systemmessages", message, SLACK_TOKEN)


def ping(function_name, alias_name=ALIAS_NAME, version=None):
    client = boto3.client('lambda')
    payload = '{"ramuda_action" : "ping"}'  # default to ping event
    response = None
    results = None

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
    print results
    return results

def scaffold():
    # Create project from the cookiecutter-pypackage/ template
    template_path = os.path.join(
        os.path.dirname(__file__), 'cookiecutter-ramuda')
    cookiecutter(template_path)



def main():

    arguments = docopt(doc)
    # print arguments
    if arguments["list"]:
        list_functions()
    elif arguments["metrics"]:
        get_metrics(arguments["<lambda>"])
    elif arguments["deploy"]:
        env = (arguments["--env"] if arguments["--env"] else "DEV")
        conf = config_from_file(env)
        lambda_name = conf.get("lambda.name")
        lambda_description = conf.get("lambda.description")
        role_arn = conf.get("lambda.role")
        lambda_handler = conf.get("lambda.handlerFunction")
        handler_filename = conf.get("lambda.handlerFile")
        timeout = int(conf.get_string("lambda.timeout"))
        memory_size = int(conf.get_string("lambda.memorySize"))
        folders_from_file = conf.get("bundling.folders")
        subnet_ids = conf.get("lambda.vpc.subnetIds", None)
        security_groups = conf.get("lambda.vpc.securityGroups", None)
        deploy_lambda(lambda_name, role_arn, handler_filename, lambda_handler,
                      folders_from_file, lambda_description, timeout, memory_size, subnet_ids=subnet_ids,
                      security_groups=security_groups)
    elif arguments["delete"]:
        delete_lambda(arguments["<lambda>"])
    elif arguments["wire"]:
        env = (arguments["--env"] if arguments["--env"] else "DEV")
        conf = config_from_file(env)
        function_name= conf.get("lambda.name")
        s3_event_sources = conf.get("lambda.events.s3Sources", [])
        time_event_sources = conf.get("lambda.events.timeSchedules", [])
        wire(function_name, s3_event_sources, time_event_sources)
    elif arguments["unwire"]:
        env = (arguments["--env"] if arguments["--env"] else "DEV")
        conf = config_from_file(env)
        s3_event_sources = conf.get("lambda.events.s3Sources")
        function_name= conf.get("lambda.name")
        unwire(function_name, s3_event_sources)
    elif arguments["bundle"]:
        env = (arguments["--env"] if arguments["--env"] else "DEV")
        conf = config_from_file(env)
        handler_filename = conf.get("lambda.handlerFile")
        folders_from_file = conf.get("bundling.folders")
        bundle_lambda(handler_filename, folders_from_file)
    elif arguments["rollback"]:
        #env = (arguments["--env"] if arguments["--env"] else "DEV")
        #conf = config_from_file(env)
        if arguments["<version>"]:
            rollback(arguments["<lambda>"], ALIAS_NAME, arguments["<version>"])
        else:
            rollback(arguments["<lambda>"], ALIAS_NAME)
    elif arguments["ping"]:
        ramuda_utils.are_credentials_still_valid()
        if arguments["<version>"]:
            ping(arguments["<lambda>"], version=arguments["<version>"])
        else:
            ping(arguments["<lambda>"])
    elif arguments["scaffold"]:
        scaffold()
    elif arguments["version"]:
        utils.version()



if __name__ == "__main__":
    ramuda_utils.are_credentials_still_valid()
    main()
