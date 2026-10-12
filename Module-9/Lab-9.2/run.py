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
import urllib.error
from http.cookies import SimpleCookie
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
            raise RuntimeError('Multiple Lab IDE instances found. Set LAB_STACK_NAME to select Lab 9.2.')
        return candidates or None
    candidates = wait_for('the Lab 9.2 IDE instance', ide_ready, timeout=600)
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
    stack = wait_for('AWS Academy to finish preparing Lab 9.2', stack_ready)
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
        raise RuntimeError('Expected the provided café bucket, ProductsApi and supplierdb from Lab 9.2.')
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
    s3 = session.client('s3')
    try:
        policy = json.loads(s3.get_bucket_policy(Bucket=config['site_bucket'])['Policy'])
    except s3.exceptions.ClientError as error:
        if error.response['Error']['Code'] != 'NoSuchBucketPolicy':
            raise
        policy = {'Statement': []}
    config['preserved_bucket_statements'] = [item for item in policy['Statement']
        if item.get('Sid') not in ('Lab92CloudFrontReadOnly', 'Lab92CloudFrontOAITransition')
        and not (item.get('Effect') == 'Allow' and item.get('Principal') == '*' and 'IpAddress' in item.get('Condition', {}))]
    if not any(item.get('Sid') == 'DenyOneObjectIfRequestNotSigned' for item in config['preserved_bucket_statements']):
        config['preserved_bucket_statements'].append({
            'Sid': 'DenyOneObjectIfRequestNotSigned', 'Effect': 'Deny', 'Principal': '*', 'Action': 's3:GetObject',
            'Resource': f"arn:aws:s3:::{config['site_bucket']}/report.html",
            'Condition': {'StringNotEquals': {'s3:authtype': 'REST-QUERY-STRING'}}
        })
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
        archive.add(ROOT / 'deploy.py', arcname='.lab92/deploy.py')
        data = json.dumps(config).encode()
        entry = tarfile.TarInfo('.lab92/config.json')
        entry.mode = 0o600
        entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))
    payload = base64.b64encode(output.getvalue()).decode()
    return "set -euo pipefail\ncd /home/ec2-user/environment\nmkdir -p .lab92\nbase64 -d <<'LAB92_BUNDLE' | tar xz\n" + payload + "\nLAB92_BUNDLE\nif ! python3.12 -m pip --version >/dev/null 2>&1; then sudo dnf install -y python3.12 python3.12-pip; fi\npython3.12 -u .lab92/deploy.py " + action


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
    cloudfront = session.client('cloudfront')
    distributions = [item for page in cloudfront.get_paginator('list_distributions').paginate()
                     for item in page.get('DistributionList', {}).get('Items', [])
                     if any(origin['DomainName'].startswith(config['site_bucket'] + '.s3')
                            for origin in item['Origins']['Items'])]
    if len(distributions) > 1:
        raise RuntimeError('Multiple CloudFront distributions use the cafe bucket. Select the intended distribution before continuing.')
    if distributions:
        adopt('aws_cloudfront_distribution.website', distributions[0]['Id'])
    attached = {origin.get('S3OriginConfig', {}).get('OriginAccessIdentity', '').rsplit('/', 1)[-1]
                for distribution in distributions for origin in distribution['Origins']['Items']
                if origin['DomainName'].startswith(config['site_bucket'] + '.s3')}
    identities = [item for page in cloudfront.get_paginator('list_cloud_front_origin_access_identities').paginate()
                  for item in page.get('CloudFrontOriginAccessIdentityList', {}).get('Items', [])]
    matches = [item for item in identities if item['Id'] in attached]
    if not matches:
        matches = [item for item in identities if item.get('Comment') == 'access-identity-cafe-website']
    if len(matches) > 1:
        raise RuntimeError('Multiple cafe origin access identities found.')
    if matches:
        adopt('aws_cloudfront_origin_access_identity.website', matches[0]['Id'])
    for item in cloudfront.list_origin_access_controls().get('OriginAccessControlList', {}).get('Items', []):
        if item['Name'] == 'access-identity-cafe-website':
            adopt('aws_cloudfront_origin_access_control.website', item['Id'])
    try:
        cloudfront.describe_function(Name='random_image_header', Stage='DEVELOPMENT')
        adopt('aws_cloudfront_function.random_image', 'random_image_header')
    except cloudfront.exceptions.NoSuchFunctionExists:
        pass
    regional_acl = None
    for scope, region, ip_name, ip_address, acl_name, acl_address in [
        ('CLOUDFRONT', 'us-east-1', 'office', 'office', 'cafe-website-office-only-during-dev', 'website'),
        ('REGIONAL', config['region'], 'office_regional', 'office_regional', 'website-api-gw-office-only-during-dev', 'api')
    ]:
        waf = session.client('wafv2', region_name=region)
        for item in waf.list_ip_sets(Scope=scope, Limit=100)['IPSets']:
            if item['Name'] == ip_name:
                adopt('aws_wafv2_ip_set.' + ip_address, item['Id'] + '/' + item['Name'] + '/' + scope)
        for item in waf.list_web_acls(Scope=scope, Limit=100)['WebACLs']:
            if item['Name'] == acl_name:
                adopt('aws_wafv2_web_acl.' + acl_address, item['Id'] + '/' + item['Name'] + '/' + scope)
                if scope == 'REGIONAL':
                    regional_acl = item['ARN']
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
        arn = f"arn:aws:apigateway:{config['region']}::/restapis/{config['api_id']}/stages/prod"
        waf = session.client('wafv2')
        try:
            association = waf.get_web_acl_for_resource(ResourceArn=arn).get('WebACL')
        except waf.exceptions.WAFNonexistentItemException:
            association = None
        if association:
            if association['ARN'] != regional_acl:
                raise RuntimeError('The API prod stage already has a different WAF ACL.')
            adopt('aws_wafv2_web_acl_association.api', association['ARN'] + ',' + arn)


