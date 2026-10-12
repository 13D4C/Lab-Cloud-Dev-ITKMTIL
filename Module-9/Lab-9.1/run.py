import base64
import concurrent.futures
import hashlib
import io
import ipaddress
import json
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile

from runtime import ROOT, TOOLS, aws_session, find_terraform, load_environment


def wait_for(description, operation, timeout=1200):
    deadline = time.monotonic() + timeout
    last_report = 0
    while True:
        result = operation()
        if result:
            return result
        if time.monotonic() > deadline:
            raise TimeoutError('Timed out waiting for ' + description + '. Run again after the lab is ready.')
        if time.monotonic() - last_report >= 30:
            print('Waiting for ' + description + '...', flush=True)
            last_report = time.monotonic()
        time.sleep(10)


def discover(session, environment):
    ec2 = session.client('ec2')
    candidates = []
    for page in ec2.get_paginator('describe_instances').paginate(Filters=[
        {'Name': 'instance-state-name', 'Values': ['running']}, {'Name': 'tag:Name', 'Values': ['Lab IDE']}
    ]):
        for reservation in page['Reservations']:
            for instance in reservation['Instances']:
                tags = {item['Key']: item['Value'] for item in instance.get('Tags', [])}
                if not environment.get('LAB_STACK_NAME') or tags.get('cloudlab') == environment['LAB_STACK_NAME']:
                    candidates.append(instance)
    if len(candidates) != 1:
        raise RuntimeError('Start Lab 9.1 and wait for Lab IDE. Set LAB_STACK_NAME if multiple labs are running.')
    ide = candidates[0]
    if len(ide['SecurityGroups']) != 1:
        raise RuntimeError('Expected one Lab IDE security group.')
    tags = {item['Key']: item['Value'] for item in ide.get('Tags', [])}
    stack_name = tags.get('cloudlab') or environment.get('LAB_STACK_NAME')
    cf = session.client('cloudformation')
    def stack_ready():
        stack = cf.describe_stacks(StackName=stack_name)['Stacks'][0]
        status = stack['StackStatus']
        if 'FAILED' in status or 'ROLLBACK' in status or status.startswith('DELETE'):
            raise RuntimeError('AWS Academy lab stack is ' + status + '. Restart the lab before running again.')
        return stack if status in ('CREATE_COMPLETE', 'UPDATE_COMPLETE') else None
    stack = wait_for('AWS Academy to finish preparing Lab 9.1', stack_ready)
    outputs = {item['OutputKey']: item['OutputValue'] for item in stack.get('Outputs', [])}
    if not outputs.get('LabIDEURL') or not outputs.get('LabIDEPassword'):
        raise RuntimeError('Lab IDE URL/password are missing from the lab stack outputs.')
    resources = [item for page in cf.get_paginator('list_stack_resources').paginate(StackName=stack_name)
                 for item in page['StackResourceSummaries']]
    buckets = [item['PhysicalResourceId'] for item in resources if item['ResourceType'] == 'AWS::S3::Bucket'
               and '-s3bucket-' in item['PhysicalResourceId']]
    api_ids = [item['PhysicalResourceId'] for item in resources if item['ResourceType'] == 'AWS::ApiGateway::RestApi']
    db_ids = [item['PhysicalResourceId'] for item in resources if item['ResourceType'] == 'AWS::RDS::DBCluster']
    if len(buckets) != 1 or len(api_ids) != 1 or db_ids != ['supplierdb']:
        raise RuntimeError('Expected the provided café bucket, ProductsApi and supplierdb from Lab 9.1.')
    cluster = session.client('rds').describe_db_clusters(DBClusterIdentifier='supplierdb')['DBClusters'][0]
    subnet_group = session.client('rds').describe_db_subnet_groups(DBSubnetGroupName=cluster['DBSubnetGroup'])['DBSubnetGroups'][0]
    if subnet_group['VpcId'] != ide['VpcId']:
        raise RuntimeError('The provided database must be in the Lab IDE VPC.')
    groups = [item['VpcSecurityGroupId'] for item in cluster['VpcSecurityGroups']]
    group_details = ec2.describe_security_groups(GroupIds=groups)['SecurityGroups']
    groups = [item['GroupId'] for item in group_details if 'ClusterSecurityGroup' in item['GroupName']]
    if len(groups) != 1:
        raise RuntimeError('Expected the Aurora ClusterSecurityGroup provided by this lab.')
    eb = session.client('elasticbeanstalk')
    def application_ready():
        values = eb.describe_environments(ApplicationName='MyNodeApp', EnvironmentNames=['MyEnv'], IncludeDeleted=False)['Environments']
        if len(values) != 1 or values[0]['Status'] in ('Terminated', 'Terminating'):
            raise RuntimeError('The provided MyNodeApp/MyEnv environment is missing.')
        return values[0] if values[0]['Status'] == 'Ready' else None
    application = wait_for('the provided Elastic Beanstalk environment', application_ready)
    settings = eb.describe_configuration_settings(ApplicationName='MyNodeApp', EnvironmentName='MyEnv')['ConfigurationSettings'][0]['OptionSettings']
    vpc = next((item['Value'] for item in settings if item['Namespace'] == 'aws:ec2:vpc' and item['OptionName'] == 'VPCId'), None)
    if vpc != ide['VpcId']:
        raise RuntimeError('MyEnv belongs to a different VPC.')
    repository = session.client('ecr').describe_repositories(repositoryNames=['cafe/node-web-app'])['repositories'][0]
    if environment.get('CLIENT_CIDR'):
        cidr = environment['CLIENT_CIDR']
    else:
        with urllib.request.urlopen('https://checkip.amazonaws.com', timeout=30) as response:
            cidr = response.read().decode().strip() + '/32'
    network = ipaddress.ip_network(cidr, strict=True)
    if network.version != 4 or network.prefixlen != 32:
        raise RuntimeError('CLIENT_CIDR must be your public IPv4 address followed by /32.')
    api = session.client('apigateway').get_rest_api(restApiId=api_ids[0])
    config = dict(account=session.client('sts').get_caller_identity()['Account'], stack_name=stack_name,
                  region=environment['TF_VAR_aws_region'], ide_instance_id=ide['InstanceId'],
                  cache_security_group_id=groups[0], ide_security_group_id=ide['SecurityGroups'][0]['GroupId'],
                  subnet_ids=sorted(item['SubnetIdentifier'] for item in subnet_group['Subnets']),
                  site_bucket=buckets[0], api_id=api['id'], api_root_id=api['rootResourceId'],
                  repository_url=repository['repositoryUri'], database_endpoint=cluster['Endpoint'],
                  application_url='http://' + application['CNAME'], environment_id=application['EnvironmentId'],
                  client_cidr=cidr)
    return config, outputs


