#    Copyright 2015 Mirantis, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import logging
import os

import pytest
from six.moves import configparser
from waiting import wait

from mos_tests.environment.devops_client import DevopsClient
from mos_tests.environment.os_actions import OpenStackActions
from mos_tests.environment.fuel_client import FuelClient
from mos_tests.settings import KEYSTONE_PASS
from mos_tests.settings import KEYSTONE_USER
from mos_tests.settings import SERVER_ADDRESS
from mos_tests.settings import SSH_CREDENTIALS

logger = logging.getLogger(__name__)


def pytest_addoption(parser):
    parser.addoption("--fuel-ip", '-I', action="store",
                     help="Fuel master server ip address")
    parser.addoption("--env", '-E', action="store",
                     help="Fuel devops env name")
    parser.addoption("--snapshot", '-S', action="store",
                     help="Fuel devops snapshot name")


def pytest_configure(config):
    # register an additional marker
    config.addinivalue_line("markers",
        "check_env_(check1, check2): mark test to run only on env, which pass "
        "all checks")
    config.addinivalue_line("markers",
        "need_devops: mark test wich need devops to run")
    config.addinivalue_line("markers",
        "neeed_tshark: mark test wich need tshark to be installed to run")
    config.addinivalue_line("markers",
        "undestructive: mark test wich has teardown")
    config.addinivalue_line("markers",
        "testrail_id(id, params={'name': value,...}): add suffix to "
        "test name. If defined, `params` apply case_id only if it "
        "matches test params.")


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    # execute all other hooks to obtain the report object
    outcome = yield
    rep = outcome.get_result()

    # set an report attribute for each phase of a call, which can
    # be "setup", "call", "teardown"
    setattr(item, "rep_" + rep.when, rep)


def pytest_runtest_teardown(item, nextitem):
    setattr(item, "nextitem", nextitem)


@pytest.fixture(scope="session")
def env_name(request):
    return request.config.getoption("--env")


@pytest.fixture(scope="session")
def snapshot_name(request):
    return request.config.getoption("--snapshot")


def revert_snapshot(env_name, snapshot_name):
    DevopsClient.revert_snapshot(env_name=env_name,
                                 snapshot_name=snapshot_name)


@pytest.fixture(scope="session", autouse=True)
def setup_session(env_name, snapshot_name):
    """Revert Fuel devops snapshot before test session"""
    if not all([env_name, snapshot_name]):
        return
    revert_snapshot(env_name, snapshot_name)


@pytest.yield_fixture(autouse=True)
def cleanup(request, env_name, snapshot_name):
    yield
    if request.config.option.exitfirst:
        return
    item = request.node
    if item.nextitem is None:
        return
    test_results = [getattr(item, 'rep_{}'.format(name), None)
                    for name in ("setup", "call", "teardown")]
    failed = any(x for x in test_results if x is not None and x.failed)
    skipped = any(x for x in test_results if x is not None and x.skipped)
    destructive = 'undestructive' not in item.keywords
    reverted = False
    if failed or (not skipped and destructive):
        revert_snapshot(env_name, snapshot_name)
        reverted = True
    setattr(item.nextitem, 'reverted', reverted)


@pytest.fixture(scope="session")
def fuel_master_ip(request, env_name, snapshot_name):
    """Get fuel master ip"""
    fuel_ip = request.config.getoption("--fuel-ip")
    if not fuel_ip:
        fuel_ip = DevopsClient.get_admin_node_ip(env_name=env_name)
    if not fuel_ip:
        fuel_ip = SERVER_ADDRESS
    return fuel_ip


def get_fuel_client(fuel_ip):
    return FuelClient(ip=fuel_ip,
                      login=KEYSTONE_USER,
                      password=KEYSTONE_PASS,
                      ssh_login=SSH_CREDENTIALS['login'],
                      ssh_password=SSH_CREDENTIALS['password'])


@pytest.fixture
def fuel(fuel_master_ip):
    """Initialized fuel client"""
    return get_fuel_client(fuel_master_ip)


@pytest.fixture
def env(request, fuel):
    """Environment instance"""
    env = fuel.get_last_created_cluster()
    if getattr(request.node, 'reverted', True):
        env.wait_for_ostf_pass()
    return env


