#!/bin/env python

"""
Main bees module for bringing bees up and down and attacking
"""
from __future__ import division
from __future__ import print_function

from future import standard_library
standard_library.install_aliases()
from builtins import zip
from builtins import map
from builtins import bytes
from builtins import range
from past.utils import old_div
from multiprocessing import Pool
import os
import re
import socket
import sys
import ast
from operator import itemgetter
IS_PY2 = sys.version_info.major == 2
if IS_PY2:
    from urllib.request import urlopen, Request
    from io import StringIO
else:
    from urllib.request import urlopen, Request
    from io import StringIO
import base64
import csv
import random
import ssl
from contextlib import contextmanager
import traceback

import boto.ec2  # TODO: deprecated
import boto.exception  # TODO: deprecated

import boto3  # Converting to boto3 slowly.. starting with broken stuff
from botocore.exceptions import ClientError
import paramiko
import json
from collections import defaultdict
import time


STATE_FILENAME = os.path.expanduser('~/.bees2')  # Changing affects _get_existing_regions

# ECS Optimized AMI to use, different per region. ID changes when an updated AMI is released.
# Good choice for now as its regularly updated
# https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs-optimized_AMI.html
AMI_NAME = 'amzn-ami-????.??.?-amazon-ecs-optimized'  # 'amzn-ami-2018.03.l-amazon-ecs-optimized'


# Utilities


@contextmanager
def _redirect_stdout(outfile=None):
    save_stdout = sys.stdout
    sys.stdout = outfile or StringIO()
    yield
    sys.stdout = save_stdout


def _read_server_list(*mr_zone):
    if len(mr_zone) > 0:
        mr_state_filename = _get_new_state_file_name(mr_zone[-1])
    else:
        mr_state_filename = STATE_FILENAME
    if not os.path.isfile(mr_state_filename):
        return None, None, None, None

    with open(mr_state_filename, 'r') as f:
        username = f.readline().strip()
        key_name = f.readline().strip()
        zone = f.readline().strip()
        text = f.read()
        instance_ids = [i for i in text.split('\n') if i != '']

        print("Read {} bees from the roster: {}".format(len(instance_ids), zone))

    return username, key_name, zone, instance_ids


def _write_server_list(username, key_name, zone, instances):
    """
    Write .bees file
    :param username:
    :param key_name:
    :param zone:
    :param instances: list, list of dicts (instances)
    :return:
    """
    with open(_get_new_state_file_name(zone), 'w') as f:
        f.write('%s\n' % username)
        f.write('%s\n' % key_name)
        f.write('%s\n' % zone)

        f.write('\n'.join([instance['InstanceId'] for instance in instances]))


def _delete_server_list(zone):
    """
    Delete .bees file
    :param zone:
    :return:
    """
    try:
        os.remove(_get_new_state_file_name(zone))
    except IOError:
        pass


def _get_pem_path(key):
    """
    Get ssh key path
    :param key:
    :return:
    """
    return os.path.expanduser('~/.ssh/%s.pem' % key)


def _get_region(zone):
    """
    Get region name from zone
    :param zone: str, zone
    :return:
    """
    return zone if 'gov' in zone else zone[:-1] # chop off the "d" in the "us-east-1d" to get the "Region"


def _get_subnet_id(connection, subnet_name):
    """

    :param connection:
    :param subnet_name:
    :return:
    """
    if not subnet_name:
        print("WARNING: No subnet was specified.")
        return

    # Try by name
    subnets = connection.describe_subnets(
        Filters=[{'Name': 'tag:Name', 'Values': [subnet_name, ]}, ]
    )
    subnets = subnets['Subnets']

    if not subnets:
        # Try by id
        subnets = connection.describe_security_groups(
            Filters=[{'Name': 'subnet-id ', 'Values': [subnet_name, ]}, ]
        )
        subnets = subnets['Subnets']

    return subnets[0]['SubnetId'] if subnets else None

def _get_security_group_id(connection, security_group_name):
    """
    Takes a security group name and
    returns the ID. If the name cannot be
    found, the name will be attempted
    as an ID.  The first group found by
    this name or ID will be used.)
    :param connection:
    :param security_group_name:
    :return:
    """
    if not security_group_name:
        print('The bees need a security group to run under. Need to open a port from where you are to the target '
              'subnet.')
        return
    # Try by name
    security_groups = connection.describe_security_groups(
        Filters=[{'Name': 'group-name', 'Values': [security_group_name, ]}, ]
    )
    security_groups = security_groups['SecurityGroups']

    if not security_groups:
        # Try by id
        security_groups = connection.describe_security_groups(
            Filters=[{'Name': 'group-id', 'Values': [security_group_name, ]}, ]
        )
        security_groups = security_groups['SecurityGroups']
        if not security_groups:
            print('The bees need a security group to run under. The one specified was not found. '
                  'Create a sg that has access to port 22 ie. from 0.0.0.0/0')
            return

    return security_groups[0]['GroupId'] if security_groups else None


# Methods

