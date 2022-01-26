import json
import kopf
import os

from kubernetes import client
from jinja2 import Template
from dotmap import DotMap

NAMESPACE = os.environ.get('POD_NAMESPACE', 'riasc-system')

PTP4L_TEMPLATE = Template('''
[global]
    slaveOnly {{ '1' if ptp.slaveOnly else '0' }}

    network_transport {{ ptp.transport }}

    verbose {{ '1' if ptp.verbose else '0' }}
    logging_level {{ ptp.loggingLevel }}

    time_stamping {{ ptp.timestamping }}

    [{{ ptp.interface }}]
    # empty

    {{ ptp.extraConfig }}
''')

CHRONY_TEMPLATE = Template('''
{% macro chrony_server(server) %}
{{ server.type }} {{ server.address }} iburst minpoll {{ server.minPoll | default(4) }} maxpoll {{ server.maxPoll | default(4) }}{% if server.prefer %} prefer{% endif %}
{% endmacro %}
user root
pidfile /run/chronyd.pid

# Stored on hostpath volume
driftfile /var/lib/chrony/drift

rtcsync
makestep 1.0 3
hwtimestamp *

# Logging
log rawmeasurements measurements statistics tracking refclocks tempcomp
logdir /var/log/chrony
logbanner 0
logchange 0.1

{% if ntp.server.local %}
local stratum {{ ntp.server.stratum }}{% if ntp.server.orphan %} orphan{% endif %}
{% endif %}

{%- if ntp.server.enabled %}
{% for item in ntp.server.allow %}
allow {{ item }}
{%- endfor %}
{%- for item in ntp.server.deny %}
deny {{ item }}
{%- endfor %}
{%- endif %}

{%- if gps.enabled %}
# https://chrony.tuxfamily.org/documentation.html
# https://gpsd.gitlab.io/gpsd/gpsd-time-service-howto.html#_feeding_chrony_from_gpsd
# gspd is looking for /var/run/chrony.pps0.sock
refclock SOCK /run/chrony.{{ gps.device }}.sock refid GPS precision 1e-1 offset 0.9999
refclock SOCK /run/chrony.pps0.sock refid PPS0 precision 1e-7 lock GPS
{%- endif %}
{%- if pps.enabled %}
refclock PPS /dev/{{ pps.device }} refid PPS1 precision 1e-7 lock GPS
{%- endif %}
{%- if ptp.enabled %}
refclock PHC /dev/{{ ptp.device }} refid PTP1
{%- endif %}

## NTP servers/peers/pools
{%- for server in (ntp.servers + ntp_default_servers) %}
{{- chrony_server(server) }}
{%- endfor %}

{{ chrony.extraConfig }}
''')  # noqa: E501

NTP_DEFAULT_SERVERS = [
  {
    'type': 'pool',
    'address': 'europe.pool.ntp.org'
  },
  {
    'type': 'server',
    'address': 'ptbtime1.ptb.de'
  },
  {
    'type': 'server',
    'address': 'ptbtime2.ptb.de'
  },
  {
    'type': 'server',
    'address': 'ptbtime3.ptb.de'
  },
  {
    'type': 'server',
    'address': 'ntp1.oma.be'
  },
  {
    'type': 'server',
    'address': 'ntp2.oma.be'
  }
]


