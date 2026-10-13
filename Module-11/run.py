import argparse
import base64
import hashlib
import io
import ipaddress
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import urllib.error
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
    def ide_ready():
        candidates = []
        for page in ec2.get_paginator('describe_instances').paginate(Filters=[
            {'Name': 'instance-state-name', 'Values': ['running']}, {'Name': 'tag:Name', 'Values': ['Lab IDE', 'Lab Instance']}
        ]):
            for reservation in page['Reservations']:
                for instance in reservation['Instances']:
                    tags = {item['Key']: item['Value'] for item in instance.get('Tags', [])}
                    if tags.get('cloudlab') and (not environment.get('LAB_STACK_NAME') or tags['cloudlab'] == environment['LAB_STACK_NAME']):
                        candidates.append(instance)
        if len(candidates) > 1:
            raise RuntimeError('Multiple Lab IDE instances found. Set LAB_STACK_NAME to select Lab 11.1.')
        return candidates or None
    candidates = wait_for('the Lab 11.1 IDE instance', ide_ready, timeout=600)
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
    stack = wait_for('AWS Academy to finish preparing Lab 11.1', stack_ready)
    outputs = {item['OutputKey']: item['OutputValue'] for item in stack.get('Outputs', [])}
    resources = [item for page in cf.get_paginator('list_stack_resources').paginate(StackName=stack_name)
                 for item in page['StackResourceSummaries']]
    if not outputs.get('LabIDEPassword'):
        for item in resources:
            if item['ResourceType'] == 'AWS::CloudFormation::Stack' and item['LogicalResourceId'] == 'VSCodeStack':
                nested = cf.list_stack_resources(StackName=item['PhysicalResourceId'])['StackResourceSummaries']
                for child in nested:
                    if child['ResourceType'] == 'AWS::CloudFormation::Stack' and 'Password' in child['LogicalResourceId']:
                        password_outputs = cf.describe_stacks(StackName=child['PhysicalResourceId'])['Stacks'][0].get('Outputs', [])
                        outputs['LabIDEPassword'] = next((value['OutputValue'] for value in password_outputs if value['OutputKey'] == 'RandomCharacters'), None)
    if not outputs.get('LabIDEURL') or not outputs.get('LabIDEPassword'):
        raise RuntimeError('Lab IDE URL/password are missing from the lab stack outputs.')
    buckets = [item['PhysicalResourceId'] for item in resources if item['ResourceType'] == 'AWS::S3::Bucket'
               and '-s3bucket-' in item['PhysicalResourceId']]
    api_ids = [item['PhysicalResourceId'] for item in resources if item['ResourceType'] == 'AWS::ApiGateway::RestApi']
    db_ids = [item['PhysicalResourceId'] for item in resources if item['ResourceType'] == 'AWS::RDS::DBCluster']
    if len(buckets) != 1 or len(api_ids) != 1 or db_ids != ['supplierdb']:
        raise RuntimeError('Expected the provided café bucket, ProductsApi and supplierdb from Lab 11.1.')
    cluster = session.client('rds').describe_db_clusters(DBClusterIdentifier='supplierdb')['DBClusters'][0]
    subnet_group = session.client('rds').describe_db_subnet_groups(DBSubnetGroupName=cluster['DBSubnetGroup'])['DBSubnetGroups'][0]
    if subnet_group['VpcId'] != ide['VpcId']:
        raise RuntimeError('The provided database must be in the Lab IDE VPC.')
    groups = [item['VpcSecurityGroupId'] for item in cluster['VpcSecurityGroups']]
    group_details = ec2.describe_security_groups(GroupIds=groups)['SecurityGroups']
    groups = [item['GroupId'] for item in group_details]
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
    cache_client = session.client('elasticache')
    caches = []
    for item in cache_client.describe_cache_clusters(ShowCacheNodeInfo=True)['CacheClusters']:
        if item['Engine'] == 'memcached':
            group = cache_client.describe_cache_subnet_groups(CacheSubnetGroupName=item['CacheSubnetGroupName'])['CacheSubnetGroups'][0]
            if group['VpcId'] == ide['VpcId']:
                caches.append(item)
    if len(caches) != 1 or caches[0]['CacheClusterStatus'] != 'available':
        raise RuntimeError('Expected the provided Memcached cluster in the Lab IDE VPC.')
    config['cache_endpoint'] = caches[0]['ConfigurationEndpoint']['Address']
    config['cache_nodes'] = [item['Endpoint'] for item in caches[0]['CacheNodes']]
    account = config['account']
    for key, name in [('lambda_role_arn', 'RoleForAllLambdas'), ('state_machine_role_arn', 'RoleForStepToCreateAReport'), ('api_execution_role_arn', 'RoleForAPIGWToTriggerStep')]:
        role = session.client('iam').get_role(RoleName=name)['Role']
        if role['Arn'].split(':')[4] != account:
            raise RuntimeError('The provided IAM role belongs to another account.')
        config[key] = role['Arn']
    config['database_security_group_id'] = groups[0]
    config['website_url'] = f"https://{config['site_bucket']}.s3.{config['region']}.amazonaws.com/index.html"
    config['topic_arn'] = f"arn:aws:sns:{config['region']}:{account}:EmailReport"
    config['state_machine_arn'] = f"arn:aws:states:{config['region']}:{account}:stateMachine:MyStateMachine"
    config['report_email'] = environment['REPORT_EMAIL']
    s3 = session.client('s3')
    try:
        policy = json.loads(s3.get_bucket_policy(Bucket=config['site_bucket'])['Policy'])
    except s3.exceptions.ClientError as error:
        if error.response['Error']['Code'] != 'NoSuchBucketPolicy':
            raise
        policy = {'Statement': []}
    config['preserved_bucket_statements'] = [item for item in policy['Statement']
        if item.get('Sid') != 'Lab111OfficeWebsite'
        and not (item.get('Effect') == 'Allow' and item.get('Principal') == '*' and 'IpAddress' in item.get('Condition', {}))]
    if not any(item.get('Sid') == 'DenyOneObjectIfRequestNotSigned' for item in config['preserved_bucket_statements']):
        config['preserved_bucket_statements'].append({
            'Sid': 'DenyOneObjectIfRequestNotSigned', 'Effect': 'Deny', 'Principal': '*', 'Action': 's3:GetObject',
            'Resource': f"arn:aws:s3:::{config['site_bucket']}/report.html",
            'Condition': {'StringNotEquals': {'s3:authtype': 'REST-QUERY-STRING'}}
        })
    config['image_tag'] = 'lab111-node22-71988210d55c2307'
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
        archive.add(ROOT / 'deploy.py', arcname='.lab111/deploy.py')
        data = json.dumps(config).encode()
        entry = tarfile.TarInfo('.lab111/config.json')
        entry.mode = 0o600
        entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))
    payload = base64.b64encode(output.getvalue()).decode()
    return "set -euo pipefail\ncd /home/ec2-user/environment\nmkdir -p .lab111\nbase64 -d <<'LAB111_BUNDLE' | tar xz\n" + payload + "\nLAB111_BUNDLE\nif ! python3.12 -m pip --version >/dev/null 2>&1; then sudo dnf install -y python3.12 python3.12-pip; fi\npython3.12 -u .lab111/deploy.py " + action


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
    desired = {'APP_DB_HOST': config['database_endpoint'], 'MEMC_HOST': config['cache_endpoint'] + ':11211'}
    environment = eb.describe_environments(EnvironmentIds=[config['environment_id']])['Environments'][0]
    if environment.get('VersionLabel') != values['application_version'] or any(current.get(key) != value for key, value in desired.items()):
        print('Deploying the reporting application and cache configuration to the provided MyEnv...', flush=True)
        eb.update_environment(EnvironmentId=config['environment_id'], VersionLabel=values['application_version'], OptionSettings=[
            {'Namespace': 'aws:elasticbeanstalk:application:environment', 'OptionName': key, 'Value': value}
            for key, value in desired.items()
        ])
    def ready():
        environment = eb.describe_environments(EnvironmentIds=[config['environment_id']])['Environments'][0]
        if environment['Status'] in ('Terminating', 'Terminated'):
            raise RuntimeError('Elastic Beanstalk environment was terminated during deployment.')
        return environment if environment['Status'] == 'Ready' and environment.get('VersionLabel') == values['application_version'] else None
    wait_for('Elastic Beanstalk to deploy the reporting application', ready)
    settings = eb.describe_configuration_settings(ApplicationName='MyNodeApp', EnvironmentName='MyEnv')['ConfigurationSettings'][0]['OptionSettings']
    current = {item['OptionName']: item.get('Value', '') for item in settings if item['Namespace'] == 'aws:elasticbeanstalk:application:environment'}
    if any(current.get(key) != value for key, value in desired.items()):
        raise RuntimeError('Elastic Beanstalk environment properties do not match the database/cache endpoints.')


