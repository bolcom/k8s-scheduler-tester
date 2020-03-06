#!/usr/bin/env python
import logging
import os
import time
import warnings

import click
import requests
from kubernetes import client, config, watch
from prometheus_client import (Counter, Gauge, Histogram, Info,
                               start_http_server)
from prometheus_client.utils import INF

VERSION = '1.1.0'

DEFAULT_REPLICAS = 3
DEFAULT_TIMEOUT = 30
DEFAULT_INTERVAL = 30
DEFAULT_IMAGE = 'gcr.io/projectcalico-org/node:v3.2.7'
DEFAULT_ARGS = 'sleep,999'
DEFAULT_KEEP = 1
DEFAULT_PROM_PORT = 9999
DEFAULT_CPU_LIMIT = '100m'
DEFAULT_MEM_LIMIT = '50Mi'


logging.basicConfig(
    format='%(asctime)s - %(name)s/%(threadName)s - %(levelname)s %(message)s')
log = logging.getLogger('main')

prom_config_info = Info('k8s_scheduler_tester_config',
                        'Tester configuration')
prom_time_to_deployment_ready = Histogram('k8s_scheduler_tester_time_to_ready_deployment',
                                          'Time between create deployment and all replicas in ready state',
                                          buckets=(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 15, 25, INF))
prom_deployment_timeouts = Counter('k8s_scheduler_tester_timeouts',
                                   'Tests time out when a deployment did not get all its pods in ready state within X seconds')


def configure_kubernetes_client(context, debug):
    try:
        config.load_incluster_config()
        log.info('Configured in-cluster Kubernetes client')
    except config.config_exception.ConfigException:
        if not context:
            log.error(
                'No in-cluster Kubernetes client possible. And no context specified. Specify context and retry')
            return False
        try:
            try:
                config.load_kube_config(
                    os.path.join(os.environ["HOME"], '.kube/config'),
                    context)
                log.info(
                    f'Configured Kubernetes client for context: {context}')
            except config.config_exception.ConfigException:
                log.error(
                    f'No kubeconfig present for context: {context}. Verify and retry.')
                return False
        except FileNotFoundError:
            log.error(
                'Can not create Kubernetes client config: no in-cluster config nor $HOME/.kube/config file found')
            return False

    if debug:
        c = client.Configuration()
        c.debug = True
        log.debug('Enabling DEBUG on Kubernetes client')
        client.Configuration.set_default(c)

    # ping kubernetes
    try:
        client.VersionApi().get_code()
    except Exception as e:
        log.error(f'Unable to ping Kubernetes cluster: {e}')
        return False

    return True


def single_test(namespace, image, args, replicas, timeout, keep, cpu_limit, memory_limit):
    deployment_name = f'scheduletest-{time.time():.0f}'

    log.info(
        f'Deploying {deployment_name}. Will timeout after {timeout}s. Will keep for {keep}s.')

    labels = {
        'test': deployment_name,
        'app': 'scheduletester',
    }

    match_expressions = []
    for label_key, label_value in labels.items():
        match_expressions.append(client.V1LabelSelectorRequirement(
            key=label_key, operator='In', values=[label_value]))

    label_selector = ','.join(f'{k}={v}' for k, v in labels.items())

    resources_limits = {
        'cpu': cpu_limit,
        'memory': memory_limit,
    }

    # new deployment
    deployment = client.V1Deployment(
        metadata=client.V1ObjectMeta(
            name=deployment_name,
            namespace=namespace,
            labels=labels,
        ),
        spec=client.V1DeploymentSpec(
            replicas=replicas,
            selector=client.V1LabelSelector(
                match_labels=labels
            ),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(
                    name=deployment_name,
                    labels=labels),
                spec=client.V1PodSpec(
                    service_account='default',
                    security_context=client.V1PodSecurityContext(
                        fs_group=1000,
                        run_as_user=1000,
                    ),
                    affinity=client.V1Affinity(
                        pod_anti_affinity=client.V1PodAntiAffinity(
                            required_during_scheduling_ignored_during_execution=[
                                client.V1PodAffinityTerm(
                                    label_selector=client.V1LabelSelector(
                                        match_expressions=match_expressions),
                                    topology_key='kubernetes.io/hostname')
                            ])),
                    termination_grace_period_seconds=1,
                    containers=[
                        client.V1Container(
                            name='canary',
                            image=image,
                            args=args,
                            resources=client.V1ResourceRequirements(
                                limits=resources_limits,
                                requests=resources_limits,
                            ),
                        )
                    ]
                ))))

    start_time = time.time()
    response = client.AppsV1Api().create_namespaced_deployment(
        namespace, body=deployment)

    duration = 0
    ready_replicas = 0
    w = watch.Watch()
    for event in w.stream(client.AppsV1Api().list_namespaced_deployment,
                          namespace, label_selector=label_selector, timeout_seconds=timeout):
        log.debug(f'Received event: {event["type"]}')
        event_ready_replicas = event['object'].status.ready_replicas
        if event_ready_replicas:
            ready_replicas = event_ready_replicas
        log.debug(f'{ready_replicas} of {replicas} pods are available')
        if ready_replicas == replicas:
            duration = time.time() - start_time
            w.stop()

    if duration > 0:
        prom_time_to_deployment_ready.observe(duration)
        log.info(f'All {replicas} pods ready after {duration:.2f}s')
        time.sleep(keep)
    else:
        prom_deployment_timeouts.inc()
        log.warning(f'Timeout: only {ready_replicas} of {replicas} pods ready')

    # cleaning up
    log.info('Deleting the deployment')
    client.AppsV1Api().delete_namespaced_deployment(deployment_name, namespace)


