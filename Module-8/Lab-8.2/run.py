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

from runtime import ROOT, TOOLS, aws_session, find_tofu, load_environment


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
        raise RuntimeError('Start Lab 8.2 and wait for Lab IDE. Set LAB_STACK_NAME if multiple labs are running.')
    ide = candidates[0]
    if len(ide['SecurityGroups']) != 1:
        raise RuntimeError('Expected one Lab IDE security group.')
    tags = {item['Key']: item['Value'] for item in ide['Tags']}
    stack_name = tags.get('cloudlab') or environment.get('LAB_STACK_NAME')
    stack = session.client('cloudformation').describe_stacks(StackName=stack_name)['Stacks'][0]
    outputs = {item['OutputKey']: item['OutputValue'] for item in stack.get('Outputs', [])}
    if not outputs.get('LabIDEURL') or not outputs.get('LabIDEPassword'):
        raise RuntimeError('Wait for Start Lab to finish; Lab IDE outputs are missing.')
    buckets = [item['Name'] for item in session.client('s3').list_buckets()['Buckets']
               if item['Name'].startswith(stack_name + '-') and '-s3bucket-' in item['Name']]
    apis = []
    for page in session.client('apigateway').get_paginator('get_rest_apis').paginate():
        apis.extend(item for item in page['items'] if item['name'] == 'ProductsApi'
                    and item.get('tags', {}).get('cloudlab', stack_name) == stack_name)
    if len(buckets) != 1 or len(apis) != 1:
        raise RuntimeError('Expected the precreated café S3 bucket and ProductsApi from Lab 8.2.')
    subnet = ec2.describe_subnets(SubnetIds=[ide['SubnetId']])['Subnets'][0]
    zones = sorted(zone['ZoneName'] for zone in ec2.describe_availability_zones()['AvailabilityZones']
                   if zone['State'] == 'available' and zone['ZoneName'] != subnet['AvailabilityZone'])
    route_tables = ec2.describe_route_tables(Filters=[{'Name': 'vpc-id', 'Values': [ide['VpcId']]}])['RouteTables']
    public_routes = [table for table in route_tables if any(route.get('DestinationCidrBlock') == '0.0.0.0/0'
                     and route.get('GatewayId', '').startswith('igw-') for route in table['Routes'])]
    if len(public_routes) != 1 or not zones:
        raise RuntimeError('Lab VPC needs a public route table and two availability zones.')
    extra_subnets = ec2.describe_subnets(Filters=[{'Name': 'vpc-id', 'Values': [ide['VpcId']]},
                                                {'Name': 'cidr-block', 'Values': ['10.0.2.0/24']}])['Subnets']
    extra_zone = extra_subnets[0]['AvailabilityZone'] if extra_subnets else zones[0]
    if extra_zone == subnet['AvailabilityZone']:
        raise RuntimeError('The extra RDS subnet must be in a different availability zone from Lab IDE.')
    rds = session.client('rds')
    versions = [item['EngineVersion'] for page in rds.get_paginator('describe_db_engine_versions').paginate(Engine='aurora-mysql')
                for item in page['DBEngineVersions'] if item['EngineVersion'].startswith('8.0.mysql_aurora.')]
    if not versions:
        raise RuntimeError('No Aurora MySQL 8.0 engine is available in this region.')
    engine = '8.0.mysql_aurora.3.07.0'
    if engine not in versions:
        engine = max(versions, key=lambda value: tuple(map(int, value.split('mysql_aurora.')[1].split('.'))))
    existing_clusters = rds.describe_db_clusters()['DBClusters']
    existing_database = next((item for item in existing_clusters if item['DBClusterIdentifier'] == 'supplierdb'), None)
    if existing_database:
        if existing_database['DBSubnetGroup'] != 'supplierdb-subnets':
            raise RuntimeError('An unrelated supplierdb cluster already exists; check the active lab.')
        engine = existing_database['EngineVersion']
    stacks = [item for item in session.client('elasticbeanstalk').list_available_solution_stacks()['SolutionStacks']
              if item.endswith('running Docker')]
    preferred = [item for item in stacks if 'Amazon Linux 2 ' in item] or stacks
    if not preferred:
        raise RuntimeError('Elastic Beanstalk Docker platform is unavailable.')
    solution = preferred[0]
    if environment.get('CLIENT_CIDR'):
        cidr = environment['CLIENT_CIDR']
    else:
        with urllib.request.urlopen('https://checkip.amazonaws.com', timeout=30) as response:
            cidr = response.read().decode().strip() + '/32'
    network = ipaddress.ip_network(cidr, strict=True)
    if network.version != 4 or network.prefixlen != 32:
        raise RuntimeError('CLIENT_CIDR must be your public IPv4 address followed by /32.')
    account = session.client('sts').get_caller_identity()['Account']
    repository = session.client('ecr').describe_repositories(repositoryNames=['cafe/node-web-app'])['repositories'][0]
    config = dict(account=account, stack_name=stack_name, region=environment['TF_VAR_aws_region'],
                  site_bucket=buckets[0], api_id=apis[0]['id'], api_root_id=apis[0]['rootResourceId'],
                  ide_instance_id=ide['InstanceId'], extra_availability_zone=extra_zone,
                  public_route_table_id=public_routes[0]['RouteTableId'], engine_version=engine,
                  solution_stack_name=solution, client_cidr=cidr, repository_url=repository['repositoryUri'])
    return config, outputs, ide