def migrate_origin_access(session, config, execute):
    cloudfront = session.client('cloudfront')
    existing = [item for page in cloudfront.get_paginator('list_distributions').paginate()
                for item in page.get('DistributionList', {}).get('Items', [])
                if any(origin['DomainName'].startswith(config['site_bucket'] + '.s3') and origin.get('OriginAccessControlId')
                       for origin in item['Origins']['Items'])]
    if not existing:
        return
    execute('apply', '-auto-approve', '-no-color', '-target=aws_cloudfront_origin_access_identity.website')
    identity_id = json.loads(execute('output', '-json', capture=True))['origin_access_identity_id']['value']
    s3 = session.client('s3')
    policy = json.loads(s3.get_bucket_policy(Bucket=config['site_bucket'])['Policy'])
    policy['Statement'] = [item for item in policy['Statement'] if item.get('Sid') != 'Lab92CloudFrontOAITransition']
    policy['Statement'].append({
        'Sid': 'Lab92CloudFrontOAITransition', 'Effect': 'Allow',
        'Principal': {'AWS': 'arn:aws:iam::cloudfront:user/CloudFront Origin Access Identity ' + identity_id},
        'Action': 's3:GetObject', 'Resource': f"arn:aws:s3:::{config['site_bucket']}/*"
    })
    def allow_oai():
        try:
            s3.put_bucket_policy(Bucket=config['site_bucket'], Policy=json.dumps(policy))
            return True
        except s3.exceptions.ClientError as error:
            details = error.response['Error']
            if details['Code'] == 'MalformedPolicy' and 'invalid principal' in details.get('Message', '').lower():
                return False
            raise
    wait_for('S3 to recognize the new CloudFront OAI', allow_oai, timeout=180)
    print('OAI read access prepared; migrating the cafe origin from OAC to OAI.', flush=True)


