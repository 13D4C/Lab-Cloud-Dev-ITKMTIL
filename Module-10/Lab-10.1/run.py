import argparse
import base64
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
            raise RuntimeError('Multiple Lab IDE instances found. Set LAB_STACK_NAME to select Lab 10.1.')
        return candidates or None
    candidates = wait_for('the Lab 10.1 IDE instance', ide_ready, timeout=600)
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
    stack = wait_for('AWS Academy to finish preparing Lab 10.1', stack_ready)
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
        raise RuntimeError('Expected the provided café bucket, ProductsApi and supplierdb from Lab 10.1.')
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
    roles = [item['PhysicalResourceId'] for item in resources
             if item['ResourceType'] == 'AWS::IAM::Role' and item['LogicalResourceId'] == 'BeanstalkInstanceRole']
    distributions = [item['PhysicalResourceId'] for item in resources
                     if item['ResourceType'] == 'AWS::CloudFront::Distribution' and item['LogicalResourceId'] == 'CafeCloudfront']
    if len(roles) != 1 or len(distributions) != 1:
        raise RuntimeError('Expected the provided café CloudFront distribution and Elastic Beanstalk instance role.')
    config['consumer_role_arn'] = f"arn:aws:iam::{account}:role/{roles[0]}"
    distribution = session.client('cloudfront').get_distribution(Id=distributions[0])['Distribution']
    origins = distribution['DistributionConfig']['Origins']['Items']
    matches = [origin for origin in origins if origin['DomainName'].startswith(config['site_bucket'] + '.s3')]
    if len(matches) != 1 or not matches[0].get('S3OriginConfig', {}).get('OriginAccessIdentity'):
        raise RuntimeError('The provided café CloudFront distribution must use the provided S3 bucket and OAI.')
    config['distribution_id'] = distribution['Id']
    config['origin_access_identity_id'] = matches[0]['S3OriginConfig']['OriginAccessIdentity'].rsplit('/', 1)[-1]
    config['website_url'] = 'https://' + distribution['DomainName']
    config['queue_url'] = f"https://sqs.{config['region']}.amazonaws.com/{account}/updated_beans.fifo"
    config['dead_letter_queue_url'] = f"https://sqs.{config['region']}.amazonaws.com/{account}/DeadLetterQueue.fifo"
    config['topic_arn'] = f"arn:aws:sns:{config['region']}:{account}:updated_beans_sns.fifo"
    s3 = session.client('s3')
    try:
        policy = json.loads(s3.get_bucket_policy(Bucket=config['site_bucket'])['Policy'])
    except s3.exceptions.ClientError as error:
        if error.response['Error']['Code'] != 'NoSuchBucketPolicy':
            raise
        policy = {'Statement': []}
    config['preserved_bucket_statements'] = [item for item in policy['Statement']
        if item.get('Sid') not in ('AllowOAIAccess', 'Lab101OfficeWebsite')
        and not (item.get('Effect') == 'Allow' and item.get('Principal') == '*' and 'IpAddress' in item.get('Condition', {}))]
    if not any(item.get('Sid') == 'DenyOneObjectIfRequestNotSigned' for item in config['preserved_bucket_statements']):
        config['preserved_bucket_statements'].append({
            'Sid': 'DenyOneObjectIfRequestNotSigned', 'Effect': 'Deny', 'Principal': '*', 'Action': 's3:GetObject',
            'Resource': f"arn:aws:s3:::{config['site_bucket']}/report.html",
            'Condition': {'StringNotEquals': {'s3:authtype': 'REST-QUERY-STRING'}}
        })
    config['image_tag'] = 'lab101-node22-b9a9ae2623144b07'
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
        archive.add(ROOT / 'deploy.py', arcname='.lab101/deploy.py')
        data = json.dumps(config).encode()
        entry = tarfile.TarInfo('.lab101/config.json')
        entry.mode = 0o600
        entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))
    payload = base64.b64encode(output.getvalue()).decode()
    return "set -euo pipefail\ncd /home/ec2-user/environment\nmkdir -p .lab101\nbase64 -d <<'LAB101_BUNDLE' | tar xz\n" + payload + "\nLAB101_BUNDLE\nif ! python3.12 -m pip --version >/dev/null 2>&1; then sudo dnf install -y python3.12 python3.12-pip; fi\npython3.12 -u .lab101/deploy.py " + action


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
        print('Deploying the messaging image and cache configuration to the provided MyEnv...', flush=True)
        eb.update_environment(EnvironmentId=config['environment_id'], VersionLabel=values['application_version'], OptionSettings=[
            {'Namespace': 'aws:elasticbeanstalk:application:environment', 'OptionName': key, 'Value': value}
            for key, value in desired.items()
        ])
    def ready():
        environment = eb.describe_environments(EnvironmentIds=[config['environment_id']])['Environments'][0]
        if environment['Status'] in ('Terminating', 'Terminated'):
            raise RuntimeError('Elastic Beanstalk environment was terminated during deployment.')
        return environment if environment['Status'] == 'Ready' and environment.get('VersionLabel') == values['application_version'] else None
    wait_for('Elastic Beanstalk to deploy the messaging application', ready)
    settings = eb.describe_configuration_settings(ApplicationName='MyNodeApp', EnvironmentName='MyEnv')['ConfigurationSettings'][0]['OptionSettings']
    current = {item['OptionName']: item.get('Value', '') for item in settings if item['Namespace'] == 'aws:elasticbeanstalk:application:environment'}
    if any(current.get(key) != value for key, value in desired.items()):
        raise RuntimeError('Elastic Beanstalk environment properties do not match the database/cache endpoints.')


