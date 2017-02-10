# -*- coding: utf-8 -*-
from __future__ import unicode_literals, print_function

from gcdt.ramuda_main import version_cmd


# note: xzy_main tests have a more "integrative" character so focus is to make
# sure that the gcdt parts fit together not functional coverage of the parts.


def test_version_cmd(capsys):
    version_cmd()
    out, err = capsys.readouterr()
    assert out.startswith('gcdt version')
