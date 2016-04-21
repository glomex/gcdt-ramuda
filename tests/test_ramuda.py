import sys

sys.path.append("../gcdt/")

from unittest import TestCase, main
import unittest
import io
import os
from zipfile import ZipFile
from ramuda_tool import install_dependencies_with_pip, create_lambda, update_lambda_function_code, get_metrics,delete_lambda,deploy_alias,update_lambda,rollback
from ramuda_utils import are_credentials_still_valid, lambda_exists
import boto3
from pyspin.spin import make_spin, Default
import shutil
import random
import string
from pyhocon import ConfigFactory


# TODO
# setup random function name
# tear down function
# testspinner
# rename to test_butaris


class MonitoringTestCase(TestCase):
    temp_string = ''.join([random.choice(string.ascii_lowercase)
                           for n in xrange(6)])
    temporary_function_name = "jenkins_test_" + temp_string

    def test_make_zip_file_bytes(self):
        pass

    def setUp(self):
        shutil.rmtree(os.getcwdu() + "/resources/vendored", ignore_errors=True)
        os.mkdir(os.getcwdu() + "/resources/vendored")
        print self.temporary_function_name
        with open("settings_dev.conf", "w") as settings:
            setting_string = """sample_lambda {
                            cw_name = "dp-dev-sample"
                            }"""
            settings.write(setting_string)
        os.mkdir(os.getcwdu() + "/vendored")

    def tearDown(self):
        # shutil.rmtree(os.getcwdu()+"/resources/vendored")
        shutil.rmtree(os.getcwdu()+"/vendored")
        os.remove("settings_dev.conf")



    def test_install_dependencies_with_pip(self):
        print install_dependencies_with_pip(os.getcwdu() + "/resources/requirements.txt",
                                            os.getcwdu() + "/resources/vendored")
        packages = os.listdir(os.getcwdu() + "/resources/vendored")
        for package in packages:
            print package
        self.assertTrue("werkzeug" in packages)
        self.assertTrue("simplejson" in packages)
        self.assertTrue("troposphere" in packages)
        self.assertTrue("boto3" in packages)

    def test_lambda_exists(self):
        non_existing_function = "Troubadix"
        existing_function = "dp-dev-lambdaEC2Basics-VXBP73NNZFKS"
        self.assertTrue(lambda_exists(existing_function))
        self.assertFalse(lambda_exists(non_existing_function))

    def test_get_metrics(self):
        get_metrics("dp-dev-lambdaEC2Basics-VXBP73NNZFKS")

    def test_create_lambda(self):
        config_string = """lambda {
    name = "dp-dev-sample-lambda-jobr"
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
}
"""
        conf = ConfigFactory.parse_string(config_string)
        lambda_name = conf.get("lambda.name")
        lambda_description = conf.get("lambda.description")
        role_arn = conf.get("lambda.role")
        lambda_handler = conf.get("lambda.handlerFunction")
        handler_filename = conf.get("lambda.handlerFile")
        timeout = int(conf.get_string("lambda.timeout"))
        memory_size = int(conf.get_string("lambda.memorySize"))
        # s3_event_sources = conf.get("lambda.events.s3Sources")
        zip_name = conf.get("bundling.zip")
        folders_from_file = conf.get("bundling.folders")
        # folders = [source["source"] for source in folders_from_file]
        subnet_ids = conf.get("lambda.vpc.subnetIds", None)
        security_groups = conf.get("lambda.vpc.securityGroups", None)
        region = conf.get("deployment.region")

        update_lambda(function_name=lambda_name,
                      role=role_arn,
                      handler_filename=handler_filename,
                      handler_function=lambda_handler,
                      folders=folders_from_file,
                      description=lambda_description,
                      timeout=timeout,
                      memory=memory_size)
        deploy_alias(lambda_name,"40")
        rollback(lambda_name, "ACTIVE", "43")
        rollback(lambda_name, "ACTIVE")

        #delete_lambda("dp-dev-sample-lambda-jobr")

    """
    def test_update_lambda(self):
        update_lambda_function_code()
    """

if __name__ == "__main__":
    # unittest.main()
    main()