@click.command(context_settings={'help_option_names': ['-h', '--help']})
@click.pass_context
@click.option('-d', '--debug', is_flag=True, help='Enable DEBUG verbosity')
@click.option('-w', '--wirelog', is_flag=True, help='Enable Kubernetes DEBUG + wire logging')
@click.option('-t', '--timeout', type=int, default=DEFAULT_TIMEOUT,
              help=f'Deployment timeout (stops waiting for ready pods). Default: {DEFAULT_TIMEOUT}')
@click.option('--prometheus-port', type=int, default=DEFAULT_PROM_PORT,
              help=f'Prometheus HTTP listener port. Default: {DEFAULT_PROM_PORT}')
@click.option('--target-namespace', help='Target Kubernetes namespace to create deployment in',
              required=True)
@click.option('-i', '--interval', type=int, default=DEFAULT_INTERVAL,
              help=f'Metric collection interval in seconds. Default: {DEFAULT_INTERVAL}')
@click.option('--context', help='Kubernetes context to use (for local development')
@click.option('-s', '--single', is_flag=True, default=False, help='Do a single run and exit (for local development)')
@click.option('-r', '--replicas', type=int, help=f'Number of replicas to deploy. Default: {DEFAULT_REPLICAS}',
              default=DEFAULT_REPLICAS)
@click.option('--keep', type=int, help='Keep pods running for X seconds. Default: {DEFAULT_KEEP}', default=DEFAULT_KEEP)
@click.option('--image', help=f'Container image to deploy. Default: {DEFAULT_IMAGE}',
              required=True, default=DEFAULT_IMAGE)
@click.option('--args', help=f'Container arguments. Comma separated. Default: {DEFAULT_ARGS}',
              required=True, default=DEFAULT_ARGS)
@click.option('--cpu-limit', help=f'Container CPU limit (also used as requests). Comma separated. Default: {DEFAULT_CPU_LIMIT}',
              required=True, default=DEFAULT_CPU_LIMIT)
@click.option('--memory-limit', help=f'Container memory limit (also used as requests). Comma separated. Default: {DEFAULT_MEM_LIMIT}',
              required=True, default=DEFAULT_MEM_LIMIT)
def cli(ctx, debug, wirelog, timeout, prometheus_port, target_namespace, interval, context, single, replicas, keep, image, args, cpu_limit, memory_limit):
    if debug:
        log.setLevel(logging.DEBUG)
        log.debug('Debug logging enabled')
    else:
        log.setLevel(logging.INFO)
    warnings.filterwarnings('ignore', r'.*end user credentials.*', UserWarning)        

    log.info(f'Starting version {VERSION}')

    if not configure_kubernetes_client(context, wirelog):
        log.fatal('Unable to create Kubernetes client')
        return

    if single:
        log.info(
            'In SINGLE mode: the Prometheus listener will not be started and app will exit after 1st run')
    else:
        log.info(f'Starting Prometheus listener on port {prometheus_port}')
        prom_config_info.info({
            'replicas': str(replicas),
            'test_timout': str(timeout),
            'keep_running_sec': str(keep),
            'interval_sec': str(interval),
            'test_image': image,
            'test_args': args,
            'version': VERSION,
        })
        start_http_server(prometheus_port)

    container_args = args.split(',')
    log.debug(f'Using image \'{image}\' with arguments: {container_args}')

    # remove any leftover deployment
    leftover_deployments = client.AppsV1Api().list_namespaced_deployment(
        target_namespace, label_selector='app=scheduletester')
    for leftover_deployment in leftover_deployments.items:
        log.info(
            f'Cleaning up leftover deployment {leftover_deployment.metadata.name}')
        client.AppsV1Api().delete_namespaced_deployment(
            leftover_deployment.metadata.name,
            leftover_deployment.metadata.namespace)

    if single:
        single_test(target_namespace, image, container_args,
                    replicas, timeout, keep, cpu_limit, memory_limit)
        log.info('End')
        return

    log.info(f'Testing every {interval}s')
    while 1:
        start_time = time.time()
        single_test(target_namespace, image, container_args,
                    replicas, timeout, keep, cpu_limit, memory_limit)
        sleep = interval - (time.time() - start_time)
        if sleep > 0:
            time.sleep(sleep)

    # will never reach


if __name__ == '__main__':
    cli(auto_envvar_prefix='OPT')  # pylint: disable=all