@pytest.fixture(scope="session")
def set_openstack_environ(fuel_master_ip):
    fuel = get_fuel_client(fuel_master_ip)
    env = fuel.get_last_created_cluster()
    """Set os.environ variables from openrc file"""
    logger.info("read OpenStack openrc file")
    controllers = env.get_nodes_by_role('controller')[0]
    with controllers.ssh() as remote:
        result = remote.check_call('env -0')
        before_vars = set(result['stdout'][-1].strip().split('\x00'))
        result = remote.check_call('. openrc && env -0')
        after_vars = set(result['stdout'][-1].strip().split('\x00'))
        for os_var in after_vars - before_vars:
            k, v = os_var.split('=', 1)
            if v == 'internalURL':
                v = 'publicURL'
            os.environ[k] = v


@pytest.fixture
def os_conn(env):
    """Openstack common actions"""
    os_conn = OpenStackActions(
        controller_ip=env.get_primary_controller_ip(),
        cert=env.certificate, env=env)

    wait(os_conn.is_nova_ready,
         timeout_seconds=60 * 5,
         expected_exceptions=Exception,
         waiting_for="OpenStack nova computes is ready")
    logger.info("OpenStack is ready")
    return os_conn


@pytest.fixture
def clean_os(os_conn):
    """Cleanup OpenStack"""
    os_conn.cleanup_network()


def is_ha(env):
    """Env deployed with HA (3 controllers)"""
    return env.is_ha and len(env.get_nodes_by_role('controller')) >= 3


def has_1_or_more_computes(env):
    """Env deployed with 1 or more computes"""
    return len(env.get_nodes_by_role('compute')) >= 1


def has_2_or_more_computes(env):
    """Env deployed with 2 or more computes"""
    return len(env.get_nodes_by_role('compute')) >= 2


def has_3_or_more_computes(env):
    """Env deployed with 3 or more computes"""
    return len(env.get_nodes_by_role('compute')) >= 3


def is_vlan(env):
    """Env deployed with vlan segmentation"""
    return env.network_segmentation_type == 'vlan'


def is_vxlan(env):
    """Env deployed with vxlan segmentation"""
    return env.network_segmentation_type == 'tun'


def get_config_option(fp, key, res_type):
    """Find and return value for key in INI-like file"""
    parser = configparser.RawConfigParser()
    parser.readfp(fp)
    if res_type is bool:
        getter = parser.getboolean
    else:
        getter = parser.get
    for section in parser.sections():
        if parser.has_option(section, key):
            return getter(section, key)


def is_l2pop(env):
    """Env deployed with vxlan segmentation and l2 population"""
    controller = env.get_nodes_by_role('controller')[0]
    with env.get_ssh_to_node(controller.data['ip']) as remote:
        with remote.open('/etc/neutron/plugin.ini') as f:
            return get_config_option(f, 'l2_population', bool) is True


def is_dvr(env):
    """Env deployed with enabled distributed routers support"""
    controller = env.get_nodes_by_role('controller')[0]
    with env.get_ssh_to_node(controller.data['ip']) as remote:
        with remote.open('/etc/neutron/plugin.ini') as f:
            return get_config_option(
                f, 'enable_distributed_routing', bool) is True


def is_l3_ha(env):
    """Env deployed with enabled distributed routers support"""
    controller = env.get_nodes_by_role('controller')[0]
    with env.get_ssh_to_node(controller.data['ip']) as remote:
        with remote.open('/etc/neutron/neutron.conf') as f:
            return get_config_option(f, 'l3_ha', bool) is True


@pytest.fixture(autouse=True)
def env_requirements(request, env):
    marker = request.node.get_marker('check_env_')
    if marker:
        for func_name in marker.args:
            func = globals().get(func_name)
            if func is not None and not func(env):
                doc = func.__doc__ or 'Env {}'.format(
                    func_name.replace('_', ' '))
                pytest.skip('Requires: {}'.format(doc))


@pytest.fixture(autouse=True)
def testrail_id(request):
    """Add suffix_(<testrail_id>) mark to testcases"""
    node = request.node
    markers = node.get_marker('testrail_id') or []
    for marker in markers:
        test_id = marker.args[0]
        params = marker.kwargs.get('params', {})
        if len(params) > 0 and node.callspec.params != params:
            continue
        suffix_string = '({})'.format(test_id)
        node.add_marker('suffixes_{}'.format(suffix_string))
        break


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_logreport(report):
    """Collect suffix_ prefixed marks and add it to testid in report"""
    suffixes = [x.lstrip('suffixes_') for x in report.keywords.keys()
                if x.startswith('suffixes_')]
    if len(suffixes) > 0:
        report.nodeid += ''.join(suffixes)
    yield