def verify_oai_bucket_access(session, config, values):
    cloudfront = session.client('cloudfront')
    distribution = cloudfront.get_distribution(Id=values['distribution_id'])['Distribution']
    origins = [item for item in distribution['DistributionConfig']['Origins']['Items']
               if item['DomainName'].startswith(config['site_bucket'] + '.s3')]
    identity_id = values['origin_access_identity_id']
    expected_path = 'origin-access-identity/cloudfront/' + identity_id
    if len(origins) != 1 or origins[0].get('S3OriginConfig', {}).get('OriginAccessIdentity') != expected_path or origins[0].get('OriginAccessControlId'):
        raise RuntimeError('Task 2B requires an OAI attached to the cafe S3 origin.')
    identity = cloudfront.get_cloud_front_origin_access_identity(Id=identity_id)['CloudFrontOriginAccessIdentity']
    expected_arn = 'arn:aws:iam::cloudfront:user/CloudFront Origin Access Identity ' + identity_id
    policy = json.loads(session.client('s3').get_bucket_policy(Bucket=config['site_bucket'])['Policy'])
    for statement in policy['Statement']:
        principal = statement.get('Principal', {})
        actions = statement.get('Action', [])
        resources = statement.get('Resource', [])
        actions = [actions] if isinstance(actions, str) else actions
        resources = [resources] if isinstance(resources, str) else resources
        if isinstance(principal, dict):
            aws = principal.get('AWS', [])
            aws = [aws] if isinstance(aws, str) else aws
            authorized = expected_arn in aws or principal.get('CanonicalUser') == identity['S3CanonicalUserId']
            if statement.get('Effect') == 'Allow' and authorized and 's3:GetObject' in actions and f"arn:aws:s3:::{config['site_bucket']}/*" in resources and not statement.get('Condition'):
                print('Task 2B: attached OAI and matching S3 bucket read permission verified.', flush=True)
                return
    raise RuntimeError('Task 2B requires bucket read permission for the attached OAI.')


def verify_function(session, values):
    cloudfront = session.client('cloudfront')
    details = cloudfront.describe_function(Name=values['function_name'], Stage='LIVE')
    domain = urllib.parse.urlsplit(values['website_url']).netloc
    event = {
        'version': '1.0', 'context': {'eventType': 'viewer-response'},
        'viewer': {'ip': '198.51.100.1'},
        'request': {'method': 'GET', 'uri': '/index.html', 'headers': {'host': {'value': domain}}, 'cookies': {}, 'querystring': {}},
        'response': {'statusCode': 200, 'statusDescription': 'OK', 'headers': {}, 'cookies': {}}
    }
    result = cloudfront.test_function(Name=values['function_name'], IfMatch=details['ETag'], Stage='LIVE', EventObject=json.dumps(event).encode())['TestResult']
    if result.get('FunctionErrorMessage'):
        raise RuntimeError(result['FunctionErrorMessage'])
    output = json.loads(result['FunctionOutput'])
    output = output.get('response', output)
    cookie = output.get('cookies', {}).get('the_image', {})
    if output.get('statusCode') != 200 or domain not in cookie.get('attributes', '') or 'Secure' not in cookie.get('attributes', ''):
        raise RuntimeError('CloudFront Function test failed.')
    print('Published CloudFront Function test passed.', flush=True)


def read_url(url, method='GET'):
    with urllib.request.urlopen(urllib.request.Request(url, method=method), timeout=30) as response:
        return response.read(), response.headers, response.geturl()