def guard_state(config):
    TOOLS.mkdir(exist_ok=True)
    path = TOOLS / 'lab.json'
    identity = {key: config[key] for key in ('account', 'stack_name', 'ide_instance_id')}
    if path.exists() and json.loads(path.read_text()) != identity:
        archive = TOOLS / 'previous-labs' / str(time.time_ns())
        archive.mkdir(parents=True)
        for state in ROOT.glob('terraform.tfstate*'):
            shutil.move(str(state), archive / state.name)
        print('New lab detected; previous local state archived.', flush=True)
    path.write_text(json.dumps(identity))


def bundle(config, action):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        archive.add(ROOT / 'deploy.py', arcname='.lab82/deploy.py')
        data = json.dumps(config).encode()
        entry = tarfile.TarInfo('.lab82/config.json')
        entry.mode = 0o600
        entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))
    payload = base64.b64encode(output.getvalue()).decode()
    return "set -euo pipefail\ncd /home/ec2-user/environment\nmkdir -p .lab82\nbase64 -d <<'LAB82_BUNDLE' | tar xz\n" + payload + "\nLAB82_BUNDLE\npython3 -u .lab82/deploy.py " + action


def recover_environment(session, ide):
    eb = session.client('elasticbeanstalk')
    for environment in eb.describe_environments(ApplicationName='MyNodeApp', EnvironmentNames=['MyEnv'], IncludeDeleted=False)['Environments']:
        if environment['Status'] == 'Terminated':
            continue
        identifier = environment['EnvironmentId']
        if environment['Status'] != 'Terminating':
            status = session.client('cloudformation').describe_stacks(StackName='awseb-' + identifier + '-stack')['Stacks'][0]['StackStatus']
            if status not in ('CREATE_FAILED', 'ROLLBACK_COMPLETE', 'ROLLBACK_FAILED'):
                continue
            settings = eb.describe_configuration_settings(ApplicationName='MyNodeApp', EnvironmentName='MyEnv')['ConfigurationSettings'][0]['OptionSettings']
            vpc = next((item['Value'] for item in settings if item['Namespace'] == 'aws:ec2:vpc' and item['OptionName'] == 'VPCId'), None)
            if vpc != ide['VpcId']:
                raise RuntimeError('Failed MyEnv belongs to a different VPC; check the active lab.')
            print('Removing the failed Elastic Beanstalk environment before retrying...', flush=True)
            eb.terminate_environment(EnvironmentId=identifier)
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            values = eb.describe_environments(EnvironmentIds=[identifier])['Environments']
            if not values or values[0]['Status'] == 'Terminated':
                break
            time.sleep(15)
        else:
            raise TimeoutError('Timed out removing the failed Elastic Beanstalk environment. Run again after termination.')


