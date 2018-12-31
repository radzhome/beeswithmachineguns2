#!/usr/bin/env python
import beeswithmachineguns2

try:
    from setuptools import setup
except ImportError:
    from distutils.core import setup

with open('requirements.txt') as f:
    required_packages = f.readlines()

setup(name='beeswithmachineguns2',
      version=beeswithmachineguns2.VERSION,
      description='A utility for arming (creating) many bees (micro EC2 instances) to attack (load test) '
                  'targets (web applications).',
      author='Christopher Groskopf, radzhome',
      author_email='cgroskopf@tribune.com',
      url='http://github.com/radzhome/beeswithmachineguns2',
      license='MIT',
      packages=['beeswithmachineguns2'],
      scripts=['bees2'],
      install_requires=required_packages,
      classifiers=[
          'Development Status :: 5 - Production/Stable',
          'Environment :: Console',
          'Intended Audience :: Developers',
          'License :: OSI Approved :: MIT License',
          'Natural Language :: English',
          'Operating System :: OS Independent',
          'Programming Language :: Python',
          'Topic :: Software Development :: Testing :: Traffic Generation',
          'Topic :: Utilities',
          ],
     )