def verify(values, config):
    body, headers, url = read_url(values['website_url'] + '/index.html')
    if b'config.js' not in body or headers.get('Cache-Control') != 'max-age=180':
        raise RuntimeError('CloudFront website or its 180-second cache headers are unavailable.')
    cookie = SimpleCookie()
    cookie.load(headers.get('Set-Cookie', ''))
    domain = urllib.parse.urlsplit(url).netloc
    if 'the_image' not in cookie or cookie['the_image']['domain'] != domain or not cookie['the_image']['secure']:
        raise RuntimeError('The random image cookie is missing or has the wrong domain.')
    content, _, _ = read_url(values['website_url'] + '/config.js')
    if config['api_id'].encode() not in content:
        raise RuntimeError('The cafe website points to the wrong API.')
    _, _, redirected = read_url(values['website_url'].replace('https:', 'http:'))
    if not redirected.startswith('https:'):
        raise RuntimeError('CloudFront did not redirect HTTP to HTTPS.')
    for endpoint in (values['application_url'] + '/beans.json', values['beans_api_url']):
        body, headers, _ = read_url(endpoint)
        if len(json.loads(body)) != 14 or headers.get('Cache-Status') not in ('From Memcached', 'Directly from RDS'):
            raise RuntimeError('The supplier application or cached beans API is unavailable.')
        if headers.get('Access-Control-Allow-Origin') != '*':
            raise RuntimeError('The beans API is missing CORS headers.')
    _, headers, _ = read_url(values['beans_api_url'])
    if headers.get('Cache-Status') != 'From Memcached':
        raise RuntimeError('The beans API is not serving data from Memcached.')
    _, headers, _ = read_url(values['beans_api_url'], 'OPTIONS')
    if headers.get('Access-Control-Allow-Origin') != '*':
        raise RuntimeError('API preflight CORS check failed.')
    body, _, _ = read_url(values['beans_api_url'].rsplit('/', 1)[0] + '/products')
    products = json.loads(body)
    items = products.get('product_item_arr', []) if isinstance(products, dict) else products
    if not isinstance(items, list) or len(items) < 26:
        raise RuntimeError('FoodProducts seed data is incomplete.')
    try:
        read_url(f"https://{config['site_bucket']}.s3.{config['region']}.amazonaws.com/index.html")
    except urllib.error.HTTPError as error:
        if error.code != 403:
            raise
    else:
        raise RuntimeError('S3 objects are still publicly accessible.')
    print('Office network: HTTPS website, both APIs, Memcached, private S3 and CORS verified.', flush=True)


def verify_cache(values):
    endpoint = values['website_url'] + '/index.html'
    images = set()
    allowed = {'apple_pie', 'apple_pie_slice', 'chocolate_chip_cupcake', 'strawberry_cupcake', 'blueberry_jelly_doughnut', 'plain_bagel'}
    headers = None
    for _ in range(16):
        _, headers, _ = read_url(endpoint)
        cookie = SimpleCookie()
        cookie.load(headers.get('Set-Cookie', ''))
        image = cookie['the_image'].value if 'the_image' in cookie else None
        if image not in allowed:
            raise RuntimeError('CloudFront returned an invalid random image cookie.')
        images.add(image)
    if len(images) < 2 or 'Hit from cloudfront' != headers.get('X-Cache'):
        raise RuntimeError('Random image responses or CloudFront cache hits could not be verified.')
    remaining = max(1, 183 - int(headers.get('Age', '0')))
    print(f'CloudFront cache hit and {len(images)} random images verified. Waiting {remaining}s to verify the 180-second expiry...', flush=True)
    deadline = time.monotonic() + remaining
    while time.monotonic() < deadline:
        print(f"Cache expiry check: {max(0, int(deadline - time.monotonic()))}s remaining...", flush=True)
        time.sleep(min(30, max(0, deadline - time.monotonic())))
    _, refreshed, _ = read_url(endpoint)
    if refreshed.get('X-Cache') not in ('RefreshHit from cloudfront', 'Miss from cloudfront') or int(refreshed.get('Age', '0')) > 3:
        raise RuntimeError('CloudFront did not refresh the object after its 180-second TTL.')
    _, cached, _ = read_url(endpoint)
    if cached.get('X-Cache') != 'Hit from cloudfront':
        raise RuntimeError('CloudFront did not cache the refreshed object.')
    print('CloudFront 180-second expiration, origin refresh and subsequent cache hit verified.', flush=True)


