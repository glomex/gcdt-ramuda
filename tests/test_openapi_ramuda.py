# -*- coding: utf-8 -*-
from __future__ import unicode_literals, print_function

from gcdt.gcdt_openapi import get_openapi_defaults, get_openapi_scaffold_min, \
    get_openapi_scaffold_max, validate_tool_config

from gcdt_ramuda import read_openapi


def test_scaffolding_default():
    spec = read_openapi()
    expected_defaults = {
        'bundling': {
            'folders': [{}],
            'settings_file': 'settings.json',
            'zip': 'bundle.zip'
        },

        'defaults': {
            'validate': True,
            'keep': False,
            'non_config_commands': ['logs', 'invoke'],
            'python_bundle_venv_dir': '.gcdt/venv'
        }
    }

    ramuda_defaults = get_openapi_defaults(spec, 'ramuda')
    assert ramuda_defaults == expected_defaults
    validate_tool_config(spec, {'ramuda': ramuda_defaults})


def test_scaffolding_sample_min():
    spec = read_openapi()
    expected_sample = {
        'bundling': {
            'folders': [{
                'source': './node_modules',
                'target': './node_modules'
            }],
            'settings_file': 'settings.json',
            'zip': 'bundle.zip'
        },

        'defaults': {
            'validate': True,
            'keep': False,
            'non_config_commands': ['logs', 'invoke'],
            'python_bundle_venv_dir': '.gcdt/venv'
        },

        'lambda': {
            'handlerFunction': 'lambda.handler',
            'role': 'arn:aws:iam::123456EXAMPLE:role/my-service-role-123EXAMPLE',
            'description': 'Lambda function for my-service',
            'name': 'my-service',
            'handlerFile': 'lambda.js'
        }
    }

    ramuda_sample = get_openapi_scaffold_min(spec, 'ramuda')
    #print(ramuda_sample)
    assert ramuda_sample == expected_sample
    validate_tool_config(spec, {'ramuda': ramuda_sample})


def test_scaffolding_sample_max():
    spec = read_openapi()
    expected_sample = {'settings': {u'any_prop2': 42, u'any_prop1': u'string'}, 'bundling': {'folders': [{'source': './node_modules', 'target': './node_modules'}], 'settings_file': 'settings.json', 'zip': 'bundle.zip'}, 'settings_text': u'string', 'defaults': {'python_bundle_venv_dir': '.gcdt/venv', 'validate': True, 'non_config_commands': ['logs', 'invoke'], 'keep': False}, 'deployment': {'region': 'eu-west-1', 'artifactBucket': 'infra-dev-deployment'}, 'lambda': {'environment': {'MYVALUE': 'FOO'}, 'vpc': {'subnetIds': ['subnet-aabb123c'], 'securityGroups': ['sg-aabb123c']}, 'description': 'Lambda function for my-service', 'memorySize': 128, 'logs': {'retentionInDays': 90}, 'handlerFunction': 'lambda.handler', 'role': 'arn:aws:iam::123456EXAMPLE:role/my-service-role-123EXAMPLE', 'timeout': 300, 'handlerFile': 'lambda.js', 'runtime': 'python2.7', 'name': 'my-service'}}

    ramuda_sample = get_openapi_scaffold_max(spec, 'ramuda')
    assert ramuda_sample == expected_sample
    validate_tool_config(spec, {'ramuda': ramuda_sample})