def imports(session, config, execute):
    resources = set(execute('state', 'list', capture=True).splitlines()) if (ROOT / 'terraform.tfstate').exists() else set()
    def adopt(address, identifier):
        if address not in resources:
            execute('import', '-no-color', address, identifier)
            resources.add(address)
    s3 = session.client('s3')
    try:
        s3.get_bucket_policy(Bucket=config['site_bucket'])
        adopt('aws_s3_bucket_policy.website', config['site_bucket'])
    except s3.exceptions.ClientError as error:
        if error.response['Error']['Code'] != 'NoSuchBucketPolicy':
            raise
    sqs = session.client('sqs')
    for name, address in [('DeadLetterQueue.fifo', 'dead_letter'), ('updated_beans.fifo', 'beans')]:
        try:
            url = sqs.get_queue_url(QueueName=name)['QueueUrl']
            adopt('aws_sqs_queue.' + address, url)
        except sqs.exceptions.QueueDoesNotExist:
            pass
    sns = session.client('sns')
    try:
        sns.get_topic_attributes(TopicArn=config['topic_arn'])
        adopt('aws_sns_topic.beans', config['topic_arn'])
        subscriptions = [item for page in sns.get_paginator('list_subscriptions_by_topic').paginate(TopicArn=config['topic_arn'])
                         for item in page['Subscriptions'] if item['Protocol'] == 'sqs'
                         and item['Endpoint'] == f"arn:aws:sqs:{config['region']}:{config['account']}:updated_beans.fifo"]
        if len(subscriptions) > 1:
            raise RuntimeError('More than one subscription links the lab topic and queue.')
        if subscriptions:
            adopt('aws_sns_topic_subscription.beans', subscriptions[0]['SubscriptionArn'])
    except sns.exceptions.NotFoundException:
        pass
    api = session.client('apigateway')
    for item in api.get_resources(restApiId=config['api_id'], limit=500)['items']:
        if item['path'] != '/bean_products':
            continue
        prefix = config['api_id'] + '/' + item['id']
        adopt('aws_api_gateway_resource.beans', prefix)
        for method, name in [('GET', 'beans'), ('OPTIONS', 'cors')]:
            if method not in item.get('resourceMethods', {}):
                continue
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


def wait_for_cloudfront(session, config):
    cloudfront = session.client('cloudfront')
    wait_for('the provided café CloudFront distribution', lambda:
             cloudfront.get_distribution(Id=config['distribution_id'])['Distribution']['Status'] == 'Deployed')


