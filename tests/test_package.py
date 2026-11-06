import sys

import k8s_agent


def test_runs_on_python_312():
    assert sys.version_info[:2] == (3, 12)


def test_package_imports():
    assert k8s_agent.__name__ == "k8s_agent"
