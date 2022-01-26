import os
import kopf
from kubernetes import client

from riasc_operator.utils.labels import label_conflicts


def label_nodes(logger: kopf.Logger, nodes: list[str], key: str, value: str | None):
    api = client.CoreV1Api()

    for node in nodes:
        try:
            api.patch_node(node, body={
                'metadata': {
                    'labels': {
                        key: value
                    }}
            })
        except Exception as e:
            raise kopf.PermanentError('Failed to label node with project label: ' + str(e))
        finally:
            logger.info('Patched annotations of node %s', node)


def add_users(logger: kopf.Logger, namespace: str, users: list[str]):
    rbac_api = client.RbacAuthorizationV1Api()

    for user in users:
        rb = client.V1RoleBinding(
            metadata=client.V1ObjectMeta(
                name=f'admin-{user}',
                namespace=namespace
            ),
            role_ref=client.V1RoleRef(
                api_group='rbac.authorization.k8s.io',
                kind='ClusterRole',
                name='admin'
            ),
            subjects=[
                client.V1Subject(
                    api_group='rbac.authorization.k8s.io',
                    kind='User',
                    name=user
                )
            ]
        )

        kopf.adopt(rb)

        rb = rbac_api.create_namespaced_role_binding(namespace, rb)
        logger.info('RoleBinding is added: %s', rb.metadata.name)


def remove_users(logger: kopf.Logger, namespace: str, users: list[str]):
    rbac_api = client.RbacAuthorizationV1Api()

    for user in users:
        rbac_api.delete_namespaced_role_binding(f'admin-{user}', namespace)
        logger.info(f'RoleBinding is removed: admin-{user}')


@kopf.on.startup()
def config(settings: kopf.OperatorSettings, **_):
    env = os.environ.get('ENV', 'development')
    if env == 'production':
        settings.admission.server = kopf.WebhookServer(
            addr=os.environ.get('ADMISSION_ADDR'),
            certfile=os.environ.get('ADMISSION_CERTFILE'),
            pkeyfile=os.environ.get('ADMISSION_PKEYFILE')
        )
    elif env == 'development':
        settings.admission.server = kopf.WebhookMinikubeServer()
        settings.admission.managed = 'project.riasc.eu'


@kopf.index('riasc.eu', 'v1', 'projects')
def projects_index(name: str, spec: kopf.Spec, **_):
    return {name: spec}


@kopf.on.resume('riasc.eu', 'v1', 'projects')
@kopf.on.create('riasc.eu', 'v1', 'projects')
@kopf.on.update('riasc.eu', 'v1', 'projects')
def resume_project(logger: kopf.Logger, name: str, spec: kopf.Spec, **_):
    nodes = spec.get('nodes', [])
    label_nodes(logger, nodes, f'project.riasc.eu/{name}', '')


@kopf.on.delete('riasc.eu', 'v1', 'projects')
def delete_project(logger: kopf.Logger, name: str, spec: kopf.Spec, **_):
    nodes = spec.get('nodes', [])
    label_nodes(logger, nodes, f'project.riasc.eu/{name}', None)


@kopf.on.update('riasc.eu', 'v1', 'projects', field='spec.nodes')
def update_project_nodes(logger: kopf.Logger, name: str, old: list[str], new: list[str], **_):
    added = set(new or []) - set(old or [])
    removed = set(old or []) - set(new or [])

    logger.info('Handling changed nodes: added=%s, removed=%s', added, removed)

    label_nodes(logger, added, f'project.riasc.eu/{name}', '')
    label_nodes(logger, removed, f'project.riasc.eu/{name}', None)


@kopf.on.update('riasc.eu', 'v1', 'projects', field='spec.users')
def update_project_users(logger: kopf.Logger, name: str, old: list[str], new: list[str], **_):
    added = set(new or []) - set(old or [])
    removed = set(old or []) - set(new or [])

    logger.info('Handling changed users: added=%s, removed=%s', added, removed)

    add_users(logger, name, added)
    remove_users(logger, name, removed)


@kopf.on.create('riasc.eu', 'v1', 'projects')
def create_project(logger: kopf.Logger, name: str, spec: kopf.Spec, **_):
    api = client.CoreV1Api()

    ns = client.V1Namespace(
        metadata=client.V1ObjectMeta(
            name=name,
            labels={
                'riasc.eu/project': name
            }
        )
    )

    kopf.adopt(ns)

    ns = api.create_namespace(ns)
    logger.info('Namespace is created: %s', ns.metadata.name)

    users = spec.get('users', [])
    add_users(logger, name, users)


@kopf.on.mutate('v1', 'pod',
                persistent=True,
                side_effects=False,
                ignore_failures=True)
def validate_node_selector(projects_index: kopf.Index, spec: kopf.Spec, namespace: str, name: str, patch: dict, logger: kopf.Logger, **_):
    project_name = namespace
    projects = projects_index.get(project_name)
    if projects is None:
        logger.info('Ignoring pod %s/%s as it is not belong to a project', namespace, name)
        return

    project = projects[0]

    podNodeSelector = spec.get('nodeSelector', {})
    projectNodeSelector = project.get('nodeSelector', {})

    if project.get('nodes', []):
        projectNodeSelector[f'project.riasc.eu/{project_name}'] = ''

    if label_conflicts(projectNodeSelector, podNodeSelector):
        raise kopf.AdmissionError(f'Conflicting nodeSelector for project {project_name}')

    podNodeSelector.update(projectNodeSelector)

    patch['spec'] = {
        'nodeSelector': podNodeSelector
    }