def up(count, group, zone, image_id, instance_type, username, key_name, subnet, tags, bid=None):
    """
    Startup the load testing server.
    :param count: int, count
    :param group: str, security group
    :param zone: str, az
    :param image_id: str, ami id of image to use (optional)
    :param instance_type: str, aws instance size i.e. t3.micro
    :param username: 
    :param key_name: 
    :param subnet: str, subnet id
    :param tags: 
    :param bid: 
    :return: 
    """
    subnet = subnet or ''
    existing_username, existing_key_name, existing_zone, instance_ids = _read_server_list(zone)

    count = int(count)

    boto3_session = boto3.Session()
    boto3_ec2_client = boto3_session.client('ec2', region_name=_get_region(zone))

    if existing_username == username and existing_key_name == key_name and existing_zone == zone:

        try:
            existing_reservations = boto3_ec2_client.describe_instances(InstanceIds=instance_ids)['Reservations']
        except ClientError:
            print("Existing bees are invalid, cleaning up server list")
            existing_reservations = []
            _delete_server_list(zone)

        existing_instances = [instance for reservation in existing_reservations for instance in reservation['Instances']
                              if instance['State'] == 'running']
        # User, key and zone match existing values and instance ids are found on state file
        if count <= len(existing_instances):
            # Count is less than the amount of existing instances. No need to create new ones.
            print('Bees are already assembled and awaiting orders.')
            return
        else:
            # Count is greater than the amount of existing instances. Need to create the only the extra instances.
            count -= len(existing_instances)
    elif instance_ids:
        # Instances found on state file but user, key and/or zone not matching existing value.
        # State file only stores one user/key/zone config combination so instances are unusable.
        print('Taking down {} unusable bees.'.format(len(instance_ids)))
        # Redirect prints in down() to devnull to avoid duplicate messages
        with _redirect_stdout():
            down()
        # down() deletes existing state file so _read_server_list() returns a blank state
        existing_username, existing_key_name, existing_zone, instance_ids = _read_server_list(zone)

    pem_path = _get_pem_path(key_name)

    if not os.path.isfile(pem_path):
        print(
            "Warning. No key file found for {}. You will need to add this key to your SSH agent to connect."
            "".format(pem_path))

    print('Connecting to the hive.')

    region = _get_region(zone)
    try:
        ec2_connection = boto.ec2.connect_to_region(region)
    except boto.exception.NoAuthHandlerFound as e:
        print("Authentication config error, perhaps you do not have a ~/.boto file with correct permissions?")
        raise e
        # return e
    except Exception as e:
        print("Unknown error occurred:")
        raise e
        # return e

    # TODO: Could check if after sg- is all numeric as well
    security_group_id = group if \
        group.lower().startswith('sg-') else _get_security_group_id(boto3_ec2_client, group)
    if security_group_id:
        print("SecurityGroupId found: %s" % security_group_id)
    else:
        raise Exception("Unable to find security group {}. Try specifying the subnet id.".format(group))

    if subnet:
        subnet = subnet if \
            subnet.lower().startswith('subnet-') else _get_subnet_id(boto3_ec2_client, subnet)
    print("SubnetId: %s" % subnet)

    placement = None if 'gov' in zone else zone
    print("Placement: %s" % placement)

    if not image_id:
        ecs_amis = boto3_ec2_client.describe_images(
            Filters=[
                {'Name': 'name', 'Values': [AMI_NAME, ]},
                {'Name': 'state', 'Values': ['available', ]}
            ]
        )
        ecs_amis = ecs_amis.get('Images')
        ecs_amis = sorted(ecs_amis, key=itemgetter('CreationDate'), reverse=True)
        image_id = ecs_amis[0]['ImageId']
    print("Image ID: %s" % image_id)

    if bid:
        # TODO: Not tested
        print('Attempting to call up %i spot bees, this can take a while...' % count)

        spot_requests = boto3_ec2_client.request_spot_instances(
            SpotPrice=bid,
            InstanceCount=count,
            LaunchSpecification={
                'ImageId': image_id,
                'KeyName': key_name,
                'SecurityGroupIds': [security_group_id, ],
                'InstanceType': instance_type,
                'Placement': {'AvailabilityZone': placement},
                'SubnetId': subnet,
            },
        )

        # it can take a few seconds before the spot requests are fully processed
        time.sleep(5)

        if not ec2_connection:
            raise Exception("Invalid zone specified? Unable to connect to region {} using zone name {}"
                            "".format(region, zone))

        ready_instances = _wait_for_spot_request_fulfillment(ec2_connection, spot_requests)  # TODO convert to boto3
    else:
        print('Attempting to call up %i bees.' % count)

        try:
            reservation = boto3_ec2_client.run_instances(
                ImageId=image_id,
                MinCount=count,
                MaxCount=count,
                KeyName=key_name,
                SecurityGroupIds=[security_group_id, ],
                InstanceType=instance_type,
                Placement={'AvailabilityZone': placement},
                SubnetId=subnet)

            time.sleep(3)  # Wait a bit for bees to come up

        except boto.exception.EC2ResponseError as e:
            print(("Unable to call bees:", e.message))
            print("Is your sec group available in this region?")
            print("Subnet", subnet)
            print("SubnetGroupID", security_group_id)
            raise e
            # return e

        ready_instances = reservation['Instances']

    if not tags:
        # tags = '{"Type": "bee-instance"}'
        tags = [
            {
                'Key': 'Name',
                'Value': 'a bee!',
            },        {
                'Key': 'Application',
                'Value': 'the_swarm',
            },
            {
                'Key': 'Type',
                'Value': 'bee-instance'
            }
        ]
    else:
        tags = ast.literal_eval(tags)

    try:
        boto3_ec2_client.create_tags(Resources=[instance['InstanceId'] for instance in ready_instances], Tags=tags)
    except Exception as e:
        print("Unable to create tags:")
        print("example: bees up -x \"{'any_key': 'any_value'}\"")
        print(e)

    # instance_ids refers to existing ec2 instances, while ready_instances are the new ones just created in this run
    if instance_ids:
        # Add existing instances to ready instances if running
        try:
            existing_reservations = boto3_ec2_client.describe_instances(InstanceIds=instance_ids)['Reservations']
        except ClientError:
            print("Existing bees are invalid, cleaning up server list")
            existing_reservations = []
            _delete_server_list(zone)

        existing_instances = [instance for reservation in existing_reservations for instance in reservation['Instances']
                              if instance['State']['Name'] == 'running']
        list(map(ready_instances.append, existing_instances))
        dead_instances = [i for i in instance_ids if i not in [j['InstanceId'] for j in existing_instances]]
        if dead_instances and instance_ids:
            # TODO: cleanup dead instances from server list?
            list(map(instance_ids.pop, [instance_ids.index(i) for i in dead_instances]))

    print("Waiting for bees to load their machine guns...")

    instance_ids = instance_ids or []

    # Can be 'pending'|'running'|'shutting-down'|'terminated'|'stopping'|'stopped'
    for instance in [i for i in ready_instances if i['State']['Name'] == 'pending']:
        instance_id = instance['InstanceId']
        private_ip = instance['PrivateIpAddress']

        instance = boto3_ec2_client.describe_instance_status(InstanceIds=[instance_id, ])['InstanceStatuses']

        if len(instance):
            instance = instance[0]

            instance['State'] = instance['InstanceState']

        while not instance or instance['State']['Name'] != 'running':
            print('.')
            # print(instance['State']['Name'])
            time.sleep(5)
            instance = boto3_ec2_client.describe_instance_status(InstanceIds=[instance_id, ])['InstanceStatuses']
            if len(instance):
                instance = instance[0]
                instance['State'] = instance['InstanceState']

        instance['PrivateIpAddress'] = private_ip
        instance_ids.append(instance['InstanceId'])

        # TODO: Need to figure out how we can check they are initialized fully or not, keep waiting...
        print("Bee {}, private ip {} is ready for the attack. Make sure they all finished initializing first"
              "".format(instance['InstanceId'], instance['PrivateIpAddress']))

    boto3_ec2_client.create_tags(Resources=instance_ids, Tags=tags)

    _write_server_list(username, key_name, zone, ready_instances)

    print("The swarm has {} bees assembled and ready.".format(len(ready_instances)))


def report():
    """
    Report the status of the load testing servers.
    """
    def _check_instances():
        """
        helper function to check multiple region files ~/.bees.*
        """
        if not instance_ids:
            print("No bees have been mobilized.")
            return

        ec2_connection = boto.ec2.connect_to_region(_get_region(zone))

        reservations = ec2_connection.get_all_instances(instance_ids=instance_ids)

        instances = []

        for reservation in reservations:
            instances.extend(reservation.instances)

        for instance in instances:
            print("Bee {}: {} @ {}".format(instance.id, instance.state, instance.ip_address))

    for i in _get_existing_regions():
        username, key_name, zone, instance_ids = _read_server_list(i)
        _check_instances()


def down(*mr_zone):
    """
    Shutdown the load testing server.
    :param mr_zone:
    :return:
    """
    def _check_to_down_it():
        """
        check if we can bring down some bees
        """
        username, key_name, zone, instance_ids = _read_server_list(region)

        if not instance_ids:
            print("No bees have been mobilized.")
            return

        print("Connecting to the hive.")

        boto3_session = boto3.Session()
        boto3_ec2_client = boto3_session.client('ec2', region_name=_get_region(zone))

        print("Calling off the swarm for {}.".format(region))

        try:
            terminated_instance_ids = boto3_ec2_client.terminate_instances(InstanceIds=instance_ids)
            print(terminated_instance_ids)
        except boto3_ec2_client.exceptions.ClientError as e:
            if 'do not exist' in str(e):
                terminated_instance_ids = []  # TODO: Not used

        print("Stood down {} bees.".format(len(instance_ids)))

        _delete_server_list(zone)

    if len(mr_zone) > 0:
        # TODO: not used
        username, key_name, zone, instance_ids = _read_server_list(mr_zone[-1])
    else:
        for region in _get_existing_regions():
            _check_to_down_it()


def _wait_for_spot_request_fulfillment(conn, requests, fulfilled_requests=None):
    """
    Wait until all spot requests are fulfilled.
    Once all spot requests are fulfilled, return a
    list of corresponding spot instances.
    :param conn:
    :param requests:
    :param fulfilled_requests:
    :return:
    """
    # TODO: Use describe_instances, describe_spot_fleet_instances(
    # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ec2.html#EC2.Client.describe_spot_fleet_instances
    fulfilled_requests = fulfilled_requests or []
    if len(requests) == 0:
        reservations = conn.get_all_instances(instance_ids = [r.instance_id for r in fulfilled_requests])
        return [r.instances[0] for r in reservations]
    else:
        time.sleep(10)
        print('.')

    requests = conn.get_all_spot_instance_requests(request_ids=[req.id for req in requests])
    for req in requests:
        if req.status.code == 'fulfilled':
            fulfilled_requests.append(req)
            print("spot bee `{}` joined the swarm.".format(req.instance_id))

    return _wait_for_spot_request_fulfillment(conn, [r for r in requests if r not in
                                                     fulfilled_requests], fulfilled_requests)