def guard_state(config):
    TOOLS.mkdir(exist_ok=True)
    identity = {key: config[key] for key in ('account', 'stack_name', 'ide_instance_id')}
    path = TOOLS / 'lab.json'
    if path.exists() and json.loads(path.read_text()) != identity:
        archive = TOOLS / 'previous-labs' / str(time.time_ns())
        archive.mkdir(parents=True)
        for state in ROOT.glob('terraform.tfstate*'):
            shutil.move(str(state), archive / state.name)
        print('New lab detected; previous local state archived.', flush=True)
    if not path.exists() and (ROOT / 'terraform.tfstate').exists():
        raise RuntimeError('Existing Terraform state has no lab identity. Restore .tools/lab.json before continuing.')
    path.write_text(json.dumps(identity))


def bundle(config, action):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        archive.add(ROOT / 'deploy.py', arcname='.lab91/deploy.py')
        data = json.dumps(config).encode()
        entry = tarfile.TarInfo('.lab91/config.json')
        entry.mode = 0o600
        entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))
    payload = base64.b64encode(output.getvalue()).decode()
    return "set -euo pipefail\ncd /home/ec2-user/environment\nmkdir -p .lab91\nbase64 -d <<'LAB91_BUNDLE' | tar xz\n" + payload + "\nLAB91_BUNDLE\npython3 -u .lab91/deploy.py " + action


