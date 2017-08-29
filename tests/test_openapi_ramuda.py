# -*- coding: utf-8 -*-
from __future__ import unicode_literals, print_function

from gcdt.gcdt_openapi import get_openapi_defaults, get_openapi_scaffold_min, \
    get_openapi_scaffold_max, validate_tool_config

from gcdt_ramuda import read_openapi


def test_scaffolding_default():
    spec = read_openapi()
    expected_defaults = {
        'defaults': {
            'validate': True,
            'settings_file': 'settings.json',
            'keep': False,
            'non_config_commands': ['logs', 'invoke'],
            'python_bundle_venv_dir': '.gcdt/venv',
            'runtime': 'python2.7'
        }
    }

    ramuda_defaults = get_openapi_defaults(spec, 'ramuda')
    assert ramuda_defaults == expected_defaults
    validate_tool_config(spec, {'ramuda': ramuda_defaults})


def test_scaffolding_sample_min():
    spec = read_openapi()
    expected_sample = {
        'defaults': {
            'validate': True,
            'settings_file': 'settings.json',
            'keep': False,
            'non_config_commands': ['logs', 'invoke'],
            'python_bundle_venv_dir': '.gcdt/venv',
            'runtime': 'python2.7'
        },

        'lambda': {
            'description': 'Lambda function for my-service',
            #'events': [OrderedDict([('event_source', OrderedDict([('arn', 'arn:aws:s3:::my-bucket'), ('events', ['s3:ObjectCreated:*'])]))])],
            'handlerFile': 'lambda.js',
            'handlerFunction': 'lambda.handler',
            'name': 'my-service',
            'role': 'arn:aws:iam::123456EXAMPLE:role/my-service-role-123EXAMPLE',
            'vpc': {
                'securityGroups': ['sg-aabb123c'],
                'subnetIds': ['subnet-aabb123c']
            },
        }
    }

    ramuda_sample = get_openapi_scaffold_min(spec, 'ramuda')
    assert ramuda_sample == expected_sample
    validate_tool_config(spec, {'ramuda': ramuda_sample})


def test_scaffolding_sample_max():
    spec = read_openapi()
    expected_sample = {
        'codedeploy': {
            'applicationName': 'string',
            'deploymentGroupName': 'string',
            'artifactsBucket': 'string',
            'deploymentConfigName': 'string'
        },

        'bundling': {
            'folders': {
                'folders': [{'source': './node_modules', 'target': './node_modules'}]
            },
            'zip': 'bundle.zip'
        },

        'defaults': {
            'validate': True,
            'log_group': '/var/log/messages',
            'stack_output_file': 'stack_output.yml',
            'settings_file': 'settings.json'
        }
    }

    ramuda_sample = get_openapi_scaffold_max(spec, 'tenkai')
    #print(ramuda_sample)
    assert ramuda_sample == expected_sample
    validate_tool_config(spec, {'tenkai': ramuda_sample})
