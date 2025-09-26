#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dump (almost) any object in YAML format.
Supports mixing flow / non-flow nodes to handles cases where non-flow makes the
dump have an absurd line count with many trite lines.

This *may* require customization if types are used for which this does not work;
search for 'YourClassThatDoesNotWork' to see where to customize your outlier cases.

"""
# pylint: disable=invalid-name

import threading
import textwrap
from io import StringIO
from collections import OrderedDict
from ruamel.yaml import YAML, comments as yaml_comments
from LibGen.CustLogger import CustLogger as lg
# import ToolBase as tb

#yaml = YAML(typ='safe') # NOTE: cannot have 'safe' and mixed block/flow style
yaml = YAML()
yaml.default_flow_style = False

flow = threading.local()
flow.flow_nodes = None

def yamlize(obj):
    """Make a complex object (e.g., class objects) more capable of
    being dumped in yaml by simplifying the types where possible."""
    # pylint: disable=too-many-return-statements
    if isinstance(obj, list):
        rv = []
        for value in obj:
            rv.append(yamlize(value))
        return rv

    if isinstance(obj, dict):
        rv = OrderedDict()
        for key, value in obj.items():
            rv[key] = yamlize(value)
        return rv

    # this handles some crazy types like:
    #   rumel.yaml.scalarfloat.ScalarFloat
    # when the object was converted from yaml
    if isinstance(obj, bool):
        return bool(obj)
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, float):
        return float(obj)
#   NOTE: if there are objects not handled, then fix here
#   if isinstance(obj, YourClassThatDoesNotWork):
#       return str(obj) # or whatever type if str is not appropriate

    # convert a potentially recursive object into a dictionary
    # - ignore variables that start with '_'
    if not hasattr(obj, '__dict__'):
        return obj
    raw_rv = vars(obj)
    rv = {}
    for key, val in raw_rv.items():
        if key.startswith('_'):
            continue
        rv[key] = yamlize(val)
    return rv

def set_flow(obj, flow_nodes=None):
    """Set flow style for certain nodes...
    Ref: https://stackoverflow.com/questions/63364894/
        how-to-dump-only-lists-with-flow-style-with-pyyaml-or-ruamel-yaml
    """
    obj = yamlize(obj)
    if flow_nodes:
        flow.flow_nodes = flow_nodes
        set_flow_recursive(obj)
    return obj

def set_flow_recursive(obj):
    """TBD"""
    if isinstance(obj, dict):
        # print('dict...')
        for key, value in obj.items():
            # print('key...', key)
            if key in flow.flow_nodes:
                if isinstance(value, dict):
                    # print('...', key, '/map')
                    value = yaml_comments.CommentedMap(value)
                    value.fa.set_flow_style()
                    obj[key] = value
                elif isinstance(value, list):
                    # print('...', key, '/list')
                    value = yaml_comments.CommentedSeq(value)
                    value.fa.set_flow_style()
                    obj[key] = value
            else:
                set_flow_recursive(value)
    elif isinstance(obj, list):
        # print('list...')
        for value in obj:
            set_flow_recursive(value)

def yaml_to_file(obj, fileh, flow_nodes=None):
    """Do a yaml-to-file conversion of an object (w/o indent)."""
    obj = set_flow(obj, flow_nodes)
    yaml.dump(obj, fileh)

def yaml_str(obj, indent=8, width=70, flow_nodes=None):
    """Do a yaml-to-string conversion of an object with indent by default."""
    outs = StringIO()
    obj = set_flow(obj, flow_nodes)
    old_width, yaml.width = yaml.width, width
    yaml.dump(obj, outs)
    yaml.width = old_width
    return textwrap.indent(outs.getvalue(), prefix=' '*indent)

def yaml_dump(obj, indent=8, flow_nodes=None):
    """Do a yaml dump of an object to stdout with indent by default1."""
    lg.pr(yaml_str(obj, indent=indent, flow_nodes=flow_nodes).rstrip())


def runner(argv):
    """Several simple tests for YamlDump including exercising flow_nodes."""
    # pylint: disable=broad-except, import-outside-toplevel, using-constant-test
    def tester_func():
        """TBD"""
        import sys
        import copy

        scrum_orig = {'flow_sec': 55, 'hash': 'cb217daa4aedf6ad8483a9333f6cc114f413cc7b',
                'name': 'Eche Palante - Refle', 'progress': 100, 'ratio': 0.8502833247184753,
                'samples': [[10.9, 10, 2.02], [15.7, 23, 1.14], [18.1, 32, 1.06],
                    [20.5, 42, 0.91], [22.9, 50, 0.93], [30.1, 65, 0.98], [34.9, 75, 0.91],
                    [39.7, 83, 0.88], [44.5, 90, 0.89], [54.1, 100, 0.85]],
                'scrum_form_sec': 8, 'scrum_sz': 12, 'time': 1621850682.8617778,
                'tor_sz': 249262127, 'up_client': 'qBittorrent/4.3.5', 'up_sec': 0}

        if True:
            scrum = copy.deepcopy(scrum_orig)
            print('\n\n===== TEST 1:', 'yaml.dump(scrum, sys.stdout)')
            yaml.dump(scrum, sys.stdout)

        if True:
            scrum = copy.deepcopy(scrum_orig)
            print('\n\n===== TEST 2:', 'surgical CommentedSeq')
            obj = yaml_comments.CommentedSeq(scrum['samples'])
            obj.fa.set_flow_style()
            scrum['samples'] = obj
            yaml.dump(scrum, sys.stdout)

        if True:
            scrum = copy.deepcopy(scrum_orig)
            print('\n\n===== TEST 3:', 'designed API')
            yaml_dump(scrum, flow_nodes=('samples',))

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('-V', '--log-level', choices=lg.choices,
            default='INFO', help='set logging/verbosity level [dflt=INFO]')
    opts = parser.parse_args(argv)
    lg.setup(level=opts.log_level)
    tester_func()