def verify_infrastructure(session, config, values):
    sqs = session.client('sqs')
    dead = sqs.get_queue_attributes(QueueUrl=values['dead_letter_queue_url'], AttributeNames=['All'])['Attributes']
    main = sqs.get_queue_attributes(QueueUrl=values['queue_url'], AttributeNames=['All'])['Attributes']
    for label, attributes, expected in [
        ('DLQ', dead, {'FifoQueue': 'true', 'VisibilityTimeout': '20', 'ReceiveMessageWaitTimeSeconds': '0',
                      'ContentBasedDeduplication': 'false', 'DeduplicationScope': 'queue'}),
        ('Queue', main, {'FifoQueue': 'true', 'VisibilityTimeout': '30', 'ReceiveMessageWaitTimeSeconds': '20',
                        'ContentBasedDeduplication': 'true', 'DeduplicationScope': 'queue', 'MessageRetentionPeriod': '345600'})
    ]:
        for key, value in expected.items():
            if attributes.get(key) != value:
                raise RuntimeError(f'{label}: {key} must be {value}.')
    redrive = json.loads(main['RedrivePolicy'])
    if redrive['deadLetterTargetArn'] != dead['QueueArn'] or int(redrive['maxReceiveCount']) != 5:
        raise RuntimeError('Queue redrive policy does not match the lab.')
    policy = json.loads(main['Policy'])
    required = {'beans-sqs', 'topic-subscription', 'get-messages'}
    if {item.get('Sid') for item in policy['Statement']} != required:
        raise RuntimeError('Queue policy does not match the lab access statements.')
    statements = {item['Sid']: item for item in policy['Statement']}
    root_arn = f"arn:aws:iam::{config['account']}:root"
    if statements['beans-sqs']['Principal'] != {'AWS': root_arn}:
        raise RuntimeError('The beans queue owner policy does not match the lab account.')
    subscription_policy = statements['topic-subscription']
    if subscription_policy['Principal'].get('Service') != 'sns.amazonaws.com' or subscription_policy.get('Condition') != {'ArnLike': {'aws:SourceArn': values['topic_arn']}}:
        raise RuntimeError('The SNS send permission must be restricted to the lab topic.')
    consumers = statements['get-messages']['Principal']['AWS']
    if set(consumers if isinstance(consumers, list) else [consumers]) != {config['consumer_role_arn'], root_arn}:
        raise RuntimeError('The queue consumer permission does not match the provided application role.')
    dead_policy = json.loads(dead['Policy'])
    if len(dead_policy['Statement']) != 1 or dead_policy['Statement'][0]['Principal'] != {'AWS': root_arn}:
        raise RuntimeError('The dead-letter queue must be restricted to the lab account.')
    topic = session.client('sns').get_topic_attributes(TopicArn=values['topic_arn'])['Attributes']
    if topic.get('FifoTopic') != 'true' or topic.get('ContentBasedDeduplication') != 'true' or topic.get('DisplayName') != 'updated beans sns':
        raise RuntimeError('The SNS topic must use FIFO and content-based deduplication.')
    topic_policy = json.loads(topic['Policy'])
    if {item.get('Sid') for item in topic_policy['Statement']} != {'BeansAllowActions', 'BeansAllowPublish'} or any(
        item['Principal'] != {'AWS': root_arn} for item in topic_policy['Statement']
    ):
        raise RuntimeError('The SNS topic policy must be restricted to the lab account.')
    subscription = session.client('sns').get_subscription_attributes(SubscriptionArn=values['subscription_arn'])['Attributes']
    if subscription.get('Endpoint') != main['QueueArn'] or subscription.get('TopicArn') != values['topic_arn'] or subscription.get('RawMessageDelivery', 'false') != 'false':
        raise RuntimeError('The SNS subscription must send wrapped notifications to the beans queue.')
    print('FIFO queues, policies, five-attempt DLQ redrive and SNS subscription verified.', flush=True)