def imports(session, config, execute):
    resources = set(execute('state', 'list', capture=True).splitlines()) if (ROOT / 'terraform.tfstate').exists() else set()
    def adopt(address, identifier):
        if address not in resources:
            execute('import', '-no-color', address, identifier)
            resources.add(address)
    ec2 = session.client('ec2')
    rules = ec2.describe_security_group_rules(Filters=[{'Name': 'group-id', 'Values': [config['cache_security_group_id']]}])['SecurityGroupRules']
    for rule in rules:
        if not rule['IsEgress'] and rule.get('IpProtocol') == 'tcp' and rule.get('FromPort') == 11211 and rule.get('ToPort') == 11211:
            source = rule.get('ReferencedGroupInfo', {}).get('GroupId')
            if source == config['cache_security_group_id']:
                adopt('aws_vpc_security_group_ingress_rule.cache_self', rule['SecurityGroupRuleId'])
            elif source == config['ide_security_group_id']:
                adopt('aws_vpc_security_group_ingress_rule.cache_ide', rule['SecurityGroupRuleId'])
    cache = session.client('elasticache')
    for group in cache.describe_cache_subnet_groups()['CacheSubnetGroups']:
        if group['CacheSubnetGroupName'] == 'elasticachesubnetgroup':
            if group['VpcId'] != ec2.describe_security_groups(GroupIds=[config['cache_security_group_id']])['SecurityGroups'][0]['VpcId']:
                raise RuntimeError('Existing ElastiCacheSubnetGroup belongs to a different VPC.')
            adopt('aws_elasticache_subnet_group.cache', group['CacheSubnetGroupName'])
    for item in cache.describe_cache_clusters()['CacheClusters']:
        if item['CacheClusterId'] == 'memcachedcache':
            if item['CacheSubnetGroupName'] != 'elasticachesubnetgroup' or item['Engine'] != 'memcached':
                raise RuntimeError('Existing memcachedcache does not match this lab.')
            adopt('aws_elasticache_cluster.cache', item['CacheClusterId'])
    api = session.client('apigateway')
    for item in api.get_resources(restApiId=config['api_id'], limit=500)['items']:
        if item['path'] == '/bean_products':
            prefix = config['api_id'] + '/' + item['id']
            adopt('aws_api_gateway_resource.beans', prefix)
            for method, name in [('GET', 'beans'), ('OPTIONS', 'cors')]:
                if method in item.get('resourceMethods', {}):
                    adopt('aws_api_gateway_method.' + name, prefix + '/' + method)
                    details = api.get_method(restApiId=config['api_id'], resourceId=item['id'], httpMethod=method)
                    if details.get('methodIntegration'):
                        adopt('aws_api_gateway_integration.' + name, prefix + '/' + method)
                    if method == 'OPTIONS' and '200' in details.get('methodResponses', {}):
                        adopt('aws_api_gateway_method_response.cors', prefix + '/OPTIONS/200')
                    if method == 'OPTIONS' and '200' in details.get('methodIntegration', {}).get('integrationResponses', {}):
                        adopt('aws_api_gateway_integration_response.cors', prefix + '/OPTIONS/200')
    if any(item['stageName'] == 'prod' for item in api.get_stages(restApiId=config['api_id'])['item']):
        adopt('aws_api_gateway_stage.prod', config['api_id'] + '/prod')


