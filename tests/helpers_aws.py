# -*- coding: utf-8 -*-
from __future__ import unicode_literals, print_function
import os
import time
import logging
import textwrap

from gcdt_ramuda.ramuda_core import deploy_lambda


log = logging.getLogger(__name__)


def settings_requirements():
    settings_file = os.path.join('settings_dev.conf')
    with open(settings_file, 'w') as settings:
        setting_string = textwrap.dedent("""\
            sample_lambda {
                cw_name = "dp-dev-sample"
            }""")
        settings.write(setting_string)
    requirements_txt = os.path.join('requirements.txt')
    with open(requirements_txt, 'w') as req:
        req.write('pyhocon==0.3.28\n')
    # ./vendored folder
    if not os.path.exists('./vendored'):
        # reuse ./vendored folder to save us some time during pip install...
        os.makedirs('./vendored')


def create_lambda_helper(awsclient, lambda_name, role_arn, handler_filename,
                         lambda_handler='handler.handle',
                         folders_from_file=None,
                         **kwargs):
    """
    NOTE: caller needs to clean up both lambda!

    :param awsclient:
    :param lambda_name:
    :param role_arn:
    :param handler_filename:
    :param lambda_handler:
    :param folders_from_file:
    :param kwargs: additional kwargs are used in deploy_lambda
    :return:
    """
    from gcdt_bundler.bundler import get_zipped_file
    settings_requirements()

    lambda_description = 'lambda created for unittesting ramuda deployment'
    timeout = 300
    memory_size = 128
    if not folders_from_file:
        folders_from_file = [
            {'source': './vendored', 'target': '.'},
            {'source': './resources/sample_lambda/impl', 'target': 'impl'}
        ]
    artifact_bucket = None

    zipfile = get_zipped_file(
        handler_filename,
        folders_from_file,
    )

    # create the AWS Lambda function
    deploy_lambda(
        awsclient=awsclient,
        function_name=lambda_name,
        role=role_arn,
        handler_filename=handler_filename,
        handler_function=lambda_handler,
        folders=folders_from_file,
        description=lambda_description,
        timeout=timeout,
        memory=memory_size,
        artifact_bucket=artifact_bucket,
        zipfile=zipfile,
        **kwargs
    )

    # TODO better use waiter for that!
    time.sleep(10)
