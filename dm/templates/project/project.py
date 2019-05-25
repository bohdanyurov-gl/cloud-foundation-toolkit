# Copyright 2018 Google Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
This template creates a single project with the specified service
accounts and APIs enabled.
"""
import copy


def generate_config(context):
    """ Entry point for the deployment resources. """

    properties = context.properties
    project_name = properties.get('name', context.env['name'])
    project_id = properties.get('projectId', project_name)

    # Ensure that the parent ID is a string.
    properties['parent']['id'] = str(properties['parent']['id'])

    resources = [
        {
            'name': 'project',
            # https://cloud.google.com/resource-manager/reference/rest/v1/projects/create
            'type': 'gcp-types/cloudresourcemanager-v1:projects',
            'properties':
                {
                    'name': project_name,
                    'projectId': project_id,
                    'parent': properties['parent'],
                    'labels' : properties.get('labels', {})
                }
        },
        {
            'name': 'billing',
            # https://cloud.google.com/billing/reference/rest/v1/projects/updateBillingInfo
            'type': 'deploymentmanager.v2.virtual.projectBillingInfo',
            'properties':
                {
                    'name':
                        'projects/$(ref.project.projectId)',
                    'billingAccountName':
                        'billingAccounts/' +
                        properties['billingAccountId']
                }
        }
    ]

    api_resources, api_names_list = activate_apis(properties)
    resources.extend(api_resources)
    resources.extend(create_service_accounts(context, project_id))

    if isinstance(properties.get('usageExportBucket', True), bool):
        properties['usageExportBucket'] = {
            'enabled': properties.get('usageExportBucket', True),
        }
    if (
        properties.get('usageExportBucket', {}).get('enabled', True) and
        'api-compute.googleapis.com' in api_names_list
    ):
        resources.extend(create_bucket(properties))

    resources.extend(create_shared_vpc(project_id, properties))

    if (
        properties.get('removeDefaultVPC', True) and
        'api-compute.googleapis.com' in api_names_list
    ):
        resources.extend(delete_default_network(api_names_list))

    if (
        properties.get('removeDefaultSA', True) and
        'api-compute.googleapis.com' in api_names_list
    ):
        resources.extend(delete_default_service_account(api_names_list))

    return {
        'resources':
            resources,
        'outputs':
            [
                {
                    'name': 'projectId',
                    'value': '$(ref.project.projectId)'
                },
                {
                    'name': 'usageExportBucketName',
                    'value': '$(ref.project.projectId)-usage-export'
                },
                {
                    'name':
                        'serviceAccountDisplayName',
                    'value':
                        '$(ref.project.projectNumber)@cloudservices.gserviceaccount.com'  # pylint: disable=line-too-long
                },
                {
                    'name':
                        'resources',
                    'value':
                        [resource['name'] for resource in resources]
                }
            ]
    }


def activate_apis(properties):
    """ Resources for API activation. """

    concurrent_api_activation = properties.get('concurrentApiActivation')
    apis = properties.get('activateApis', [])

    if 'storage-component.googleapis.com' not in apis:
        if (
            # Enable the storage-component API if the usage export bucket is enabled.
            properties.get('usageExportBucket')
        ):
            apis.append('storage-component.googleapis.com')

    if 'compute.googleapis.com' not in apis:
        if (
            properties.get('sharedVPCHost') or
            properties.get('sharedVPC') or
            properties.get('sharedVPCSubnets')
        ):
            apis.append('compute.googleapis.com')

    resources = []
    api_names_list = ['billing']
    for api in apis:
        depends_on = ['billing']
        # Serialize activation of all APIs by making apis[n]
        # depend on apis[n-1].
        if resources and not concurrent_api_activation:
            depends_on.append(resources[-1]['name'])

        api_name = 'api-{}'.format(api)
        api_names_list.append(api_name)
        resources.append(
            {
                'name': api_name,
                # https://cloud.google.com/service-infrastructure/docs/service-management/reference/rest/v1/services/enable
                'type': 'gcp-types/servicemanagement-v1:servicemanagement.services.enable',
                'metadata': {
                    'dependsOn': depends_on
                },
                'properties':
                    {
                        'consumerId': 'project:$(ref.project.projectId)',
                        'serviceName': api
                    }
            }
        )

    # Return the API resources to enable other resources to use them as
    # dependencies, to ensure that they are created first. For example,
    # the default VPC or service account.
    return resources, api_names_list


def create_project_iam(dependencies, role_member_list):
    """ Grant the shared project IAM permissions. """

    resources = [
        {
            # Get the IAM policy first, so as not to remove
            # any existing bindings.
            'name': 'project-iam-policy',
            'type': 'cft-iam_project_member.py',
            'properties': {
                'projectId': '$(ref.project.projectId)',
                'roles': role_member_list
            },
            'metadata':
                {
                    'dependsOn': dependencies,
                    'runtimePolicy': ['UPDATE_ALWAYS']
                }
        }
    ]

    return resources


def create_shared_vpc_subnet_iam(context, dependencies, members_list):
    """ Grant the shared VPC subnet IAM permissions to Service Accounts. """

    resources = []

    # Grant the Service Accounts access to the shared VPC subnets.
    # Note that, until there is a subnetwork IAM patch support,
    # setIamPolicy will overwrite any existing policies on the subnet.
    for i, subnet in enumerate(
            context.properties.get('sharedVPCSubnets'), 1
        ):
        resources.append(
            {
                'name': 'add-vpc-subnet-iam-policy-{}'.format(i),
                # https://cloud.google.com/compute/docs/reference/rest/v1/subnetworks/setIamPolicy
                'type': 'gcp-types/compute-v1:compute.subnetworks.setIamPolicy',  # pylint: disable=line-too-long
                'metadata':
                    {
                        'dependsOn': dependencies,
                    },
                'properties':
                    {
                        'name': subnet['subnetId'],
                        'project': context.properties['sharedVPC'],
                        'region': subnet['region'],
                        'policy' : {
                            'bindings': [
                                {
                                    'role': 'roles/compute.networkUser',
                                    'members': members_list,
                                }
                            ],
                        },
                    }
            }
        )

    return resources


def create_service_accounts(context, project_id):
    """ Create Service Accounts and grant project IAM permissions. """

    resources = []
    network_list = ['serviceAccount:$(ref.project.projectNumber)@cloudservices.gserviceaccount.com'] # pylint: disable=line-too-long
    service_account_dep = ["api-compute.googleapis.com"]
    policies_to_add = []

    for service_account in context.properties['serviceAccounts']:
        account_id = service_account['accountId']
        display_name = service_account.get('displayName', account_id)
        sa_name = 'serviceAccount:{}@{}.iam.gserviceaccount.com'.format(
            account_id,
            project_id
        )

        # Check if the member needs shared VPC permissions. Put in
        # a list to grant the shared VPC subnet IAM permissions.
        if service_account.get('networkAccess'):
            network_list.append(sa_name)

        # Build the service account bindings for the project IAM permissions.
        for role in service_account['roles']:
            policies_to_add.append({'role': role, 'members': [sa_name]})

        # Build a list of SA resources to be used as a dependency
        # for permission granting.
        name = 'service-account-' + account_id
        service_account_dep.append(name)

        # Create the service account resource.
        resources.append(
            {
                'name': name,
                # https://cloud.google.com/iam/reference/rest/v1/projects.serviceAccounts/create
                'type': 'gcp-types/iam-v1:projects.serviceAccounts',
                'properties':
                    {
                        'accountId': account_id,
                        'displayName': display_name,
                        'projectId': '$(ref.project.projectId)'
                    }
            }
        )

    # Build the group bindings for the project IAM permissions.
    for group in context.properties['groups']:
        group_name = 'group:{}'.format(group['name'])
        for role in group['roles']:
            policies_to_add.append({'role': role, 'members': [group_name]})

        # Check if the group needs shared VPC permissions. Put in
        # a list to grant the shared VPC subnet IAM permissions.
        if group.get('networkAccess'):
            network_list.append(group_name)

    # Create the project IAM permissions.
    if policies_to_add:
        iam = create_project_iam(service_account_dep, policies_to_add)
        resources.extend(iam)

    if (
        not context.properties.get('sharedVPCHost') and
        context.properties.get('sharedVPCSubnets') and
        context.properties.get('sharedVPC')
    ):
        # Create the shared VPC subnet IAM permissions.
        resources.extend(
            create_shared_vpc_subnet_iam(
                context,
                service_account_dep,
                network_list
            )
        )

    return resources


def create_bucket(properties):
    """ Resources for the usage export bucket. """

    resources = []

    bucket_name = '$(ref.project.projectId)-usage-export'

    # Create the bucket.
    resources.append({
        'name': 'create-usage-export-bucket',
        # https://cloud.google.com/storage/docs/json_api/v1/buckets/insert
        'type': 'gcp-types/storage-v1:buckets',
        'properties':
            {
                'project': '$(ref.project.projectId)',
                'name': bucket_name
            },
        'metadata':
            {
                'dependsOn': ['api-storage-component.googleapis.com']
            }
    })

    # Set the project's usage export bucket.
    usage_resource = {
        'name':
            'set-usage-export-bucket',
        'action':
        # https://cloud.google.com/compute/docs/reference/rest/v1/projects/setUsageExportBucket
            'gcp-types/compute-v1:compute.projects.setUsageExportBucket',  # pylint: disable=line-too-long
        'properties':
            {
                'project': '$(ref.project.projectId)',
                'bucketName': 'gs://' + bucket_name
            },
        'metadata': {
            'dependsOn': [
                'create-usage-export-bucket',
                'api-compute.googleapis.com',
            ]
        }
    }
    if properties.get('usageExportBucket', {}).get('reportNamePrefix'):
        usage_resource['properties']['reportNamePrefix'] = properties.get('usageExportBucket', {}).get('reportNamePrefix')
    resources.append(usage_resource)


    return resources


def create_shared_vpc(project_id, properties):
    """ Configure the project Shared VPC properties. """

    resources = []

    service_project = properties.get('sharedVPC')
    if service_project:
        resources.append(
            {
                'name': project_id + '-attach-xpn-service-' + service_project,
                # https://cloud.google.com/compute/docs/reference/rest/v1/projects/enableXpnResource
                'type': 'gcp-types/compute-v1:compute.projects.enableXpnResource',
                'metadata': {
                    'dependsOn': ['api-compute.googleapis.com']
                },
                'properties':
                    {
                        'project': service_project,
                        'xpnResource':
                            {
                                'id': '$(ref.project.projectId)',
                                'type': 'PROJECT',
                            }
                    }
            }
        )
    elif properties.get('sharedVPCHost'):
        resources.append(
            {
                'name': project_id + '-xpn-host',
                # https://cloud.google.com/compute/docs/reference/rest/v1/projects/enableXpnHost
                'type': 'gcp-types/compute-v1:compute.projects.enableXpnHost',
                'metadata': {
                    'dependsOn': ['api-compute.googleapis.com']
                },
                'properties': {
                    'project': '$(ref.project.projectId)'
                }
            }
        )

    return resources


def delete_default_network(api_names_list):
    """ Delete the default network. """

    default_firewalls = [
        'default-allow-icmp',
        'default-allow-internal',
        'default-allow-rdp',
        'default-allow-ssh',
    ]

    resources = []
    for firewall_name in default_firewalls:
        resources.append({
            'name': 'delete-{}'.format(firewall_name),
            # https://cloud.google.com/compute/docs/reference/rest/v1/firewalls/delete
            'action': 'gcp-types/compute-v1:compute.firewalls.delete',
            'metadata': {
                'dependsOn': api_names_list
            },
            'properties':
                {
                    'firewall': firewall_name,
                    'project': '$(ref.project.projectId)',
                }
        })

    # Ensure the firewall rules are removed before deleting the VPC.
    network_dependency = copy.copy(api_names_list)
    network_dependency.extend([row['name'] for row in resources])

    resources.append(
        {
            'name': 'delete-default-network',
            # https://cloud.google.com/compute/docs/reference/rest/v1/networks/delete
            'action': 'gcp-types/compute-v1:compute.networks.delete',
            'metadata': {
                'dependsOn': network_dependency
            },
            'properties':
                {
                    'network': 'default',
                    'project': '$(ref.project.projectId)'
                }
        }
    )

    return resources


def delete_default_service_account(api_names_list):
    """ Delete the default service account. """

    resource = [
        {
            'name': 'delete-default-sa',
            # https://cloud.google.com/iam/reference/rest/v1/projects.serviceAccounts/delete
            'action': 'gcp-types/iam-v1:iam.projects.serviceAccounts.delete',
            'metadata':
                {
                    'dependsOn': api_names_list,
                    'runtimePolicy': ['CREATE']
                },
            'properties':
                {
                    'name':
                        'projects/$(ref.project.projectId)/serviceAccounts/$(ref.project.projectNumber)-compute@developer.gserviceaccount.com'  # pylint: disable=line-too-long
                }
        }
    ]

    return resource