def _sting(params):
    """
    Request the target URL for caching.
    Intended for use with multiprocessing.
    :param params:
    """
    url = params['url']
    headers = params['headers']
    contenttype = params['contenttype']
    cookies = params['cookies']
    post_file = params['post_file']
    basic_auth = params['basic_auth']

    # Create request
    request = Request(url)

    # Need to revisit to support all http verbs.
    if post_file:
        try:
            with open(post_file, 'r') as content_file:
                content = content_file.read()
            if IS_PY2:
                request.add_data(content)  # TODO
            else:
                # python3 removed add_data method from Request and added data attribute,
                # either bytes or iterable of bytes
                request.data = bytes(content.encode('utf-8'))
        except IOError:
            print('bees: error: The post file you provided doesn\'t exist.')
            return

    if cookies is not '':
        request.add_header('Cookie', cookies)

    if basic_auth is not '':
        authentication = base64.encodestring(basic_auth).replace('\n', '')
        request.add_header('Authorization', 'Basic %s' % authentication)

    # Ping url so it will be cached for testing
    dict_headers = {}
    if headers is not '':
        dict_headers = headers = dict(j.split(':') for j in [i.strip() for i in headers.split(';') if i != ''])

    if contenttype is not '':
        request.add_header("Content-Type", contenttype)

    for key, value in list(dict_headers.items()):
        request.add_header(key, value)

    if url.lower().startswith("https://") and hasattr(ssl, '_create_unverified_context'):
        context = ssl._create_unverified_context()
        response = urlopen(request, context=context)
    else:
        response = urlopen(request)

    response.read()


def _attack(params):
    """
    Test the target URL with requests.

    Intended for use with multiprocessing.
    :param params:
    """
    print('Bee {} is joining the swarm.'.format(params['i']))
    client = _paramiko_connect(params)
    print('Bee {} joined the swarm.'.format(params['i']))

    try:
        print("Bee {:d} is firing her machine gun. Bang bang!".format(params['i']))

        options = ''
        if params['headers'] is not '':
            for h in params['headers'].split(';'):
                if h != '':
                    options += ' -H "%s"' % h.strip()

        if params['contenttype'] is not '':
            options += ' -T %s' % params['contenttype']

        stdin, stdout, stderr = client.exec_command('mktemp')
        # paramiko's read() returns bytes which need to be converted back to a str
        params['csv_filename'] = IS_PY2 and stdout.read().strip() or stdout.read().decode('utf-8').strip()
        if params['csv_filename']:
            options += ' -e %(csv_filename)s' % params
        else:
            print("Bee {} ({}) lost sight of the target (connection timed out creating csv_filename)."
                  "".format(params['i'], params['instance_name']))
            return None

        if params['post_file']:
            pem_file_path=_get_pem_path(params['key_name'])
            scp_command = "scp -q -o 'StrictHostKeyChecking=no' -i %s %s %s@%s:~/" \
                          "".format((pem_file_path, params['post_file'], params['username'], params['instance_name']))
            os.system(scp_command)
            options += ' -p ~/%s' % params['post_file']

        if params['keep_alive']:
            options += ' -k'

        if params['cookies'] is not '':
            options += ' -H \"Cookie: %s;sessionid=NotARealSessionID;\"' % params['cookies']
        else:
            options += ' -C \"sessionid=NotARealSessionID\"'

        if params['ciphers'] is not '':
            options += ' -Z %s' % params['ciphers']

        if params['basic_auth'] is not '':
            options += ' -A %s' % params['basic_auth']

        params['options'] = options
        # substrings to use for grep to perform remote output filtering
        # resolves issue #194, too much data sent over SSH control channel to BWMG
        # any future statistics parsing that requires more output from ab
        # may need this line altered to include other patterns
        params['output_filter_patterns'] = '\n'.join(
            ['Time per request:', 'Requests per second: ', 'Failed requests: ', 'Connect: ', 'Receive: ',
             'Length: ', 'Exceptions: ', 'Complete requests: ', 'HTTP/1.1'])

        # Make sure we have ab
        # TODO Move to up, and poll until done, then ready, default ami user?
        ab_install_command = 'sudo yum install httpd-tools -y'
        client.exec_command(ab_install_command)
        time.sleep(5)  # Wait for install

        # TODO: bee is going down
        # Traceback (most recent call last):
        #   File "/usr/local/Cellar/python/3.7.2/Frameworks/Python.framework/Versions/3.7/lib/python3.7/threading.py", line 917, in _bootstrap_inner
        #     self.run()
        #   File "/usr/local/Cellar/python/3.7.2/Frameworks/Python.framework/Versions/3.7/lib/python3.7/threading.py", line 865, in run
        #     self._target(*self._args, **self._kwargs)
        #   File "/usr/local/lib/python3.7/site-packages/beeswithmachineguns2/bees.py", line 1173, in attack
        #     requests_per_instance, sting)
        #   File "/usr/local/lib/python3.7/site-packages/beeswithmachineguns2/bees.py", line 956, in _get_paramiko_conn_params
        #     instance_name = instance['PublicDnsName'] or instance['PrivateIpAddress']
        # KeyError: 'PrivateIpAddress'

        # Make sure we can open many concurrent connections
        # print("Bee {} ({}) Increasing open file limit & installing ab.".format(params['i'], params['instance_name']))
        # ulimit_command = 'ulimit -S -n 4096'  # TODO: change permantenly
        # client.exec_command(ulimit_command)
        # time.sleep(1)
        # https://www.thatsgeeky.com/2011/11/installing-apachebench-without-apache-on-amazons-linux/
        # default image set to ? ami-0f552e0a86f08b660
        # benchmark_command = 'ab -v 3 -r -n %(num_requests)s -c %(concurrent_requests)s %(options)s "%(url)s" ' \
        benchmark_command = 'ulimit -S -n 4096 && ab -v 3 -r -n %(num_requests)s -c %(concurrent_requests)s %(options)s "%(url)s" ' \
                            '2>/dev/null | grep -F "%(output_filter_patterns)s"' % params
        print("Benchmark command is: {}".format(benchmark_command))
        stdin, stdout, stderr = client.exec_command(benchmark_command)

        response = {}

        # paramiko's read() returns bytes which need to be converted back to a str
        ab_results = IS_PY2 and stdout.read() or stdout.read().decode('utf-8')
        ms_per_request_search = re.search('Time\ per\ request:\s+([0-9.]+)\ \[ms\]\ \(mean\)', ab_results)

        if not ms_per_request_search:
            print("Bee {} ({}) lost sight of the target (ab command failed).".format(params['i'],
                                                                                     params['instance_name']))
            print("Error is: {}. It could be that the bee cannot resolve the target or "
                  "that the open file limit is reached (See ulimit -a).".format(stderr))
            return None

        requests_per_second_search = re.search('Requests\ per\ second:\s+([0-9.]+)\ \[#\/sec\]\ \(mean\)', ab_results)
        failed_requests = re.search('Failed\ requests:\s+([0-9.]+)', ab_results)
        response['failed_requests_connect'] = 0
        response['failed_requests_receive'] = 0
        response['failed_requests_length'] = 0
        response['failed_requests_exceptions'] = 0
        if float(failed_requests.group(1)) > 0:
            failed_requests_detail = re.search('(Connect: [0-9.]+, Receive: [0-9.]+, Length: [0-9.]+, '
                                               'Exceptions: [0-9.]+)', ab_results)
            if failed_requests_detail:
                response['failed_requests_connect'] = float(re.search('Connect:\s+([0-9.]+)',
                                                                      failed_requests_detail.group(0)).group(1))
                response['failed_requests_receive'] = float(re.search('Receive:\s+([0-9.]+)',
                                                                      failed_requests_detail.group(0)).group(1))
                response['failed_requests_length'] = float(re.search('Length:\s+([0-9.]+)',
                                                                     failed_requests_detail.group(0)).group(1))
                response['failed_requests_exceptions'] = float(re.search('Exceptions:\s+([0-9.]+)',
                                                                         failed_requests_detail.group(0)).group(1))

        complete_requests_search = re.search('Complete\ requests:\s+([0-9]+)', ab_results)

        response['number_of_200s'] = len(re.findall('HTTP/1.1\ 2[0-9][0-9]', ab_results))
        response['number_of_300s'] = len(re.findall('HTTP/1.1\ 3[0-9][0-9]', ab_results))
        response['number_of_400s'] = len(re.findall('HTTP/1.1\ 4[0-9][0-9]', ab_results))
        response['number_of_500s'] = len(re.findall('HTTP/1.1\ 5[0-9][0-9]', ab_results))

        response['ms_per_request'] = float(ms_per_request_search.group(1))
        response['requests_per_second'] = float(requests_per_second_search.group(1))
        response['failed_requests'] = float(failed_requests.group(1))
        response['complete_requests'] = float(complete_requests_search.group(1))

        stdin, stdout, stderr = client.exec_command('cat %(csv_filename)s' % params)
        response['request_time_cdf'] = []
        for row in csv.DictReader(stdout):
            row["Time in ms"] = float(row["Time in ms"])
            response['request_time_cdf'].append(row)
        if not response['request_time_cdf']:
            print("Bee {} ({}) lost sight of the target (connection timed out reading csv)."
                  "".format(params['i'], params['instance_name']))
            return None

        print('Bee %i is out of ammo.' % params['i'])

        client.close()

        return response
    except socket.error as e:
        return e
    except Exception as e:
        traceback.print_exc()
        print()
        raise e


