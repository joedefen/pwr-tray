#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from setuptools import setup, find_packages

setup(
    name='pwr_tray',
    version='0.1',
    packages=find_packages(),
    include_package_data=True,  # This is important
    package_data={
        'pwr_tray': ['resources/*'],  # Include all files in the resources directory
    },
    entry_points= {
        'console_scripts': [
            'pwr-tray=pwr_tray.main:main'
        ]
    }
    # other metadata
)