def main():
    environment = load_environment()
    session = aws_session(environment)
    config, outputs = discover(session, environment)
    guard_state(config)
    for key in ('site_bucket', 'api_id', 'api_root_id', 'application_url', 'client_cidr', 'preserved_bucket_statements'):
        environment['TF_VAR_' + key] = json.dumps(config[key]) if isinstance(config[key], (list, dict)) else config[key]
    environment['TF_IN_AUTOMATION'] = '1'
    environment['TF_INPUT'] = '0'
    terraform = find_terraform()
    def execute(*args, capture=False):
        result = subprocess.run([terraform, *args], cwd=ROOT, env=environment, check=True, text=True,
                                stdout=subprocess.PIPE if capture else None)
        return result.stdout if capture else None
    application_zip(config['repository_url'] + ':latest')
    print(f"Lab 9.2: account {config['account']}, IDE {config['ide_instance_id']}, office {config['client_cidr']}", flush=True)
    execute('init', '-no-color')
    execute('validate', '-no-color')
    imports(session, config, execute)
    migrate_origin_access(session, config, execute)
    from lab_ide import LabIDE
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        distribution = pool.submit(execute, 'apply', '-auto-approve', '-no-color',
            '-target=aws_cloudfront_distribution.website', '-target=aws_s3_bucket_policy.website',
            '-target=aws_wafv2_web_acl_association.api')
        remote = LabIDE(outputs['LabIDEURL'], outputs['LabIDEPassword'])
        try:
            if 'LAB92_PREPARED' not in remote.run(bundle(config, 'prepare'), timeout=1800):
                raise RuntimeError('Lab IDE preparation did not finish.')
        finally:
            remote.close()
        distribution.result()
    image = session.client('ecr').describe_images(repositoryName='cafe/node-web-app', imageIds=[{'imageTag': 'latest'}])['imageDetails'][0]
    application_zip(config['repository_url'] + '@' + image['imageDigest'])
    version = 'Lab92-' + hashlib.md5((TOOLS / 'application.zip').read_bytes()).hexdigest()
    versions = session.client('elasticbeanstalk').describe_application_versions(ApplicationName='MyNodeApp', VersionLabels=[version])['ApplicationVersions']
    state = json.loads(execute('show', '-json', capture=True))
    tracked = [item['values']['name'] for item in state.get('values', {}).get('root_module', {}).get('resources', [])
               if item['type'] == 'aws_elastic_beanstalk_application_version']
    environment['TF_VAR_create_application_version'] = 'true' if not versions or version in tracked else 'false'
    execute('apply', '-auto-approve', '-no-color')
    values = {key: value['value'] for key, value in json.loads(execute('output', '-json', capture=True)).items()}
    update_application(session, config, values)
    cloudfront = session.client('cloudfront')
    invalidation = cloudfront.create_invalidation(DistributionId=values['distribution_id'], InvalidationBatch={
        'Paths': {'Quantity': 1, 'Items': ['/*']}, 'CallerReference': str(time.time_ns())})['Invalidation']
    wait_for('CloudFront to publish the latest website files', lambda: cloudfront.get_invalidation(
        DistributionId=values['distribution_id'], Id=invalidation['Id'])['Invalidation']['Status'] == 'Completed')
    verify_oai_bucket_access(session, config, values)
    verify_function(session, values)
    deadline = time.monotonic() + 300
    while True:
        try:
            verify(values, config)
            break
        except Exception as error:
            if time.monotonic() > deadline:
                raise
            print(f'Waiting for website/API/WAF propagation: {error}', flush=True)
            time.sleep(10)
    config.update(website_url=values['website_url'], beans_api_url=values['beans_api_url'])
    remote = LabIDE(outputs['LabIDEURL'], outputs['LabIDEPassword'])
    try:
        if 'LAB92_BLOCKING_VERIFIED' not in remote.run(bundle(config, 'verify'), timeout=600):
            raise RuntimeError('Outside-office WAF checks did not finish.')
    finally:
        remote.close()
    verify_cache(values)
    print('\nLab 9.2 completed. CloudFront HTTPS, private S3, both WAF ACLs, random image cookies and 180-second caching verified.')
    print('Cafe: ' + values['website_url'])
    print('Coffee API: ' + values['beans_api_url'])
    print('Submit the lab in AWS Academy before ending the lab.')


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print(f'Lab 9.2 failed: {error}', file=sys.stderr)
        sys.exit(1)