def _summarize_results(results, params, csv_filename):
    """
    Summarize results
    :param results:
    :param params:
    :param csv_filename:
    :return:
    """
    summarized_results = dict()
    summarized_results['timeout_bees'] = [r for r in results if r is None]
    summarized_results['exception_bees'] = [r for r in results if type(r) == socket.error]
    summarized_results['complete_bees'] = [r for r in results if r is not None and type(r) != socket.error]
    summarized_results['timeout_bees_params'] = [p for r, p in zip(results, params) if r is None]
    summarized_results['exception_bees_params'] = [p for r, p in zip(results, params) if type(r) == socket.error]
    summarized_results['complete_bees_params'] = [p for r, p in zip(results, params) if
                                                  r is not None and type(r) != socket.error]
    summarized_results['num_timeout_bees'] = len(summarized_results['timeout_bees'])
    summarized_results['num_exception_bees'] = len(summarized_results['exception_bees'])
    summarized_results['num_complete_bees'] = len(summarized_results['complete_bees'])

    # Unable to connect to server?
    if summarized_results['complete_bees'] and 'Err' in str(summarized_results['complete_bees'][0]):
        raise Exception("Error getting results, {}".format(summarized_results['complete_bees'][0]))

    complete_results = [r['complete_requests'] for r in summarized_results['complete_bees']]
    summarized_results['total_complete_requests'] = sum(complete_results)

    complete_results = [r['failed_requests'] for r in summarized_results['complete_bees']]
    summarized_results['total_failed_requests'] = sum(complete_results)

    complete_results = [r['failed_requests_connect'] for r in summarized_results['complete_bees']]
    summarized_results['total_failed_requests_connect'] = sum(complete_results)

    complete_results = [r['failed_requests_receive'] for r in summarized_results['complete_bees']]
    summarized_results['total_failed_requests_receive'] = sum(complete_results)

    complete_results = [r['failed_requests_length'] for r in summarized_results['complete_bees']]
    summarized_results['total_failed_requests_length'] = sum(complete_results)

    complete_results = [r['failed_requests_exceptions'] for r in summarized_results['complete_bees']]
    summarized_results['total_failed_requests_exceptions'] = sum(complete_results)

    complete_results = [r['number_of_200s'] for r in summarized_results['complete_bees']]
    summarized_results['total_number_of_200s'] = sum(complete_results)

    complete_results = [r['number_of_300s'] for r in summarized_results['complete_bees']]
    summarized_results['total_number_of_300s'] = sum(complete_results)

    complete_results = [r['number_of_400s'] for r in summarized_results['complete_bees']]
    summarized_results['total_number_of_400s'] = sum(complete_results)

    complete_results = [r['number_of_500s'] for r in summarized_results['complete_bees']]
    summarized_results['total_number_of_500s'] = sum(complete_results)

    complete_results = [r['requests_per_second'] for r in summarized_results['complete_bees']]
    summarized_results['mean_requests'] = sum(complete_results)

    complete_results = [r['ms_per_request'] for r in summarized_results['complete_bees']]
    if summarized_results['num_complete_bees'] == 0:
        summarized_results['mean_response'] = "no bees are complete"
    else:
        summarized_results['mean_response'] = old_div(sum(complete_results), summarized_results['num_complete_bees'])

    summarized_results['tpr_bounds'] = params[0]['tpr']
    summarized_results['rps_bounds'] = params[0]['rps']

    if summarized_results['tpr_bounds'] is not None:
        if summarized_results['mean_response'] < summarized_results['tpr_bounds']:
            summarized_results['performance_accepted'] = True
        else:
            summarized_results['performance_accepted'] = False

    if summarized_results['rps_bounds'] is not None:
        if summarized_results['mean_requests'] > summarized_results['rps_bounds'] and \
                summarized_results['performance_accepted'] is True or None:
            summarized_results['performance_accepted'] = True
        else:
            summarized_results['performance_accepted'] = False

    summarized_results['request_time_cdf'] = _get_request_time_cdf(summarized_results['total_complete_requests'],
                                                                   summarized_results['complete_bees'])
    if csv_filename:
        _create_request_time_cdf_csv(results, summarized_results['complete_bees_params'],
                                     summarized_results['request_time_cdf'], csv_filename)

    return summarized_results


def _create_request_time_cdf_csv(results, complete_bees_params, request_time_cdf, csv_filename):
    if csv_filename:
        # csv requires files in text-mode with newlines='' in python3
        # see http://python3porting.com/problems.html#csv-api-changes
        openmode = IS_PY2 and 'w' or 'wt'
        openkwargs = IS_PY2 and {} or {'encoding': 'utf-8', 'newline': ''}
        with open(csv_filename, openmode, openkwargs) as stream:
            writer = csv.writer(stream)
            header = ["% faster than", "all bees [ms]"]
            for p in complete_bees_params:
                header.append("bee %(instance_id)s [ms]" % p)
            writer.writerow(header)
            for i in range(100):
                row = [i, request_time_cdf[i]] if i < len(request_time_cdf) else [i,float("inf")]
                for r in results:
                    if r is not None:
                        row.append(r['request_time_cdf'][i]["Time in ms"])
                writer.writerow(row)


def _get_request_time_cdf(total_complete_requests, complete_bees):
    """
    Recalculate the global cdf based on the csv files collected from
    ab. Can do this by sampling the request_time_cdfs for each of
    the completed bees in proportion to the number of
    complete_requests they have
    :param total_complete_requests:
    :param complete_bees:
    :return:
    """
    n_final_sample = 100
    sample_size = 100 * n_final_sample
    n_per_bee = [int(r['complete_requests'] / total_complete_requests * sample_size)
                 for r in complete_bees]
    sample_response_times = []
    for n, r in zip(n_per_bee, complete_bees):
        cdf = r['request_time_cdf']
        for i in range(n):
            j = int(random.random() * len(cdf))
            sample_response_times.append(cdf[j]["Time in ms"])
    sample_response_times.sort()
    # python3 division returns floats so convert back to int
    request_time_cdf = sample_response_times[0:sample_size:int(old_div(sample_size, n_final_sample))]

    return request_time_cdf