@kopf.on.create('riasc.eu', 'v1', 'timesyncconfigs')
def create_time_sync(logger: kopf.Logger, name: str, spec: kopf.Spec, **_):
    api = client.CoreV1Api()
    apps_api = client.AppsV1Api()

    spec = DotMap(dict(spec))

    labels = {
        'app.kubernetes.io/name': 'time-sync',
        'app.kubernetes.io/instance': name,
        'app.kubernetes.io/managed-by': 'riasc-operator'
    }

    cm = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(
            name=f'time-sync-{name}',
            labels=labels
        ),
        data={
            'config.json': json.dumps(spec.toDict()),
            'chrony.conf': CHRONY_TEMPLATE.render(**spec.toDict(), ntp_default_servers=NTP_DEFAULT_SERVERS)
        }
    )

    if spec.ptp.enabled:
        cm.data['ptp4l.conf'] = PTP4L_TEMPLATE.render(**spec.toDict())

    containers: list[client.V1Container] = [
        client.V1Container(
            name='chrony',
            image='erigrid/time-sync',
            image_pull_policy='Always',
            command=[
                '/bin/sh'
            ],
            args=[
                '-c',
                'rm -f /run/chronyd.pid; chronyd -dd -f /etc/chrony.conf'
            ],
            security_context=client.V1SecurityContext(
                privileged=True,
                # capabilities=client.V1Capabilities(
                #     add=['SYS_TIME']
                # )
            ),
            volume_mounts=[
                client.V1VolumeMount(
                    name='dev',
                    mount_path='/dev/'
                ),
                client.V1VolumeMount(
                    name='config',
                    mount_path='/etc/chrony.conf',
                    sub_path='chrony.conf',
                    read_only=True
                ),
                client.V1VolumeMount(
                    name='run',
                    mount_path='/run/'
                ),
                client.V1VolumeMount(
                    name='var-chrony',
                    mount_path='/var/lib/chrony'
                ),
            ]
        ),
        client.V1Container(
            name='status',
            image='erigrid/riasc-operator',
            image_pull_policy='Always',
            command=[
                'time-sync-status'
            ],
            env=[
                client.V1EnvVar(
                    name='NODE_NAME',
                    value_from=client.V1EnvVarSource(
                        field_ref=client.V1ObjectFieldSelector(
                            field_path='spec.nodeName'
                        )
                    )
                )
            ],
            volume_mounts=[
                client.V1VolumeMount(
                    name='config',
                    mount_path='/config.json',
                    sub_path='config.json',
                    read_only=True
                ),
                client.V1VolumeMount(
                    name='run',
                    mount_path='/run/'
                ),
                client.V1VolumeMount(
                    name='var-chrony',
                    mount_path='/var/lib/chrony'
                ),
            ]
        )
    ]

    if spec.gps.enabled:
        containers.append(client.V1Container(
            name='gpsd',
            image='erigrid/time-sync',
            image_pull_policy='Always',
            security_context=client.V1SecurityContext(
                privileged=True,
                # capabilities=client.V1Capabilities(
                #     add=['SYS_NICE']
                # )
            ),
            command=[
                '/bin/sh'
            ],
            args=[
                '-c',
                'while [ ! -S /run/chrony.$(GPS_DEVICE).sock ]; do sleep 1; done; sleep 1; gpsd -n -N -D1 /dev/$(GPS_DEVICE)'
            ],
            env=[
                client.V1EnvVar(
                    name='GPS_DEVICE',
                    value=spec.gps.device
                )
            ],
            volume_mounts=[
                client.V1VolumeMount(
                    name='dev',
                    mount_path='/dev/'
                ),
                client.V1VolumeMount(
                    name='run',
                    mount_path='/run/'
                )
            ]
        ))

    if spec.ptp.enabled:
        containers.append(client.V1Container(
            name='ptp4l',
            image='erigrid/time-sync',
            image_pull_policy='Always',
            security_context=client.V1SecurityContext(
                privileged=True,
            ),
            command=[
                'ptp4l'
            ],
            args=[
                '-f', '/etc/ptp4l.conf'
            ],
            env=[
                client.V1EnvVar(
                    name='PTP_INTERFACE',
                    value=spec.ptp.interface
                )
            ],
            volume_mounts=[
                client.V1VolumeMount(
                    name='dev',
                    mount_path='/dev/'
                ),
                client.V1VolumeMount(
                    name='run',
                    mount_path='/run/'
                ),
                client.V1VolumeMount(
                    name='config',
                    read_only=True,
                    sub_path='ptp4l.conf'
                )
            ]
        ))

    ds = client.V1DaemonSet(
        metadata=client.V1ObjectMeta(
            name=f'time-sync-{name}',
            labels=labels
        ),
        spec=client.V1DaemonSetSpec(
            selector=client.V1LabelSelector(
                match_labels=labels
            ),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(
                    labels=labels
                ),
                spec=client.V1PodSpec(
                    node_selector=spec.nodeSelector,
                    service_account='time-sync',
                    host_network=True,  # for PTP
                    containers=containers,
                    volumes=[
                        client.V1Volume(
                            name='dev',
                            host_path=client.V1HostPathVolumeSource(
                                path='/dev'
                            )
                        ),
                        client.V1Volume(
                            name='config',
                            config_map=client.V1ConfigMapVolumeSource(
                                name=cm.metadata.name
                            )
                        ),
                        client.V1Volume(
                            name='run',
                            empty_dir=client.V1EmptyDirVolumeSource()
                        ),
                        client.V1Volume(
                            name='var-chrony',
                            host_path=client.V1HostPathVolumeSource(
                                path='/var/lib/chrony',
                                type='DirectoryOrCreate'
                            )
                        )
                    ]
                )
            )
        ))

    kopf.adopt(cm)
    kopf.adopt(ds)

    cm = api.create_namespaced_config_map(NAMESPACE, cm)
    logger.info('ConfigMap is created: %s', cm.metadata.name)

    ds = apps_api.create_namespaced_daemon_set(NAMESPACE, ds)
    logger.info('DaemonSet is created: %s', cm.metadata.name)
