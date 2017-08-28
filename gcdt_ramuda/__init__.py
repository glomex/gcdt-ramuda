# -*- coding: utf-8 -*-
from __future__ import unicode_literals, print_function
import os

from gcdt.gcdt_logging import getLogger
from gcdt.gcdt_openapi import read_openapi_ordered

# dependency comes with bravado-core (via gcdt)
from swagger_spec_validator import validator20


def here(p):
    return os.path.abspath(os.path.join(os.path.dirname(__file__), p))


__version__ = '0.2.0'
OPENAPI = here('openapi_ramuda.yaml')


log = getLogger(__name__)


def read_openapi():
    """Load spec from yaml file.

    :return: OrderedDict containing spec
    """
    log.debug('read openapi spec from \'%s\'', OPENAPI)
    doc = read_openapi_ordered(OPENAPI)
    return doc


# validate spec!
validator20.validate_spec(read_openapi())