def application_zip(image):
    document = {'AWSEBDockerrunVersion': '1', 'Image': {'Name': image, 'Update': 'true'},
                'Ports': [{'ContainerPort': 3000}]}
    with zipfile.ZipFile(TOOLS / 'application.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        entry = zipfile.ZipInfo('Dockerrun.aws.json', date_time=(2020, 1, 1, 0, 0, 0))
        archive.writestr(entry, json.dumps(document, sort_keys=True))


def update_application(session, config, values):
    eb = session.client('elasticbeanstalk')
    settings = eb.describe_configuration_settings(ApplicationName='MyNodeApp', EnvironmentName='MyEnv')['ConfigurationSettings'][0]['OptionSettings']
    current = {item['OptionName']: item.get('Value', '') for item in settings if item['Namespace'] == 'aws:elasticbeanstalk:application:environment'}
    desired = {'APP_DB_HOST': config['database_endpoint'], 'MEMC_HOST': values['cache_endpoint']}
    environment = eb.describe_environments(EnvironmentIds=[config['environment_id']])['Environments'][0]
    if environment.get('VersionLabel') != values['application_version'] or any(current.get(key) != value for key, value in desired.items()):
        print('Deploying the caching image and MEMC_HOST to the provided MyEnv...', flush=True)
        eb.update_environment(EnvironmentId=config['environment_id'], VersionLabel=values['application_version'], OptionSettings=[
            {'Namespace': 'aws:elasticbeanstalk:application:environment', 'OptionName': key, 'Value': value}
            for key, value in desired.items()
        ])
    def ready():
        environment = eb.describe_environments(EnvironmentIds=[config['environment_id']])['Environments'][0]
        if environment['Status'] in ('Terminating', 'Terminated'):
            raise RuntimeError('Elastic Beanstalk environment was terminated during deployment.')
        return environment if environment['Status'] == 'Ready' and environment.get('VersionLabel') == values['application_version'] else None
    wait_for('Elastic Beanstalk to deploy the caching application', ready)
    settings = eb.describe_configuration_settings(ApplicationName='MyNodeApp', EnvironmentName='MyEnv')['ConfigurationSettings'][0]['OptionSettings']
    current = {item['OptionName']: item.get('Value', '') for item in settings if item['Namespace'] == 'aws:elasticbeanstalk:application:environment'}
    if any(current.get(key) != value for key, value in desired.items()):
        raise RuntimeError('Elastic Beanstalk environment properties do not match the database/cache endpoints.')


def verify(values, config):
    for url in (values['application_url'] + '/beans.json', values['beans_api_url']):
        with urllib.request.urlopen(url, timeout=30) as response:
            beans = json.load(response)
            if response.headers.get('Access-Control-Allow-Origin') != '*':
                raise RuntimeError('Coffee API did not return CORS headers.')
            if response.headers.get('Cache-Status') not in ('From Memcached', 'Directly from RDS'):
                raise RuntimeError('The application is not using the new caching controller.')
        first = next((item for item in beans if item['id'] == 1), {})
        if len(beans) != 14 or first.get('product_name') != 'Best bean EVER' or first.get('quantity') != 800:
            raise RuntimeError('Task 5 inventory update is missing.')
        if not any(item['product_name'] == 'Worlds greatest bean' for item in beans) or any(item['id'] == 14 for item in beans):
            raise RuntimeError('Task 6 creation or Task 7 deletion is missing.')
    with urllib.request.urlopen(values['application_url'] + '/beans', timeout=30) as response:
        if 'results from Cache' not in response.read().decode():
            raise RuntimeError('Waiting for the application to serve its bean list from Memcached.')
    with urllib.request.urlopen(values['cafe_url'], timeout=30) as response:
        if 'config.js' not in response.read().decode():
            raise RuntimeError('Café website is unavailable.')
    with urllib.request.urlopen(values['cafe_url'].rsplit('/', 1)[0] + '/config.js', timeout=30) as response:
        if config['api_id'] not in response.read().decode():
            raise RuntimeError('Café website points to the wrong API.')
    request = urllib.request.Request(values['beans_api_url'], method='OPTIONS')
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.headers.get('Access-Control-Allow-Origin') != '*':
            raise RuntimeError('API preflight CORS check failed.')
    products_url = values['beans_api_url'].rsplit('/', 1)[0] + '/products'
    with urllib.request.urlopen(products_url, timeout=30) as response:
        products = json.load(response)
        items = products.get('product_item_arr', []) if isinstance(products, dict) else products
        if not isinstance(items, list) or len(items) < 26:
            raise RuntimeError('The existing café product API is missing its seed data.')


def main():
    environment = load_environment()
    session = aws_session(environment)
    config, outputs = discover(session, environment)
    guard_state(config)
    for key in ('cache_security_group_id', 'ide_security_group_id', 'subnet_ids', 'site_bucket',
                'api_id', 'api_root_id', 'application_url'):
        environment['TF_VAR_' + key] = json.dumps(config[key]) if isinstance(config[key], list) else config[key]
    environment['TF_IN_AUTOMATION'] = '1'
    environment['TF_INPUT'] = '0'
    terraform = find_terraform()
    def execute(*args, capture=False):
        result = subprocess.run([terraform, *args], cwd=ROOT, env=environment, check=True, text=True,
                                stdout=subprocess.PIPE if capture else None)
        return result.stdout if capture else None
    application_zip(config['repository_url'] + ':latest')
    print(f"Lab 9.1: account {config['account']}, IDE {config['ide_instance_id']}", flush=True)
    execute('init', '-no-color')
    execute('validate', '-no-color')
    imports(session, config, execute)
    from lab_ide import LabIDE
    remote = LabIDE(outputs['LabIDEURL'], outputs['LabIDEPassword'])
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            cache = pool.submit(execute, 'apply', '-auto-approve', '-no-color', '-target=aws_elasticache_cluster.cache')
            result = remote.run(bundle(config, 'prepare'), timeout=1800)
            if 'LAB91_PREPARED' not in result:
                raise RuntimeError('Lab IDE preparation did not finish.')
            cache.result()
        values = {key: value['value'] for key, value in json.loads(execute('output', '-json', capture=True)).items()}
        config.update(cache_endpoint=values['cache_endpoint'], cache_nodes=values['cache_nodes'])
        result = remote.run(bundle(config, 'cache'), timeout=900)
        if 'LAB91_CACHE_VERIFIED' not in result:
            raise RuntimeError('Tasks 4-7 cache/database tests did not finish.')
    finally:
        remote.close()
    image = session.client('ecr').describe_images(repositoryName='cafe/node-web-app', imageIds=[{'imageTag': 'latest'}])['imageDetails'][0]
    application_zip(config['repository_url'] + '@' + image['imageDigest'])
    version = 'Lab91-' + hashlib.md5((TOOLS / 'application.zip').read_bytes()).hexdigest()
    versions = session.client('elasticbeanstalk').describe_application_versions(ApplicationName='MyNodeApp', VersionLabels=[version])['ApplicationVersions']
    state = json.loads(execute('show', '-json', capture=True))
    tracked = [item['values']['name'] for item in state.get('values', {}).get('root_module', {}).get('resources', [])
               if item['type'] == 'aws_elastic_beanstalk_application_version']
    environment['TF_VAR_create_application_version'] = 'true' if not versions or version in tracked else 'false'
    execute('apply', '-auto-approve', '-no-color')
    values = {key: value['value'] for key, value in json.loads(execute('output', '-json', capture=True)).items()}
    update_application(session, config, values)
    deadline = time.monotonic() + 300
    last_report = 0
    while True:
        try:
            verify(values, config)
            break
        except Exception as error:
            if time.monotonic() > deadline:
                raise
            if time.monotonic() - last_report >= 30:
                print(f'Waiting for website/API/cache propagation: {error}', flush=True)
                last_report = time.monotonic()
            time.sleep(10)
    print('\nLab 9.1 completed. Three cache nodes, Python CRUD/cache and the deployed caching application verified.')
    print('Café: ' + values['cafe_url'])
    print('Beans: ' + values['application_url'] + '/beans')
    print('Coffee API: ' + values['beans_api_url'])
    print('Submit the lab in AWS Academy before ending the lab.')


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print(f'Lab 9.1 failed: {error}', file=sys.stderr)
        sys.exit(1)