def imports(session, config, ide, execute):
    resources = set(execute('state', 'list', capture=True).splitlines()) if (ROOT / 'terraform.tfstate').exists() else set()
    def adopt(address, identifier):
        if address not in resources:
            execute('import', '-no-color', address, identifier)
    ec2 = session.client('ec2')
    subnets = ec2.describe_subnets(Filters=[{'Name': 'vpc-id', 'Values': [ide['VpcId']]},
                                          {'Name': 'cidr-block', 'Values': ['10.0.2.0/24']}])['Subnets']
    if subnets:
        adopt('aws_subnet.extra', subnets[0]['SubnetId'])
        tables = ec2.describe_route_tables(Filters=[{'Name': 'association.subnet-id', 'Values': [subnets[0]['SubnetId']]}])['RouteTables']
        if tables:
            adopt('aws_route_table_association.extra', subnets[0]['SubnetId'] + '/' + tables[0]['RouteTableId'])
    group = ide['SecurityGroups'][0]['GroupId']
    rules = ec2.describe_security_group_rules(Filters=[{'Name': 'group-id', 'Values': [group]}])['SecurityGroupRules']
    for rule in rules:
        if rule['IsEgress'] or rule.get('IpProtocol') != 'tcp' or rule.get('FromPort') != rule.get('ToPort'):
            continue
        if rule.get('FromPort') == 3306 and rule.get('ReferencedGroupInfo', {}).get('GroupId') == group:
            adopt('aws_vpc_security_group_ingress_rule.database', rule['SecurityGroupRuleId'])
        if rule.get('FromPort') == 8000 and rule.get('CidrIpv4') == config['client_cidr']:
            adopt('aws_vpc_security_group_ingress_rule.preview', rule['SecurityGroupRuleId'])
    rds = session.client('rds')
    for item in rds.describe_db_subnet_groups()['DBSubnetGroups']:
        if item['DBSubnetGroupName'] == 'supplierdb-subnets':
            adopt('aws_db_subnet_group.database', item['DBSubnetGroupName'])
    for item in rds.describe_db_clusters()['DBClusters']:
        if item['DBClusterIdentifier'] == 'supplierdb':
            adopt('aws_rds_cluster.database', 'supplierdb')
            name = 'rds-db-credentials/' + item['DbClusterResourceId'] + '/admin'
            secrets = session.client('secretsmanager')
            try:
                secret = secrets.describe_secret(SecretId=name)
            except secrets.exceptions.ResourceNotFoundException:
                pass
            else:
                adopt('aws_secretsmanager_secret.query_editor', secret['ARN'])
                versions = secrets.list_secret_version_ids(SecretId=secret['ARN'])['Versions']
                for version in versions:
                    if 'AWSCURRENT' in version.get('VersionStages', []):
                        adopt('aws_secretsmanager_secret_version.query_editor', secret['ARN'] + '|' + version['VersionId'])
                        break
    for item in rds.describe_db_instances()['DBInstances']:
        if item['DBInstanceIdentifier'] == 'supplierdb-instance-1':
            adopt('aws_rds_cluster_instance.database', item['DBInstanceIdentifier'])
    eb = session.client('elasticbeanstalk')
    for item in eb.describe_applications(ApplicationNames=['MyNodeApp'])['Applications']:
        adopt('aws_elastic_beanstalk_application.application', item['ApplicationName'])
    for item in eb.describe_environments(ApplicationName='MyNodeApp', EnvironmentNames=['MyEnv'], IncludeDeleted=False)['Environments']:
        if item['Status'] != 'Terminated':
            adopt('aws_elastic_beanstalk_environment.application', item['EnvironmentId'])
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


def verify(values, config):
    for url in (values['application_url'] + '/beans.json', values['beans_api_url']):
        with urllib.request.urlopen(url, timeout=30) as response:
            beans = json.load(response)
            if response.headers.get('Access-Control-Allow-Origin') != '*':
                raise RuntimeError('Coffee API did not return CORS headers.')
        if len(beans) < 14 or not any(item['product_name'] == 'Best bean' for item in beans):
            raise RuntimeError('Expected coffee bean inventory from Aurora.')
    with urllib.request.urlopen(values['cafe_url'], timeout=30) as response:
        if 'config.js' not in response.read().decode():
            raise RuntimeError('Café website is unavailable.')
    with urllib.request.urlopen(values['cafe_url'].rsplit('/', 1)[0] + '/config.js', timeout=30) as response:
        if config['api_id'] not in response.read().decode():
            raise RuntimeError('Café website points to the wrong API.')
    products_url = values['beans_api_url'].rsplit('/', 1)[0] + '/products'
    with urllib.request.urlopen(products_url, timeout=30) as response:
        if len(json.load(response).get('product_item_arr', [])) < 26:
            raise RuntimeError('Existing café products API did not return the seeded food inventory.')
    request = urllib.request.Request(values['beans_api_url'], method='OPTIONS')
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.headers.get('Access-Control-Allow-Origin') != '*':
            raise RuntimeError('CORS preflight did not pass.')


def verify_database(session, values):
    client = session.client('rds-data')
    def query(sql):
        return client.execute_statement(resourceArn=values['database_arn'], secretArn=values['query_editor_secret_arn'],
                                        database='COFFEE', sql=sql)['records']
    objects = query("SELECT TABLE_NAME FROM information_schema.tables WHERE TABLE_SCHEMA='COFFEE' AND TABLE_NAME IN ('suppliers','beans') ORDER BY TABLE_NAME")
    if [row[0]['stringValue'] for row in objects] != ['beans', 'suppliers']:
        raise RuntimeError('Task 6 tables are not accessible through the RDS Data API.')
    users = query("SELECT user,host,plugin FROM mysql.user WHERE user='nodeapp' AND host='%'")
    if not users or users[0][2]['stringValue'] != 'mysql_native_password':
        raise RuntimeError('Task 6 nodeapp account does not match the lab instructions.')
    grants = query("SHOW GRANTS FOR 'nodeapp'@'%'")
    if not any('ON *.*' in row[0]['stringValue'] and 'WITH GRANT OPTION' in row[0]['stringValue'] for row in grants):
        raise RuntimeError('Task 6 nodeapp grants do not match the lab instructions.')
    counts = query('SELECT (SELECT COUNT(*) FROM suppliers), (SELECT COUNT(*) FROM beans)')[0]
    suppliers, beans = counts[0]['longValue'], counts[1]['longValue']
    if suppliers < 8 or beans < 14:
        raise RuntimeError(f'Task 7 data is incomplete: {suppliers} suppliers, {beans} beans.')
    print(f'Tasks 6-7 verified through RDS Data API: tables, nodeapp grants, {suppliers} suppliers, {beans} beans.', flush=True)