def _print_results(summarized_results):
    """
    Print summarized load-testing results.
    :param summarized_results:
    :return:
    """
    if summarized_results['exception_bees']:
        print("     %i of your bees didn't make it to the action. They might be taking a little longer than normal to"
              " find their machine guns, or may have been terminated without using \"bees down\"."
              "".format(summarized_results['num_exception_bees']))

    if summarized_results['timeout_bees']:
        print('     Target timed out without fully responding to %i bees.' % summarized_results['num_timeout_bees'])

    if summarized_results['num_complete_bees'] == 0:
        print('     No bees completed the mission. Apparently your bees are peace-loving hippies.')
        return

    print('     Complete requests:\t\t%i' % summarized_results['total_complete_requests'])

    print('     Failed requests:\t\t%i' % summarized_results['total_failed_requests'])
    print('          connect:\t\t%i' % summarized_results['total_failed_requests_connect'])
    print('          receive:\t\t%i' % summarized_results['total_failed_requests_receive'])
    print('          length:\t\t%i' % summarized_results['total_failed_requests_length'])
    print('          exceptions:\t\t%i' % summarized_results['total_failed_requests_exceptions'])
    print('     Response Codes:')
    print('          2xx:\t\t%i' % summarized_results['total_number_of_200s'])
    print('          3xx:\t\t%i' % summarized_results['total_number_of_300s'])
    print('          4xx:\t\t%i' % summarized_results['total_number_of_400s'])
    print('          5xx:\t\t%i' % summarized_results['total_number_of_500s'])
    print('     Requests per second:\t%f [#/sec] (mean of bees)' % summarized_results['mean_requests'])
    if 'rps_bounds' in summarized_results and summarized_results['rps_bounds'] is not None:
        print('     Requests per second:\t%f [#/sec] (upper bounds)' % summarized_results['rps_bounds'])

    print('     Time per request:\t\t%f [ms] (mean of bees)' % summarized_results['mean_response'])
    if 'tpr_bounds' in summarized_results and summarized_results['tpr_bounds'] is not None:
        print('     Time per request:\t\t%f [ms] (lower bounds)' % summarized_results['tpr_bounds'])

    print('     50%% responses faster than:\t%f [ms]' % summarized_results['request_time_cdf'][49])
    print('     90%% responses faster than:\t%f [ms]' % summarized_results['request_time_cdf'][89])

    if 'performance_accepted' in summarized_results:
        print('     Performance check:\t\t%s' % summarized_results['performance_accepted'])

    if summarized_results['mean_response'] < 500:
        print('Mission Assessment: Target crushed bee offensive.')
    elif summarized_results['mean_response'] < 1000:
        print('Mission Assessment: Target successfully fended off the swarm.')
    elif summarized_results['mean_response'] < 1500:
        print('Mission Assessment: Target wounded, but operational.')
    elif summarized_results['mean_response'] < 2000:
        print('Mission Assessment: Target severely compromised.')
    else:
        print('Mission Assessment: Swarm annihilated target.')


def _get_paramiko_conn_params(instances, url, options, username, key_name, headers,
                              contenttype, cookies, ciphers, connections_per_instance,
                              requests_per_instance, sting):
    """
    Gets paramiko conn params for ab
    :param instances: dict, from ['Reservations']['Instances'] from describe_instances
    :param url: string, list of urls, comma spliced
    :param options:
    :param username:
    :param key_name:
    :param headers:
    :param contenttype:
    :param cookies:
    :param ciphers:
    :param connections_per_instance:
    :param requests_per_instance:
    :return:
    """
    params = []

    urls = url.split(",")
    url_count = len(urls)
    instance_count = len(instances)

    if url_count > instance_count:
        print("bees: warning: more urls given than instances. last urls will be ignored.")

    for i, instance in enumerate(instances):
        # PrivateDnsName is useless if you can't resolve the private ip from it.
        instance_name = instance['PublicDnsName'] or instance['PrivateIpAddress']

        params.append({
            'i': i,
            'instance_id': instance['InstanceId'],
            'instance_name': instance_name,
            'url': urls[i % url_count],
            'concurrent_requests': connections_per_instance,
            'num_requests': requests_per_instance,
            'username': username,
            'key_name': key_name,
            'headers': headers,
            'contenttype': contenttype,
            'cookies': cookies,
            'ciphers': ciphers,
            'post_file': options.get('post_file'),
            'keep_alive': options.get('keep_alive'),
            'mime_type': options.get('mime_type', ''),
            'tpr': options.get('tpr'),
            'rps': options.get('rps'),
            'basic_auth': options.get('basic_auth')
        })

    if sting == 1:
        print('Stinging URL sequentially so it will be cached for the attack.')
        for param in params:
            _sting(param)
    elif sting == 2:
        print('Stinging URL in parallel so it will be cached for the attack.')
        url_used_count = min(url_count - 1, instance_count - 1)
        pool = Pool(url_used_count + 1)
        pool.map(_sting, params[:url_used_count - 1])
    else:
        print('Stinging URL skipped.')

    return params


def _is_valid_concurrency_to_instances(n, c, instance_count):
    """
    Validates concurrency settings ok for number of instances
    :param n:
    :param c:
    :param instance_count:
    :return:
    """
    if n < instance_count * 2:
        print("bees: error: the total number of requests must be at least %d (2x num. instances)"
              "".format((instance_count * 2)))
        return False
    if c < instance_count:
        print('bees: error: the number of concurrent requests must be at least %d (num. instances)' % instance_count)
        return False
    if n < c:
        print("bees: error: the number of concurrent requests ({:d}) must be at most the same as number of "
              "requests ({:d})".format(c, n))
        return False

    return True


# TODO: create _attack_wrk2 and _summarize_results_wrk2
# def attack_wrk2(url, n, c, **options):
#     """
#
#     :param url:
#     :param n:
#     :param c:
#     :param options:
#     :return:
#     """
#
#     username, key_name, zone, instance_ids = _read_server_list(options.get('zone'))
#     headers = options.get('headers', '')
#     contenttype = options.get('contenttype', '')
#     csv_filename = options.get("csv_filename", '')
#     cookies = options.get('cookies', '')
#     ciphers = options.get('ciphers', '')
#     # post_file = options.get('post_file', '')
#     # keep_alive = options.get('keep_alive', False)
#     # basic_auth = options.get('basic_auth', '')
#     sting = options.get('sting', 1)
#
#     if csv_filename:
#         try:
#             stream = open(csv_filename, 'w')
#         except IOError as e:
#             raise IOError("Specified csv_filename='%s' is not writable. Check permissions or specify a different "
#                           "filename and try again." % csv_filename)
#
#     if not instance_ids:
#         print('No bees are ready to attack.')
#         return
#
#     print('Connecting to the hive.')
#
#     boto3_session = boto3.Session()
#     boto3_ec2_client = boto3_session.client('ec2', region_name=_get_region(zone))
#
#     print('Assembling bees.')
#
#     try:
#         reservations = boto3_ec2_client.describe_instances(InstanceIds=instance_ids)['Reservations']
#     except ClientError:
#         print("bees: failed to assemble working bees.")
#         _delete_server_list(zone)
#         raise
#
#     instances = []
#
#     for reservation in reservations:
#         instances.extend(reservation['Instances'])
#
#     instance_count = len(instances)
#
#     if not _is_valid_concurrency_to_instances(n, c, instance_count):
#         return
#
#     requests_per_instance = int(old_div(float(n), instance_count))
#     connections_per_instance = int(old_div(float(c), instance_count))
#
#     print("Each of {:d} bees will fire {} rounds, {} at a time.".format(instance_count, requests_per_instance,
#                                                                         connections_per_instance))
#
#     params = _get_paramiko_conn_params(instances, url, options, username, key_name, headers,
#                                        contenttype, cookies, ciphers, connections_per_instance,
#                                        requests_per_instance, sting)
#
#     print('Organizing the swarm.')
#     # Spin up processes for connecting to EC2 instances
#     pool = Pool(len(params))
#
#     try:
#         results = pool.map(_attack_wrk2, params)
#     except Exception as e:
#         print("Unable to connect to bees instances, are they all accessible?")
#         raise e
#
#     summarized_results = _summarize_results_wrk2(results, params, csv_filename)
#     print('Offensive complete.')
#     _print_results(summarized_results)
#
#     print('The swarm is awaiting new orders.')
#
#     if 'performance_accepted' in summarized_results:
#         if summarized_results['performance_accepted'] is False:
#             print("Your targets performance tests did not meet our standard.")
#             sys.exit(1)
#         else:
#             print('Your targets performance tests meet our standards, the Queen sends her regards.')
#             sys.exit(0)