def build_packages():
    import os
    import platform
    import tempfile
    TOOLS.mkdir(exist_ok=True)
    source = ROOT / 'lambda'
    staging = TOOLS / 'lambda'
    staging.mkdir(exist_ok=True)
    for name in ('package.json', 'package-lock.json', 'generate_html.mjs', 'report_data.mjs'):
        shutil.copy2(source / name, staging / name)
    fingerprint = hashlib.sha256((source / 'package-lock.json').read_bytes()).hexdigest()
    marker = staging / '.dependencies'
    if not (staging / 'node_modules').exists() or not marker.exists() or marker.read_text() != fingerprint:
        npm = shutil.which('npm')
        if not npm:
            system = {'Linux': 'linux', 'Darwin': 'darwin', 'Windows': 'win'}.get(platform.system())
            architecture = {'x86_64': 'x64', 'AMD64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine())
            if not system or not architecture:
                raise RuntimeError('Install Node.js 22 or later to package the Lambda functions.')
            version = 'v22.23.1'
            extension = 'zip' if system == 'win' else 'tar.gz'
            filename = f'node-{version}-{system}-{architecture}.{extension}'
            base = f'https://nodejs.org/dist/{version}'
            with tempfile.TemporaryDirectory(dir=TOOLS) as directory:
                archive = Path(directory) / filename
                with urllib.request.urlopen(f'{base}/{filename}', timeout=120) as response, archive.open('wb') as output:
                    shutil.copyfileobj(response, output)
                with urllib.request.urlopen(f'{base}/SHASUMS256.txt', timeout=60) as response:
                    expected = next(line.split()[0] for line in response.read().decode().splitlines() if line.split()[-1] == filename)
                if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
                    raise RuntimeError('Node.js package checksum does not match.')
                if extension == 'zip':
                    with zipfile.ZipFile(archive) as package:
                        package.extractall(TOOLS)
                else:
                    with tarfile.open(archive) as package:
                        package.extractall(TOOLS, **({'filter': 'data'} if hasattr(tarfile, 'data_filter') else {}))
            home = TOOLS / f'node-{version}-{system}-{architecture}'
            binary = home if system == 'win' else home / 'bin'
            os.environ['PATH'] = str(binary) + os.pathsep + os.environ.get('PATH', '')
            npm = str(binary / ('npm.cmd' if system == 'win' else 'npm'))
        subprocess.run([npm, 'ci', '--omit=dev', '--ignore-scripts', '--no-audit', '--no-fund'], cwd=staging, check=True)
        marker.write_text(fingerprint)
    def package(destination, files):
        with zipfile.ZipFile(TOOLS / destination, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path, name in files:
                entry = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, path.read_bytes())
    files = [(path, path.relative_to(staging).as_posix()) for path in sorted(staging.rglob('*'))
             if path.is_file() and path.name != '.dependencies']
    package('report_functions.zip', files)
    package('presigned_url.zip', [(source / 'presigned_url.py', 'presigned_url.py')])


def notification_status(config):
    path = TOOLS / 'notifications.json'
    identity = {key: config[key] for key in ('account', 'stack_name', 'report_email')}
    previous = json.loads(path.read_text()) if path.exists() else {}
    return identity, previous.get('identity') == identity and previous.get('closed') is True


def prepare_subscription(session, config):
    sns = session.client('sns')
    topic = sns.create_topic(Name='EmailReport')['TopicArn']
    if topic != config['topic_arn']:
        raise RuntimeError('The SNS topic belongs to another lab.')
    def subscriptions():
        return [item for page in sns.get_paginator('list_subscriptions_by_topic').paginate(TopicArn=topic)
                for item in page['Subscriptions'] if item['Protocol'] == 'email'
                and item['Endpoint'].lower() == config['report_email'].lower()]
    existing = subscriptions()
    if len(existing) > 1:
        raise RuntimeError('More than one subscription exists for this report email.')
    if not existing:
        sns.subscribe(TopicArn=topic, Protocol='email', Endpoint=config['report_email'], ReturnSubscriptionArn=True)
    def confirmed():
        return next((item['SubscriptionArn'] for item in subscriptions()
                     if item['SubscriptionArn'] != 'PendingConfirmation'), None)
    if not confirmed():
        print('Open the AWS Notifications email and click Confirm subscription. The runner will resume automatically.', flush=True)
    return wait_for('email subscription confirmation', confirmed, timeout=1800)


def imports(session, config, execute, subscription_arn=None):
    resources = set(execute('state', 'list', capture=True).splitlines()) if (ROOT / 'terraform.tfstate').exists() else set()
    def adopt(address, identifier):
        if address not in resources:
            execute('import', '-input=false', '-no-color', address, identifier)
            resources.add(address)
    s3 = session.client('s3')
    try:
        s3.get_bucket_policy(Bucket=config['site_bucket'])
        adopt('aws_s3_bucket_policy.website', config['site_bucket'])
    except s3.exceptions.ClientError as error:
        if error.response['Error']['Code'] != 'NoSuchBucketPolicy':
            raise
    sns = session.client('sns')
    try:
        sns.get_topic_attributes(TopicArn=config['topic_arn'])
        adopt('aws_sns_topic.email_report', config['topic_arn'])
    except sns.exceptions.NotFoundException:
        pass
    if subscription_arn:
        adopt('aws_sns_topic_subscription.email_report[0]', subscription_arn)
    lambdas = session.client('lambda')
    for name, address in [('GeneratePresignedURL', 'presigned_url'), ('generateHTML', 'generate_html'), ('generateReportData', 'report_data')]:
        try:
            def active():
                current = lambdas.get_function(FunctionName=name)['Configuration']
                if current['State'] == 'Failed':
                    raise RuntimeError(name + ' failed to initialize: ' + current.get('StateReason', ''))
                return current if current['State'] == 'Active' else None
            current = wait_for(name + ' to finish initializing', active)
            adopt('aws_lambda_function.' + address, name)
            state = json.loads((ROOT / 'terraform.tfstate').read_text())
            tainted = any(instance.get('status') == 'tainted' for resource in state['resources']
                          if resource['type'] == 'aws_lambda_function' and resource['name'] == address
                          for instance in resource['instances'])
            if tainted:
                package = TOOLS / ('presigned_url.zip' if address == 'presigned_url' else 'report_functions.zip')
                expected = base64.b64encode(hashlib.sha256(package.read_bytes()).digest()).decode()
                if current['CodeSha256'] != expected or current['Role'] != config['lambda_role_arn']:
                    raise RuntimeError('Interrupted function does not match the desired package: ' + name)
                execute('untaint', '-no-color', 'aws_lambda_function.' + address)
        except lambdas.exceptions.ResourceNotFoundException:
            pass
    states = session.client('stepfunctions')
    try:
        existing = states.describe_state_machine(stateMachineArn=config['state_machine_arn'])
        if existing['type'] != 'EXPRESS':
            raise RuntimeError('MyStateMachine already exists as a Standard workflow; refusing to replace it.')
        adopt('aws_sfn_state_machine.report', config['state_machine_arn'])
    except states.exceptions.StateMachineDoesNotExist:
        pass
    logs = session.client('logs')
    name = '/aws/vendedlogs/states/MyStateMachine-Logs'
    if any(group['logGroupName'] == name for group in logs.describe_log_groups(logGroupNamePrefix=name)['logGroups']):
        adopt('aws_cloudwatch_log_group.state_machine', name)
    api = session.client('apigateway')
    for item in api.get_resources(restApiId=config['api_id'], limit=500)['items']:
        if item['path'] not in ('/bean_products', '/create_report'):
            continue
        report = item['path'] == '/create_report'
        if report and 'POST' in item.get('resourceMethods', {}):
            current = api.get_integration(restApiId=config['api_id'], resourceId=item['id'], httpMethod='POST')
            if current['type'] != 'AWS':
                api.put_integration(restApiId=config['api_id'], resourceId=item['id'], httpMethod='POST',
                    type='AWS', integrationHttpMethod='POST', credentials=config['api_execution_role_arn'],
                    uri=f"arn:aws:apigateway:{config['region']}:states:action/StartExecution",
                    passthroughBehavior='WHEN_NO_TEMPLATES')
                print('Migrated the provided report integration to Step Functions; the API resource and method were preserved.', flush=True)
        prefix = config['api_id'] + '/' + item['id']
        adopt('aws_api_gateway_resource.' + ('report' if report else 'beans'), prefix)
        for method, name in ([('POST', 'report')] if report else [('GET', 'beans'), ('OPTIONS', 'cors')]):
            if method not in item.get('resourceMethods', {}):
                continue
            adopt('aws_api_gateway_method.' + name, prefix + '/' + method)
            details = api.get_method(restApiId=config['api_id'], resourceId=item['id'], httpMethod=method)
            if details.get('methodIntegration'):
                adopt('aws_api_gateway_integration.' + name, prefix + '/' + method)
            if name != 'beans':
                if '200' in details.get('methodResponses', {}):
                    adopt('aws_api_gateway_method_response.' + name, prefix + '/' + method + '/200')
                if '200' in details.get('methodIntegration', {}).get('integrationResponses', {}):
                    adopt('aws_api_gateway_integration_response.' + name, prefix + '/' + method + '/200')
    if any(item['stageName'] == 'prod' for item in api.get_stages(restApiId=config['api_id'])['item']):
        adopt('aws_api_gateway_stage.prod', config['api_id'] + '/prod')


def apply_plan(execute, filename, allowed_deletions=()):
    execute('plan', '-input=false', '-no-color', '-out=' + filename)
    plan = json.loads(execute('show', '-json', filename, capture=True))
    for item in plan.get('resource_changes', []):
        if item['type'] == 'aws_sfn_state_machine' and item['change'].get('after', {}).get('definition'):
            workflow = json.loads(item['change']['after']['definition'])
            if workflow.get('QueryLanguage') != 'JSONata' or workflow['States']['Process Report'].get('Next') != 'SNS Publish':
                raise RuntimeError('The plan contains the old workflow format. Restore the JSONata workflow before applying.')
    changes = {item['address']: item['change']['actions'] for item in plan.get('resource_changes', [])
               if item['change']['actions'] != ['no-op']}
    forbidden = [address for address, actions in changes.items() if 'delete' in actions and
                 (address not in allowed_deletions or actions != ['delete'])]
    if forbidden:
        raise RuntimeError('The plan would delete or replace lab resources: ' + ', '.join(forbidden))
    if allowed_deletions and any(address not in allowed_deletions for address in changes):
        raise RuntimeError('Subscription cleanup would also change other resources: ' + str(changes))
    execute('apply', '-input=false', '-no-color', filename)


def invoke_lambda(session, name, event):
    result = session.client('lambda').invoke(FunctionName=name, Payload=json.dumps(event).encode())
    payload = json.loads(result['Payload'].read())
    if result.get('FunctionError'):
        raise RuntimeError(name + ' failed: ' + json.dumps(payload))
    return payload


def verify(session, config, values, subscribed):
    data = invoke_lambda(session, 'generateReportData', {})
    if data.get('statusCode') != 200:
        raise RuntimeError('generateReportData did not return HTTP 200.')
    suppliers = json.loads(data['body'])['my_json_arr']
    if len(suppliers) != 8 or sum(len(item['bean_info_obj_arr']) for item in suppliers) != 14:
        raise RuntimeError('generateReportData must return eight suppliers and fourteen bean records.')
    rendered = invoke_lambda(session, 'generateHTML', data)
    if rendered.get('msg_str') != 'Report published to S3':
        raise RuntimeError('generateHTML did not upload the report.')
    signed = invoke_lambda(session, 'GeneratePresignedURL', {})['presigned_url_str']
    with urllib.request.urlopen(signed, timeout=30) as response:
        if response.headers.get('Content-Type') != 'text/html' or response.headers.get('Cache-Control') != 'max-age=0':
            raise RuntimeError('The report must be HTML with Cache-Control max-age=0.')
        report = response.read().decode()
    if report.count('data-role="supplier_info"') != 8 or report.count('data-role="bean_info"') != 14:
        raise RuntimeError('The signed report does not contain the complete supplier inventory.')
    states = session.client('stepfunctions')
    definition = states.describe_state_machine(stateMachineArn=values['state_machine_arn'])
    if definition['type'] != 'EXPRESS' or definition['loggingConfiguration']['level'] != 'ALL':
        raise RuntimeError('MyStateMachine must use Express mode and log all events.')
    workflow = json.loads(definition['definition'])
    if workflow.get('QueryLanguage') != 'JSONata':
        raise RuntimeError('The lab workflow must use JSONata Arguments and Output fields.')
    parallel = workflow['States']['Process Report']
    notification = workflow['States'].get('SNS Publish', {})
    if parallel.get('Next') != 'SNS Publish' or notification.get('Arguments', {}).get('TopicArn') != values['topic_arn']:
        raise RuntimeError('SNS Publish must notify EmailReport after both report branches finish.')
    branch_functions = []
    for branch in parallel['Branches']:
        branch_states = branch['States']
        if len(branch_states) != 1:
            raise RuntimeError('Each report branch must contain one Lambda invocation.')
        task = branch_states[branch['StartAt']]
        branch_functions.append(task['Arguments']['FunctionName'].split(':')[-2])
        if not task.get('End'):
            raise RuntimeError('Each parallel Lambda branch must end before SNS Publish.')
    if set(branch_functions) != {'GeneratePresignedURL', 'generateHTML'}:
        raise RuntimeError('The report branches must invoke GeneratePresignedURL and generateHTML.')
    if subscribed:
        session.client('sns').publish(TopicArn=values['topic_arn'], Subject='Test', Message='Hello! This is a test.')
    result = states.start_sync_execution(stateMachineArn=values['state_machine_arn'], input='{}', name='lab111-' + str(time.time_ns()))
    if result['status'] != 'SUCCEEDED':
        raise RuntimeError('The complete report workflow failed: ' + json.dumps({key: result.get(key) for key in ('status', 'error', 'cause')}))
    branches = json.loads(result['output'])
    if len(branches) != 2 or not any(item.get('msg_str') == 'Report published to S3' for item in branches):
        raise RuntimeError('The workflow did not execute both parallel report branches.')
    request = urllib.request.Request(values['create_report_url'], data=b'', method='POST', headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=60) as response:
        started = json.loads(response.read())
    arn = started.get('executionArn', '')
    if ':express:MyStateMachine:' not in arn or arn.split(':')[4] != config['account']:
        raise RuntimeError('POST /create_report did not start the lab state machine: ' + json.dumps(started))
    print('Started API report execution: ' + arn, flush=True)
    logs = session.client('logs')
    def api_finished():
        messages = []
        for page in logs.get_paginator('filter_log_events').paginate(
                logGroupName='/aws/vendedlogs/states/MyStateMachine-Logs',
                startTime=int(started['startDate'] * 1000) - 5000):
            for event in page['events']:
                message = json.loads(event['message'])
                if message.get('execution_arn') == arn:
                    messages.append(message)
        failures = [item for item in messages if item.get('type') in ('ExecutionFailed', 'ExecutionAborted', 'ExecutionTimedOut')]
        if failures:
            raise RuntimeError('The API report execution failed: ' + json.dumps(failures))
        return any(item.get('type') == 'ExecutionSucceeded' for item in messages)
    wait_for('the API-triggered report execution logs', api_finished, timeout=300)
    try:
        urllib.request.urlopen(f"https://{config['site_bucket']}.s3.{config['region']}.amazonaws.com/report.html", timeout=30)
    except urllib.error.HTTPError as error:
        if error.code != 403:
            raise
    else:
        raise RuntimeError('Unsigned access to report.html must be denied.')
    from botocore.config import Config
    expiration_url = session.client('s3', config=Config(signature_version='s3v4')).generate_presigned_url(
        'get_object', Params={'Bucket': config['site_bucket'], 'Key': 'report.html'}, ExpiresIn=5)
    with urllib.request.urlopen(expiration_url, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError('The temporary signed URL did not work.')
    time.sleep(7)
    try:
        urllib.request.urlopen(expiration_url, timeout=30)
    except urllib.error.HTTPError as error:
        if error.code != 403:
            raise
    else:
        raise RuntimeError('The temporary signed URL did not expire.')
    with urllib.request.urlopen(values['website_url'], timeout=30) as response:
        if '<html' not in response.read().decode().lower():
            raise RuntimeError('The café website was not available.')
    with urllib.request.urlopen(f"https://{config['api_id']}.execute-api.{config['region']}.amazonaws.com/prod/products", timeout=30) as response:
        if len(json.loads(response.read())['product_item_arr']) != 26:
            raise RuntimeError('The products API does not contain the expected seed data.')
    print('Verified: eight suppliers, fourteen beans, parallel workflow, API execution, private report and expiring signed URLs.', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify-only', action='store_true')
    subscription_options = parser.add_mutually_exclusive_group()
    subscription_options.add_argument('--subscribe-email', action='store_true')
    subscription_options.add_argument('--unsubscribe-email', action='store_true')
    args = parser.parse_args()
    environment = load_environment()
    if not environment.get('REPORT_EMAIL') or environment['REPORT_EMAIL'] == 'you@example.com':
        raise RuntimeError('Set report_email in Module-11/.env before running.')
    session = aws_session(environment)
    config, outputs = discover(session, environment)
    print(f"Lab 11.1 account: {config['account']}; stack: {config['stack_name']}", flush=True)
    guard_state(config)
    identity, closed = notification_status(config)
    if args.subscribe_email:
        closed = False
    if args.unsubscribe_email:
        closed = True
    subscription = None if closed else prepare_subscription(session, config)
    terraform = find_terraform()
    for key in ('site_bucket', 'api_id', 'api_root_id', 'application_url', 'lambda_role_arn', 'state_machine_role_arn',
                'api_execution_role_arn', 'database_endpoint', 'subnet_ids', 'database_security_group_id',
                'report_email', 'client_cidr', 'preserved_bucket_statements'):
        environment['TF_VAR_' + key] = json.dumps(config[key]) if key in ('subnet_ids', 'preserved_bucket_statements') else config[key]
    environment['TF_VAR_account_id'] = config['account']
    environment['TF_VAR_subscribe_email'] = 'false' if closed else 'true'
    environment['TF_IN_AUTOMATION'] = '1'
    environment['AWS_PAGER'] = ''
    def execute(*arguments, capture=False):
        result = subprocess.run([terraform, *arguments], cwd=ROOT, env=environment, check=True,
                                text=True, stdout=subprocess.PIPE if capture else None)
        return result.stdout if capture else None
    execute('init', '-input=false', '-no-color')
    execute('fmt', '-check', '-no-color')
    if args.unsubscribe_email:
        state = json.loads((ROOT / 'terraform.tfstate').read_text())
        environment['TF_VAR_create_application_version'] = 'true' if any(
            item['type'] == 'aws_elastic_beanstalk_application_version' and item['instances']
            for item in state['resources']) else 'false'
        apply_plan(execute, '.tools/unsubscribe.tfplan', allowed_deletions=('aws_sns_topic_subscription.email_report[0]',))
        remaining = [item for page in session.client('sns').get_paginator('list_subscriptions_by_topic').paginate(TopicArn=config['topic_arn'])
                     for item in page['Subscriptions'] if item['Protocol'] == 'email' and item['Endpoint'].lower() == config['report_email'].lower()]
        if remaining:
            raise RuntimeError('The email subscription was not removed.')
        (TOOLS / 'notifications.json').write_text(json.dumps({'identity': identity, 'closed': True}))
        print('Email subscription closed. Other lab resources were preserved.', flush=True)
        return
    if not args.verify_only:
        build_packages()
        from lab_ide import LabIDE
        remote = LabIDE(outputs['LabIDEURL'], outputs['LabIDEPassword'])
        try:
            if 'LAB111_PREPARED' not in remote.run(bundle(config, 'prepare'), timeout=1800):
                raise RuntimeError('Application preparation did not finish.')
        finally:
            remote.close()
        image = session.client('ecr').describe_images(repositoryName='cafe/node-web-app',
            imageIds=[{'imageTag': config['image_tag']}])['imageDetails'][0]
        application_zip(config['repository_url'] + '@' + image['imageDigest'])
        version = 'Lab111-' + hashlib.md5((TOOLS / 'application.zip').read_bytes()).hexdigest()
        versions = session.client('elasticbeanstalk').describe_application_versions(ApplicationName='MyNodeApp', VersionLabels=[version])['ApplicationVersions']
        tracked = []
        if (ROOT / 'terraform.tfstate').exists():
            state = json.loads(execute('show', '-json', capture=True))
            tracked = [item['values']['name'] for item in state.get('values', {}).get('root_module', {}).get('resources', [])
                       if item['type'] == 'aws_elastic_beanstalk_application_version']
        environment['TF_VAR_create_application_version'] = 'true' if not versions or version in tracked else 'false'
        execute('validate', '-no-color')
        imports(session, config, execute, subscription)
        apply_plan(execute, '.tools/apply.tfplan')
        values = {key: item['value'] for key, item in json.loads(execute('output', '-json', capture=True)).items()}
        update_application(session, config, values)
    else:
        values = {key: item['value'] for key, item in json.loads(execute('output', '-json', capture=True)).items()}
    verify(session, config, values, subscribed=bool(subscription))
    if subscription:
        (TOOLS / 'notifications.json').write_text(json.dumps({'identity': identity, 'closed': False}))
        print('Email subscription retained for grading. After submitting, run: python3 Module-11/run.py --unsubscribe-email', flush=True)
    print('Lab 11.1 completed. Submit the lab in AWS Academy before ending it.')
    print('Create report: ' + values['create_report_url'])
    print('Website: ' + values['website_url'])


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print(f'Lab 11.1 failed: {error}', file=sys.stderr)
        sys.exit(1)