def verify_web(config, values):
    def request(url):
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.read(), response.headers
    page, _ = request(values['website_url'] + '/index.html')
    if b'<html' not in page.lower():
        raise RuntimeError('CloudFront did not return the café website.')
    beans, _ = request(config['application_url'] + '/beans.json')
    rows = json.loads(beans)
    quantities = {f"{item['supplier_id']}:{item['type']}": int(item['quantity']) for item in rows}
    if len(rows) != 14 or quantities.get('3:Excelsa') != 209 or quantities.get('6:Arabica') != 306:
        raise RuntimeError('The web application does not show the final inventory.')
    _, headers = request(config['application_url'] + '/beans.json')
    if headers.get('Cache-Status') != 'From Memcached':
        raise RuntimeError('The application is not using Memcached.')
    api_beans, _ = request(values['beans_api_url'])
    if json.loads(api_beans) != rows:
        raise RuntimeError('The beans API and application returned different inventory.')
    products, _ = request(f"https://{config['api_id']}.execute-api.{config['region']}.amazonaws.com/prod/products")
    if len(json.loads(products)['product_item_arr']) != 26:
        raise RuntimeError('The food products API did not return the seeded products.')
    try:
        request(f"https://{config['site_bucket']}.s3.{config['region']}.amazonaws.com/index.html")
    except urllib.error.HTTPError as error:
        if error.code != 403:
            raise
    else:
        raise RuntimeError('The website bucket must be private behind the provided OAI.')
    print('CloudFront website, products API, beans API, Memcached and private S3 verified.', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    environment = load_environment()
    session = aws_session(environment)
    config, outputs = discover(session, environment)
    print(f"Lab 10.1 account: {config['account']}; stack: {config['stack_name']}", flush=True)
    guard_state(config)
    terraform = find_terraform()
    for key in ('site_bucket', 'api_id', 'api_root_id', 'application_url', 'consumer_role_arn',
                'origin_access_identity_id', 'website_url', 'client_cidr', 'preserved_bucket_statements'):
        environment['TF_VAR_' + key] = json.dumps(config[key]) if key == 'preserved_bucket_statements' else config[key]
    environment['TF_VAR_account_id'] = config['account']
    environment['TF_IN_AUTOMATION'] = '1'
    environment['AWS_PAGER'] = ''
    def execute(*arguments, capture=False):
        result = subprocess.run([terraform, *arguments], cwd=ROOT, env=environment, check=True,
                                text=True, stdout=subprocess.PIPE if capture else None)
        return result.stdout if capture else None
    execute('init', '-input=false', '-no-color')
    execute('fmt', '-check', '-no-color')
    if args.verify_only:
        values = {key: item['value'] for key, item in json.loads(execute('output', '-json', capture=True)).items()}
    else:
        from lab_ide import LabIDE
        remote = LabIDE(outputs['LabIDEURL'], outputs['LabIDEPassword'])
        try:
            if 'LAB101_PREPARED' not in remote.run(bundle(config, 'prepare'), timeout=1800):
                raise RuntimeError('Application preparation did not finish.')
        finally:
            remote.close()
        image = session.client('ecr').describe_images(repositoryName='cafe/node-web-app',
            imageIds=[{'imageTag': config['image_tag']}])['imageDetails'][0]
        application_zip(config['repository_url'] + '@' + image['imageDigest'])
        version = 'Lab101-' + hashlib.md5((TOOLS / 'application.zip').read_bytes()).hexdigest()
        versions = session.client('elasticbeanstalk').describe_application_versions(ApplicationName='MyNodeApp', VersionLabels=[version])['ApplicationVersions']
        tracked = []
        if (ROOT / 'terraform.tfstate').exists():
            state = json.loads(execute('show', '-json', capture=True))
            tracked = [item['values']['name'] for item in state.get('values', {}).get('root_module', {}).get('resources', [])
                       if item['type'] == 'aws_elastic_beanstalk_application_version']
        environment['TF_VAR_create_application_version'] = 'true' if not versions or version in tracked else 'false'
        execute('validate', '-no-color')
        imports(session, config, execute)
        execute('plan', '-input=false', '-no-color', '-out=.tools/apply.tfplan')
        plan = json.loads(execute('show', '-json', '.tools/apply.tfplan', capture=True))
        deletes = [item['address'] for item in plan.get('resource_changes', []) if 'delete' in item['change']['actions']]
        if deletes:
            raise RuntimeError('The plan would delete or replace resources: ' + ', '.join(deletes))
        execute('apply', '-input=false', '-no-color', '.tools/apply.tfplan')
        values = {key: item['value'] for key, item in json.loads(execute('output', '-json', capture=True)).items()}
        update_application(session, config, values)
        wait_for_cloudfront(session, config)
    verify_infrastructure(session, config, values)
    from lab_ide import LabIDE
    remote = LabIDE(outputs['LabIDEURL'], outputs['LabIDEPassword'])
    try:
        action = 'verify' if args.verify_only else 'messaging'
        if 'LAB101_MESSAGING_VERIFIED' not in remote.run(bundle(config, action), timeout=1800):
            raise RuntimeError('Message processing verification did not finish.')
    finally:
        remote.close()
    def website_ready():
        try:
            verify_web(config, values)
            return True
        except (urllib.error.URLError, RuntimeError, json.JSONDecodeError) as error:
            print('Waiting for website/API/cache propagation: ' + str(error), flush=True)
            return False
    wait_for('the completed web application', website_ready, timeout=300)
    print('\nLab 10.1 completed. Deduplication, three DLQ messages and inventory 209/306 verified.')
    print('Coffee suppliers: ' + values['application_url'])
    print('Cafe: ' + values['website_url'])
    print('Submit the lab in AWS Academy before ending the lab.')


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print(f'Lab 10.1 failed: {error}', file=sys.stderr)
        sys.exit(1)