def attack(url, n, c, **options):
    """
    Test the root url of this site.
    :param url:
    :param n:
    :param c:
    :param options:
    :return:
    """
    username, key_name, zone, instance_ids = _read_server_list(options.get('zone'))
    headers = options.get('headers', '')
    contenttype = options.get('contenttype', '')
    csv_filename = options.get("csv_filename", '')
    cookies = options.get('cookies', '')
    ciphers = options.get('ciphers', '')
    # post_file = options.get('post_file', '')
    # keep_alive = options.get('keep_alive', False)
    # basic_auth = options.get('basic_auth', '')
    sting = options.get('sting', 1)

    if csv_filename:
        try:
            stream = open(csv_filename, 'w')
        except IOError as e:
            raise IOError("Specified csv_filename='%s' is not writable. Check permissions or specify a different "
                          "filename and try again." % csv_filename)

    if not instance_ids:
        print('No bees are ready to attack.')
        return

    print('Connecting to the hive.')

    boto3_session = boto3.Session()
    boto3_ec2_client = boto3_session.client('ec2', region_name=_get_region(zone))

    print('Assembling bees.')

    try:
        reservations = boto3_ec2_client.describe_instances(InstanceIds=instance_ids)['Reservations']
    except ClientError:
        # reservations = []
        print("bees: failed to assemble working bees.")
        _delete_server_list(zone)
        raise

    instances = []

    for reservation in reservations:
        instances.extend(reservation['Instances'])

    instance_count = len(instances)

    if not _is_valid_concurrency_to_instances(n, c, instance_count):
        return

    requests_per_instance = int(old_div(float(n), instance_count))
    connections_per_instance = int(old_div(float(c), instance_count))

    print("Each of {:d} bees will fire {} rounds, {} at a time.".format(instance_count, requests_per_instance,
                                                                        connections_per_instance))

    params = _get_paramiko_conn_params(instances, url, options, username, key_name, headers,
                                       contenttype, cookies, ciphers, connections_per_instance,
                                       requests_per_instance, sting)

    print('Organizing the swarm.')
    # Spin up processes for connecting to EC2 instances
    pool = Pool(len(params))

    try:
        results = pool.map(_attack, params)
    except Exception as e:
        print("Unable to connect to bees instances, are they all accessible?")
        raise e

    summarized_results = _summarize_results(results, params, csv_filename)
    print('Offensive complete.')
    _print_results(summarized_results)

    print('The swarm is awaiting new orders.')

    if 'performance_accepted' in summarized_results:
        if summarized_results['performance_accepted'] is False:
            print("Your targets performance tests did not meet our standard.")
            sys.exit(1)
        else:
            print('Your targets performance tests meet our standards, the Queen sends her regards.')
            sys.exit(0)

#############################
### hurl version methods, ###
#############################


def hurl_attack(url, n, c, **options):
    """
    Test the root url of this site.
    :param url:
    :param n:
    :param c:
    :param options:
    :return:
    """
    # TODO: Create a hurl binary for amazon linux that can use
    raise NotImplementedError("This feature is disabled for now.")

    # print(options.get('zone'))
    username, key_name, zone, instance_ids = _read_server_list(options.get('zone'))
    headers = options.get('headers', '')
    contenttype = options.get('contenttype', '')
    csv_filename = options.get("csv_filename", '')
    cookies = options.get('cookies', '')
    post_file = options.get('post_file', '')
    keep_alive = options.get('keep_alive', False)
    basic_auth = options.get('basic_auth', '')

    if csv_filename:
        try:
            stream = open(csv_filename, 'w')
        except IOError as e:
            raise IOError("Specified csv_filename='%s' is not writable. Check permissions or specify a "
                          "different filename and try again." % csv_filename)

    if not instance_ids:
        print("No bees are ready to attack.")
        return

    print("Connecting to the hive.")

    ec2_connection = boto.ec2.connect_to_region(_get_region(zone))

    print("Assembling bees.")

    reservations = ec2_connection.get_all_instances(instance_ids=instance_ids)

    instances = []

    for reservation in reservations:
        instances.extend(reservation.instances)

    instance_count = len(instances)

    if n < instance_count * 2:
        print("bees: error: the total number of requests must be at least {:d} (2x num. instances)"
              "".format(instance_count * 2))
        return
    if c < instance_count:
        print("bees: error: the number of concurrent requests must be at least {:d} (num. instances)"
              "".format(instance_count))
        return
    if n < c:
        print("bees: error: the number of concurrent requests ({:d}) must be at most the same as number of "
              "requests ({:d})".format(c, n))
        return

    requests_per_instance = int(old_div(float(n), instance_count))
    connections_per_instance = int(old_div(float(c), instance_count))

    print("Each of {:d} bees will fire {} rounds, {} at a time.".format(instance_count, requests_per_instance,
                                                                        connections_per_instance))

    params = []

    # These conn params are different from ab ones
    for i, instance in enumerate(instances):
        params.append({
            'i': i,
            'instance_id': instance.id,
            'instance_name': instance.private_ip_address if instance.public_dns_name == "" else instance.public_dns_name,
            'url': url,
            'concurrent_requests': connections_per_instance,
            'num_requests': requests_per_instance,
            'username': username,
            'key_name': key_name,
            'headers': headers,
            'contenttype': contenttype,
            'cookies': cookies,
            'post_file': options.get('post_file'),
            'keep_alive': options.get('keep_alive'),
            'mime_type': options.get('mime_type', ''),
            'tpr': options.get('tpr'),
            'rps': options.get('rps'),
            'basic_auth': options.get('basic_auth'),
            'seconds': options.get('seconds'),
            'rate': options.get('rate'),
            'long_output': options.get('long_output'),
            'responses_per': options.get('responses_per'),
            'verb': options.get('verb'),
            'threads': options.get('threads'),
            'fetches': options.get('fetches'),
            'timeout': options.get('timeout'),
            'send_buffer': options.get('send_buffer'),
            'recv_buffer': options.get('recv_buffer')
        })

    print('Stinging URL so it will be cached for the attack.')

    request = Request(url)
    # Need to revisit to support all http verbs.
    if post_file:
        try:
            with open(post_file, 'r') as content_file:
                content = content_file.read()
            if IS_PY2:
                request.add_data(content)
            else:
                # python3 removed add_data method from Request and added data attribute,
                # either bytes or iterable of bytes
                request.data = bytes(content.encode('utf-8'))
        except IOError:
            print('bees: error: The post file you provided doesn\'t exist.')
            return

    if cookies is not '':
        request.add_header('Cookie', cookies)

    if basic_auth is not '':
        authentication = base64.encodestring(basic_auth).replace('\n', '')
        request.add_header('Authorization', 'Basic %s' % authentication)

    # Ping url so it will be cached for testing
    dict_headers = {}
    if headers is not '':
        dict_headers = dict(j.split(':') for j in [i.strip() for i in headers.split(';') if i != ''])

    if contenttype is not '':
        request.add_header("Content-Type", contenttype)

    for key, value in list(dict_headers.items()):
        request.add_header(key, value)

    if url.lower().startswith("https://") and hasattr(ssl, '_create_unverified_context'):
        context = ssl._create_unverified_context()
        response = urlopen(request, context=context)
    else:
        response = urlopen(request)

    response.read()

    print('Organizing the swarm.')
    # Spin up processes for connecting to EC2 instances
    pool = Pool(len(params))
    results = pool.map(_hurl_attack, params)

    summarized_results = _hurl_summarize_results(results, params, csv_filename)
    print('Offensive complete.')

    _hurl_print_results(summarized_results)

    print('The swarm is awaiting new orders.')

    if 'performance_accepted' in summarized_results:
        if summarized_results['performance_accepted'] is False:
            print("Your targets performance tests did not meet our standard.")
            sys.exit(1)
        else:
            print('Your targets performance tests meet our standards, the Queen sends her regards.')
            sys.exit(0)