def main():
    environment = load_environment()
    session = aws_session(environment)
    config, outputs, ide = discover(session, environment)
    guard_state(config)
    keys = ('ide_instance_id', 'extra_availability_zone', 'public_route_table_id', 'engine_version',
            'solution_stack_name', 'client_cidr', 'site_bucket', 'api_id', 'api_root_id')
    environment.update({'TF_VAR_' + key: config[key] for key in keys})
    environment.update(TF_IN_AUTOMATION='1', TF_INPUT='0')
    tofu = find_tofu()
    def execute(*args, capture=False):
        result = subprocess.run([tofu, *args], cwd=ROOT, env=environment, check=True, text=True,
                                stdout=subprocess.PIPE if capture else None)
        return result.stdout if capture else None
    def application_zip(image):
        document = {'AWSEBDockerrunVersion': '1', 'Image': {'Name': image, 'Update': 'true'},
                    'Ports': [{'ContainerPort': 3000}]}
        with zipfile.ZipFile(TOOLS / 'application.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
            entry = zipfile.ZipInfo('Dockerrun.aws.json', date_time=(2020, 1, 1, 0, 0, 0))
            archive.writestr(entry, json.dumps(document, sort_keys=True))
    application_zip(config['repository_url'] + ':latest')
    print(f"Lab 8.2: account {config['account']}, IDE {config['ide_instance_id']}", flush=True)
    print(f"Aurora {config['engine_version']}; {config['solution_stack_name']}", flush=True)
    execute('init', '-no-color')
    execute('validate', '-no-color')
    recover_environment(session, ide)
    imports(session, config, ide, execute)
    from lab_ide import LabIDE
    remote = LabIDE(outputs['LabIDEURL'], outputs['LabIDEPassword'])
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            database = pool.submit(execute, 'apply', '-auto-approve', '-no-color',
                                   '-target=aws_rds_cluster_instance.database', '-target=aws_vpc_security_group_ingress_rule.preview')
            result = remote.run(bundle(config, 'prepare'), timeout=1800)
            if 'LAB82_PREPARED' not in result:
                raise RuntimeError('Lab IDE image preparation did not finish.')
            database.result()
        config['database_endpoint'] = json.loads(execute('output', '-json', capture=True))['database_endpoint']['value']
        result = remote.run(bundle(config, 'database'), timeout=900)
        if 'LAB82_DATABASE_READY' not in result:
            raise RuntimeError('Database and container verification did not finish.')
    finally:
        remote.close()
    image = session.client('ecr').describe_images(repositoryName='cafe/node-web-app', imageIds=[{'imageTag': 'latest'}])['imageDetails'][0]
    application_zip(config['repository_url'] + '@' + image['imageDigest'])
    version = 'MyNodeApp-' + hashlib.md5((TOOLS / 'application.zip').read_bytes()).hexdigest()
    resources = set(execute('state', 'list', capture=True).splitlines())
    existing_versions = session.client('elasticbeanstalk').describe_application_versions(
        ApplicationName='MyNodeApp', VersionLabels=[version])['ApplicationVersions']
    if existing_versions and 'aws_elastic_beanstalk_application_version.application' not in resources:
        execute('import', '-no-color', 'aws_elastic_beanstalk_application_version.application', 'MyNodeApp/' + version)
    execute('apply', '-auto-approve', '-no-color')
    values = {key: value['value'] for key, value in json.loads(execute('output', '-json', capture=True)).items()}
    verify_database(session, values)
    deadline = time.monotonic() + 300
    last_report = 0
    while True:
        try:
            verify(values, config)
            break
        except Exception as error:
            if time.monotonic() > deadline:
                raise
            if time.monotonic() - last_report > 30:
                print(f'Waiting for website/API propagation: {error}', flush=True)
                last_report = time.monotonic()
            time.sleep(10)
    print('\nLab 8.2 completed. Aurora, container CRUD, Elastic Beanstalk and API inventory verified.')
    print('Café: ' + values['cafe_url'])
    print('Suppliers: ' + values['application_url'] + '/suppliers')
    print('Coffee API: ' + values['beans_api_url'])
    print('Submit the lab in AWS Academy before ending the lab.')


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print(f'Lab 8.2 failed: {error}', file=sys.stderr)
        sys.exit(1)