def _paramiko_connect(params):
    """
    Create ssh connection with client
    :param params: dict, config params
    :return: paramiko client, conn client
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    pem_path = params.get('key_name') and _get_pem_path(params['key_name']) or None
    print("Pem key is {}".format(pem_path))

    if not os.path.isfile(pem_path):
        client.load_system_host_keys()
        try:
            client.connect(params['instance_name'],
                           username=params['username'],
                           timeout=10
                           )
        except paramiko.ssh_exception.AuthenticationException:
            raise Exception("Pem key {} not found, all other authentication methods failed".format(pem_path))
    else:
        client.connect(
            params['instance_name'],
            username=params['username'],
            key_filename=pem_path,
            timeout=10)
    return client


def _hurl_attack(params):
    """
    Test the target URL with requests.

    Intended for use with multiprocessing.

    hurl:
    https://github.com/VerizonDigital/hurl

    :param params:
    :return:
    """

    # TODO: Create a hurl binary for amazon linux that can use
    raise NotImplementedError("This feature is disabled for now.")

    print('Bee %i is joining the swarm.' % params['i'])

    client = _paramiko_connect(params)

    try:

        print('Bee %i is firing her machine gun. Bang bang!' % params['i'])

        options = ''
        if params['headers'] is not '':
            for h in params['headers'].split(';'):
                if h != '':
                    options += ' -H "%s"' % h.strip()

        if params['contenttype'] is not '':
            options += ' -H \"Content-Type : %s\"' % params['contenttype']

        stdin, stdout, stderr = client.exec_command('mktemp')
        # paramiko's read() returns bytes which need to be converted back to a str
        params['csv_filename'] = IS_PY2 and stdout.read().strip() or stdout.read().decode('utf-8').strip()
        if params['csv_filename']:
            options += ' -o %(csv_filename)s' % params
        else:
            print("Bee {} ({}) lost sight of the target (connection timed out creating csv_filename)."
                  "".format(params['i'], params['instance_name']))
            return None

        if params['post_file']:
            pem_file_path=_get_pem_path(params['key_name'])
            scp_command = "scp -q -o 'StrictHostKeyChecking=no' -i %s %s %s@%s:~/" \
                          "".format((pem_file_path,params['post_file'], params['username'], params['instance_name']))
            os.system(scp_command)
            options += ' -d ~/%s' % params['post_file']

        if params['cookies'] is not '':
            options += ' -H \"Cookie: %s;\"' % params['cookies']

        if params['basic_auth'] is not '':
            options += ' -H \"Authorization : Basic %s\"' % params['basic_auth']

        if params['seconds']:
            options += ' -l %d' % params['seconds']

        if params['rate']:
            options += ' -A %d' % params['rate']

        if params['responses_per']:
            options += ' -L'

        if params['verb'] is not '':
            options += ' -X %s' % params['verb']

        if params['threads']:
            options += ' -t %d' % params['threads']

        if params['fetches']:
            options += ' -f %d' % params['fetches']

        if params['timeout']:
            options += ' -T %d' % params['timeout']

        if params['send_buffer']:
            options += ' -S %d' % params['send_buffer']

        if params['recv_buffer']:
            options += ' -R %d' % params['recv_buffer']

        params['options'] = options

        # benchmark_command
        hurl_command = 'hurl %(url)s -p %(concurrent_requests)s %(options)s -j' % params
        stdin, stdout, stderr = client.exec_command(hurl_command)

        response = defaultdict(int)

        # paramiko's read() returns bytes which need to be converted back to a str
        hurl_results = IS_PY2 and stdout.read() or stdout.read().decode('utf-8')

        # print output for each instance if -o/--long_output is supplied
        def _long_output():
            '''if long_output option,.. display info per bee instead of summarized version'''
            tabspace=''
            singletab=()
            doubletabs = ('seconds', 'connect-ms-min',
                          'fetches', 'bytes-per-sec',
                          'end2end-ms-min',
                          'max-parallel', 'response-codes',
                          'end2end-ms-max', 'connect-ms-max')
            trippletab= 'bytes'
            try:
                print("Bee: {}".format(params['instance_id']))
                for k, v in list(response.items()):
                    if k == 'response-codes':
                        print(k)
                        tabspace='\t'
                        for rk, rv in list(v.items()):
                            print("{}{}:{}{}".format(tabspace, rk, tabspace + tabspace, rv))
                        continue
                    if k in doubletabs:
                        tabspace='\t\t'
                    elif k in trippletab:
                        tabspace='\t\t\t'
                    else:
                        tabspace='\t'
                    print("{}:{}{}".format(k, tabspace, v))
                print("\n")

            except Exception as e:
                print("Please check the url entered, also possible no requests were successful Line: 1018, {}"
                      "".format(e))
                return None

        # create the response dict to return to hurl_attack()
        stdin, stdout, stderr = client.exec_command('cat %(csv_filename)s' % params)
        try:
            hurl_json = dict(json.loads(stdout.read().decode('utf-8')))
            for k ,v in list(hurl_json.items()):
                response[k] = v

            # check if user wants output for separate instances and display if so
            if params['long_output']:
                print(hurl_command)
                print("\n", params['instance_id'] + "\n",params['instance_name'] + "\n" , hurl_results)
                _long_output()
                time.sleep(.02)

        except:
            print("Please check the url entered, also possible no requests were successful Line: 1032")
            return None
        finally:
            print("Bee {:d} is out of ammo.".format(params['i']))
            client.close()
            return response

        # # TODO: Code is unreachable:
        # # print(hurl_json['response-codes'])
        # response['request_time_cdf'] = []
        # for row in csv.DictReader(stdout):
        #     row["Time in ms"] = float(row["Time in ms"])
        #     response['request_time_cdf'].append(row)
        # if not response['request_time_cdf']:
        #     print('Bee %i lost sight of the target (connection timed out reading csv).' % params['i'])
        #     return None
        #
        # print("Bee {:d} is out of ammo.".format(params['i']))
        #
        # client.close()
        #
        # return response
    except socket.error as e:
        return e
    except Exception as e:
        traceback.print_exc()
        print()
        raise e


def _hurl_summarize_results(results, params, csv_filename):
    """
    Hurl results summary
    :param results:
    :param params:
    :param csv_filename:
    :return:
    """

    summarized_results = defaultdict(int)
    summarized_results['timeout_bees'] = [r for r in results if r is None]
    summarized_results['exception_bees'] = [r for r in results if type(r) == socket.error]
    summarized_results['complete_bees'] = [r for r in results if r is not None and type(r) != socket.error]
    summarized_results['timeout_bees_params'] = [p for r, p in zip(results, params) if r is None]
    summarized_results['exception_bees_params'] = [p for r, p in zip(results, params) if type(r) == socket.error]
    summarized_results['complete_bees_params'] = [p for r, p in zip(results, params) if r and type(r) != socket.error]
    summarized_results['num_timeout_bees'] = len(summarized_results['timeout_bees'])
    summarized_results['num_exception_bees'] = len(summarized_results['exception_bees'])
    summarized_results['num_complete_bees'] = len(summarized_results['complete_bees'])

    complete_results = [r['fetches'] for r in summarized_results['complete_bees']]
    summarized_results['total_complete_requests'] = sum(complete_results)

    # make summarized_results based of the possible response codes hurl gets
    reported_response_codes = [r['response-codes'] for r in [x for x in summarized_results['complete_bees']]]
    for i in reported_response_codes:
        if isinstance(i, dict):
            for k , v in list(i.items()):
                if k.startswith('20'):
                    summarized_results['total_number_of_200s']+=float(v)
                elif k.startswith('30'):
                    summarized_results['total_number_of_300s']+=float(v)
                elif k.startswith('40'):
                    summarized_results['total_number_of_400s']+=float(v)
                elif k.startswith('50'):
                    summarized_results['total_number_of_500s']+=float(v)

    complete_results = [r['bytes'] for r in summarized_results['complete_bees']]
    summarized_results['total_bytes'] = sum(complete_results)

    complete_results = [r['seconds'] for r in summarized_results['complete_bees']]
    summarized_results['seconds'] = max(complete_results)

    complete_results = [r['connect-ms-max'] for r in summarized_results['complete_bees']]
    summarized_results['connect-ms-max'] = max(complete_results)

    complete_results = [r['1st-resp-ms-max'] for r in summarized_results['complete_bees']]
    summarized_results['1st-resp-ms-max'] = max(complete_results)

    complete_results = [r['1st-resp-ms-mean'] for r in summarized_results['complete_bees']]
    summarized_results['1st-resp-ms-mean'] = old_div(sum(complete_results), summarized_results['num_complete_bees'])

    complete_results = [r['fetches-per-sec'] for r in summarized_results['complete_bees']]
    summarized_results['fetches-per-sec'] = old_div(sum(complete_results), summarized_results['num_complete_bees'])

    complete_results = [r['fetches'] for r in summarized_results['complete_bees']]
    summarized_results['total-fetches'] = sum(complete_results)

    complete_results = [r['connect-ms-min'] for r in summarized_results['complete_bees']]
    summarized_results['connect-ms-min'] = min(complete_results)

    complete_results = [r['bytes-per-sec'] for r in summarized_results['complete_bees']]
    summarized_results['bytes-per-second-mean'] = old_div(sum(complete_results),
                                                          summarized_results['num_complete_bees'])

    complete_results = [r['end2end-ms-min'] for r in summarized_results['complete_bees']]
    summarized_results['end2end-ms-min'] = old_div(sum(complete_results), summarized_results['num_complete_bees'])

    complete_results = [r['mean-bytes-per-conn'] for r in summarized_results['complete_bees']]
    summarized_results['mean-bytes-per-conn'] = old_div(sum(complete_results), summarized_results['num_complete_bees'])

    complete_results = [r['connect-ms-mean'] for r in summarized_results['complete_bees']]
    summarized_results['connect-ms-mean'] = old_div(sum(complete_results), summarized_results['num_complete_bees'])

    if summarized_results['num_complete_bees'] == 0:
        summarized_results['mean_response'] = "no bees are complete"
    else:
        summarized_results['mean_response'] = old_div(sum(complete_results), summarized_results['num_complete_bees'])

    complete_results = [r['connect-ms-mean'] for r in summarized_results['complete_bees']]
    if summarized_results['num_complete_bees'] == 0:
        summarized_results['mean_response'] = "no bees are complete"
    else:
        summarized_results['mean_response'] = old_div(sum(complete_results), summarized_results['num_complete_bees'])


    summarized_results['tpr_bounds'] = params[0]['tpr']
    summarized_results['rps_bounds'] = params[0]['rps']

    if summarized_results['tpr_bounds'] is not None:
        if summarized_results['mean_response'] < summarized_results['tpr_bounds']:
            summarized_results['performance_accepted'] = True
        else:
            summarized_results['performance_accepted'] = False

    if summarized_results['rps_bounds'] is not None:
        if summarized_results['mean_requests'] > summarized_results['rps_bounds'] and \
                summarized_results['performance_accepted'] is True or None:
            summarized_results['performance_accepted'] = True
        else:
            summarized_results['performance_accepted'] = False

    summarized_results['request_time_cdf'] = _get_request_time_cdf(summarized_results['total_complete_requests'],
                                                                   summarized_results['complete_bees'])
    if csv_filename:
        _create_request_time_cdf_csv(results, summarized_results['complete_bees_params'],
                                     summarized_results['request_time_cdf'], csv_filename)

    return summarized_results


def _hurl_print_results(summarized_results):
    """
    Print summarized load-testing results.
    :param summarized_results:
    :return:
    """
    if summarized_results['exception_bees']:
        print(
            "     {:d} of your bees didn't make it to the action. They might be taking a little longer than normal to"
            " find their machine guns, or may have been terminated without using \"bees down\".".format(
                summarized_results['num_exception_bees']))

    if summarized_results['timeout_bees']:
        print("     Target timed out without fully responding to %i bees." % summarized_results['num_timeout_bees'])

    if summarized_results['num_complete_bees'] == 0:
        print("     No bees completed the mission. Apparently your bees are peace-loving hippies.")
        return
    print("\nSummarized Results")
    print("     Total bytes:\t\t%i" % summarized_results['total_bytes'])
    print("     Seconds:\t\t\t%i" % summarized_results['seconds'])
    print("     Connect-ms-max:\t\t%f" % summarized_results['connect-ms-max'])
    print("     1st-resp-ms-max:\t\t%f" % summarized_results['1st-resp-ms-max'])
    print("     1st-resp-ms-mean:\t\t%f" % summarized_results['1st-resp-ms-mean'])
    print("     Fetches/sec mean:\t\t%f" % summarized_results['fetches-per-sec'])
    print("     connect-ms-min:\t\t%f" % summarized_results['connect-ms-min'])
    print("     Total fetches:\t\t%i" % summarized_results['total-fetches'])
    print("     bytes/sec mean:\t\t%f" % summarized_results['bytes-per-second-mean'])
    print("     end2end-ms-min mean:\t%f" % summarized_results['end2end-ms-min'])
    print("     mean-bytes-per-conn:\t%f" % summarized_results['mean-bytes-per-conn'])
    print("     connect-ms-mean:\t\t%f" % summarized_results['connect-ms-mean'])
    print("\nResponse Codes:")

    print("     2xx:\t\t\t%i" % summarized_results['total_number_of_200s'])
    print("     3xx:\t\t\t%i" % summarized_results['total_number_of_300s'])
    print("     4xx:\t\t\t%i" % summarized_results['total_number_of_400s'])
    print("     5xx:\t\t\t%i" % summarized_results['total_number_of_500s'])
    print()

    if 'rps_bounds' in summarized_results and summarized_results['rps_bounds'] is not None:
        print("     Requests per second:\t%f [#/sec] (upper bounds)" % summarized_results['rps_bounds'])

    if 'tpr_bounds' in summarized_results and summarized_results['tpr_bounds'] is not None:
        print("     Time per request:\t\t%f [ms] (lower bounds)" % summarized_results['tpr_bounds'])

    if 'performance_accepted' in summarized_results:
        print("     Performance check:\t\t%s" % summarized_results['performance_accepted'])

    if summarized_results['mean_response'] < 500:
        print("Mission Assessment: Target crushed bee offensive.")
    elif summarized_results['mean_response'] < 1000:
        print("Mission Assessment: Target successfully fended off the swarm.")
    elif summarized_results['mean_response'] < 1500:
        print("Mission Assessment: Target wounded, but operational.")
    elif summarized_results['mean_response'] < 2000:
        print("Mission Assessment: Target severely compromised.")
    else:
        print("Mission Assessment: Swarm annihilated target.")


def _get_new_state_file_name(zone):
    """
    Take zone and return multi regional bee file,
    from ~/.bees to ~/.bees.${region}
    :param zone:
    :return:
    """
    return '{}.{}'.format(STATE_FILENAME, zone)


def _get_existing_regions():
    """
    return a list of zone name strings from looking at
    existing region ~/.bees.* files
    :return:
    """
    existing_regions = []
    possible_files = os.listdir(os.path.expanduser('~'))
    for f in possible_files:
        something = re.search(r'\.bees2\.(.*)', f)
        existing_regions.append(something.group(1)) if something else "no"
    return existing_regions
