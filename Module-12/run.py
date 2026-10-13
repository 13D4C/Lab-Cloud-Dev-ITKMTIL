import argparse
import base64
import hashlib
import io
import ipaddress
import json
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile

from runtime import ROOT, TOOLS, aws_session, find_terraform, load_environment


def wait_for(description, operation, timeout=1800):
    deadline = time.monotonic() + timeout
    next_message = 0
    while time.monotonic() < deadline:
        value = operation()
        if value:
            return value
        if time.monotonic() >= next_message:
            print('Waiting for ' + description + '...', flush=True)
            next_message = time.monotonic() + 30
        time.sleep(10)
    raise TimeoutError('Timed out waiting for ' + description + '. Run the same command again to resume.')


def discover(session, environment):
    identity = session.client('sts').get_caller_identity()
    cf = session.client('cloudformation')
    candidates = [item for page in cf.get_paginator('describe_stacks').paginate() for item in page['Stacks']
                  if not item.get('ParentId') and not item['StackName'].startswith('awseb-')
                  and item['StackName'].startswith(environment.get('LAB_STACK_NAME', 'c'))
                  and not item['StackStatus'].startswith('DELETE')]
    if len(candidates) != 1:
        raise RuntimeError('Expected one AWS Academy lab stack. Set LAB_STACK_NAME in .env if there are several.')
    name = candidates[0]['StackName']

    def ready():
        item = cf.describe_stacks(StackName=name)['Stacks'][0]
        if any(word in item['StackStatus'] for word in ('FAILED', 'ROLLBACK', 'DELETE')):
            raise RuntimeError('AWS Academy stack is ' + item['StackStatus'])
        return item if item['StackStatus'] in ('CREATE_COMPLETE', 'UPDATE_COMPLETE') else None

    stack = wait_for('AWS Academy to finish creating Lab 12', ready)
    resources = [item for page in cf.get_paginator('list_stack_resources').paginate(StackName=name)
                 for item in page['StackResourceSummaries']]
    resource = {item['LogicalResourceId']: item['PhysicalResourceId'] for item in resources}
    expected = ('S3Bucket', 'ApiGatewayRestApi', 'CafeCloudfront', 'CafeOAI', 'MyStateMachine', 'SNSForSendingEmail')
    if not all(key in resource for key in expected):
        raise RuntimeError('These credentials do not belong to the expected Module 12 Cognito lab.')
    outputs = {item['OutputKey']: item['OutputValue'] for item in stack.get('Outputs', [])}

    def nested_outputs(stack_id):
        item = cf.describe_stacks(StackName=stack_id)['Stacks'][0]
        result = {x['OutputKey']: x['OutputValue'] for x in item.get('Outputs', [])}
        if not (result.get('LabIDEPassword') or result.get('RandomCharacters')):
            for page in cf.get_paginator('list_stack_resources').paginate(StackName=stack_id):
                for child in page['StackResourceSummaries']:
                    if child['ResourceType'] == 'AWS::CloudFormation::Stack':
                        result.update(nested_outputs(child['PhysicalResourceId']))
        return result

    if not outputs.get('LabIDEPassword'):
        nested = nested_outputs(resource['VSCodeStack'])
        outputs['LabIDEPassword'] = nested.get('LabIDEPassword', nested.get('RandomCharacters'))
        outputs.setdefault('LabIDEURL', nested.get('LabWorkspaceURL'))
    if not outputs.get('LabIDEURL') or not outputs.get('LabIDEPassword'):
        raise RuntimeError('LabIDEURL or LabIDEPassword is missing from the lab stack outputs.')
    api = session.client('apigateway')
    api_id = resource['ApiGatewayRestApi']
    paths = {x['path']: x for page in api.get_paginator('get_resources').paginate(restApiId=api_id) for x in page['items']}
    report_id = paths['/create_report']['id']
    method = api.get_method(restApiId=api_id, resourceId=report_id, httpMethod='POST')
    responses = {}
    for verb in ('POST', 'OPTIONS'):
        value = api.get_integration_response(restApiId=api_id, resourceId=report_id, httpMethod=verb, statusCode='200')
        responses[verb] = {key: value.get(camel, fallback) for key, camel, fallback in (
            ('selection_pattern', 'selectionPattern', None), ('response_templates', 'responseTemplates', {}),
            ('response_parameters', 'responseParameters', {}))}
        responses[verb]['response_templates'] = {k: v or '' for k, v in responses[verb]['response_templates'].items()}
    distribution = session.client('cloudfront').get_distribution(Id=resource['CafeCloudfront'])['Distribution']
    if not any(resource['S3Bucket'] in x['DomainName'] for x in distribution['DistributionConfig']['Origins']['Items']):
        raise RuntimeError('The café CloudFront distribution points to a different bucket.')
    cluster = session.client('rds').describe_db_clusters(DBClusterIdentifier='supplierdb')['DBClusters'][0]
    application = session.client('elasticbeanstalk').describe_environments(
        ApplicationName='MyNodeApp', EnvironmentNames=['MyEnv'], IncludeDeleted=False)['Environments'][0]
    repository = session.client('ecr').describe_repositories(repositoryNames=['cafe/node-web-app'])['repositories'][0]
    cache = session.client('elasticache').describe_cache_clusters(CacheClusterId='memcached', ShowCacheNodeInfo=True)['CacheClusters'][0]
    if environment.get('CLIENT_CIDR'):
        cidr = environment['CLIENT_CIDR']
    else:
        with urllib.request.urlopen('https://checkip.amazonaws.com', timeout=30) as response:
            cidr = response.read().decode().strip() + '/32'
    network = ipaddress.ip_network(cidr, strict=True)
    if network.version != 4 or network.prefixlen != 32:
        raise RuntimeError('CLIENT_CIDR must be your public IPv4 address followed by /32.')
    email = environment.get('REPORT_EMAIL')
    if not email or email == 'you@example.com':
        email = identity['Arn'].rsplit('=', 1)[-1]
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email):
        raise RuntimeError('Set report_email in Module-12/.env to the email address that will receive reports.')
    s3 = session.client('s3')
    try:
        statements = json.loads(s3.get_bucket_policy(Bucket=resource['S3Bucket'])['Policy'])['Statement']
    except s3.exceptions.ClientError as error:
        if error.response['Error']['Code'] != 'NoSuchBucketPolicy':
            raise
        statements = []
    statements = [x for x in statements if x.get('Sid') not in ('AllowOAIAccess', 'DenyOneObjectIfRequestNotSigned')]
    statements.extend([
        {'Sid': 'AllowOAIAccess', 'Effect': 'Allow',
         'Principal': {'AWS': 'arn:aws:iam::cloudfront:user/CloudFront Origin Access Identity ' + resource['CafeOAI']},
         'Action': 's3:GetObject', 'Resource': 'arn:aws:s3:::' + resource['S3Bucket'] + '/*'},
        {'Sid': 'DenyOneObjectIfRequestNotSigned', 'Effect': 'Deny', 'Principal': '*', 'Action': 's3:GetObject',
         'Resource': 'arn:aws:s3:::' + resource['S3Bucket'] + '/report.html',
         'Condition': {'StringNotEquals': {'s3:authtype': 'REST-QUERY-STRING'}}}
    ])
    config = dict(account_id=identity['Account'], stack_name=name, region=session.region_name,
                  site_bucket=resource['S3Bucket'], bucket_statements=statements,
                  api_id=api_id, api_root_id=paths['/']['id'], report_resource_id=report_id,
                  cloudfront_id=resource['CafeCloudfront'], cloudfront_domain=distribution['DomainName'],
                  application_url='http://' + application['CNAME'], environment_id=application['EnvironmentId'],
                  repository_url=repository['repositoryUri'], database_endpoint=cluster['Endpoint'],
                  cache_endpoint=cache['ConfigurationEndpoint']['Address'], client_cidr=cidr, report_email=email,
                  topic_arn=resource['SNSForSendingEmail'], state_machine_arn=resource['MyStateMachine'],
                  report_method=dict(api_key_required=method.get('apiKeyRequired', False),
                                     request_parameters=method.get('requestParameters', {}),
                                     request_models=method.get('requestModels', {}),
                                     request_validator_id=method.get('requestValidatorId')),
                  report_responses=responses, image_tag='lab12-node22-ec2e97d06128526d')
    return config, outputs


def guard_state(config):
    TOOLS.mkdir(exist_ok=True)
    identity = {key: config[key] for key in ('account_id', 'stack_name', 'site_bucket')}
    path = TOOLS / 'lab.json'
    if path.exists() and json.loads(path.read_text()) != identity:
        archive = TOOLS / 'previous-labs' / str(time.time_ns())
        archive.mkdir(parents=True)
        for state in ROOT.glob('terraform.tfstate*'):
            shutil.move(str(state), archive / state.name)
        for filename in ('login.json', 'prepared.json'):
            if (TOOLS / filename).exists():
                shutil.move(str(TOOLS / filename), archive / filename)
        print('New lab detected; previous local state archived.', flush=True)
    if not path.exists() and (ROOT / 'terraform.tfstate').exists():
        raise RuntimeError('Existing state has no lab identity. Restore .tools/lab.json before continuing.')
    path.write_text(json.dumps(identity))


def login_password(environment):
    path = TOOLS / 'login.json'
    password = environment.get('FRANK_PASSWORD')
    if not password and path.exists():
        password = json.loads(path.read_text())['password']
    password = password or ('Cafe!' + secrets.token_urlsafe(18) + '9aA')
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(json.dumps({'username': 'frank', 'password': password}, indent=2) + '\n')
    return password


def prepare_subscription(session, config):
    sns = session.client('sns')
    def subscriptions():
        return [x for page in sns.get_paginator('list_subscriptions_by_topic').paginate(TopicArn=config['topic_arn'])
                for x in page['Subscriptions'] if x['Protocol'] == 'email' and x['Endpoint'].lower() == config['report_email'].lower()]
    values = subscriptions()
    if not values:
        sns.subscribe(TopicArn=config['topic_arn'], Protocol='email', Endpoint=config['report_email'], ReturnSubscriptionArn=True)
        print('SNS confirmation email sent to ' + config['report_email'] + '. Click Confirm subscription once; this command continues automatically.', flush=True)
    elif values[0]['SubscriptionArn'] == 'PendingConfirmation':
        print('Confirm the pending AWS SNS email sent to ' + config['report_email'] + '.', flush=True)
    return lambda: next((x for x in subscriptions() if x['SubscriptionArn'].startswith('arn:')), None)


def bundle(config):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        archive.add(ROOT / 'deploy.py', arcname='.lab12/deploy.py')
        data = json.dumps(dict(config, account=config['account_id'])).encode()
        entry = tarfile.TarInfo('.lab12/config.json')
        entry.mode = 0o600
        entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))
    payload = base64.b64encode(output.getvalue()).decode()
    return "set -euo pipefail\ncd /home/ec2-user/environment\nmkdir -p .lab12\nbase64 -d <<'LAB12_BUNDLE' | tar xz\n" + payload + "\nLAB12_BUNDLE\nif ! python3.12 -m pip --version >/dev/null 2>&1; then sudo dnf install -y python3.12 python3.12-pip; fi\npython3.12 -u .lab12/deploy.py\n"


def prepare_application(session, config, outputs):
    marker = TOOLS / 'prepared.json'
    fingerprint = hashlib.sha256((ROOT / 'deploy.py').read_bytes()).hexdigest()
    identity = {'stack': config['stack_name'], 'fingerprint': fingerprint}
    if marker.exists() and json.loads(marker.read_text()) == identity:
        print('Reusing prepared website, database and application image.', flush=True)
    else:
        from lab_ide import LabIDE
        remote = LabIDE(outputs['LabIDEURL'], outputs['LabIDEPassword'])
        try:
            if 'LAB12_PREPARED' not in remote.run(bundle(config), timeout=2400):
                raise RuntimeError('Lab application preparation did not finish.')
        finally:
            remote.close()
        marker.write_text(json.dumps(identity))
    ecr = session.client('ecr')
    image = ecr.describe_images(repositoryName='cafe/node-web-app', imageIds=[{'imageTag': config['image_tag']}])['imageDetails'][0]
    descriptor = {'AWSEBDockerrunVersion': '1', 'Image': {'Name': config['repository_url'] + '@' + image['imageDigest'], 'Update': 'true'},
                  'Ports': [{'ContainerPort': 3000}]}
    data = json.dumps(descriptor, sort_keys=True).encode()
    version = 'Lab12-' + hashlib.sha256(data).hexdigest()[:24]
    key = 'lab12/' + version + '.zip'
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        entry = zipfile.ZipInfo('Dockerrun.aws.json', date_time=(2020, 1, 1, 0, 0, 0))
        archive.writestr(entry, data)
    eb = session.client('elasticbeanstalk')
    if not eb.describe_application_versions(ApplicationName='MyNodeApp', VersionLabels=[version])['ApplicationVersions']:
        session.client('s3').put_object(Bucket=config['site_bucket'], Key=key, Body=output.getvalue())
        eb.create_application_version(ApplicationName='MyNodeApp', VersionLabel=version,
                                      SourceBundle={'S3Bucket': config['site_bucket'], 'S3Key': key})
    settings = eb.describe_configuration_settings(ApplicationName='MyNodeApp', EnvironmentName='MyEnv')['ConfigurationSettings'][0]['OptionSettings']
    current = {x['OptionName']: x.get('Value', '') for x in settings if x['Namespace'] == 'aws:elasticbeanstalk:application:environment'}
    desired = {'APP_DB_HOST': config['database_endpoint'], 'MEMC_HOST': config['cache_endpoint'] + ':11211'}
    item = wait_for('the provided MyEnv environment', lambda: next((x for x in eb.describe_environments(EnvironmentIds=[config['environment_id']])['Environments'] if x['Status'] == 'Ready'), None))
    if item.get('VersionLabel') != version or any(current.get(k) != v for k, v in desired.items()):
        eb.update_environment(EnvironmentId=config['environment_id'], VersionLabel=version, OptionSettings=[
            {'Namespace': 'aws:elasticbeanstalk:application:environment', 'OptionName': k, 'Value': v} for k, v in desired.items()])
    wait_for('MyEnv to deploy the café application', lambda: next((x for x in eb.describe_environments(EnvironmentIds=[config['environment_id']])['Environments'] if x['Status'] == 'Ready' and x.get('VersionLabel') == version), None))


def import_existing(session, config, execute):
    tracked = set(execute('state', 'list', capture=True).splitlines()) if (ROOT / 'terraform.tfstate').exists() else set()
    def add(address, identifier):
        if address not in tracked:
            execute('import', '-input=false', '-no-color', address, identifier)
            tracked.add(address)
    api = session.client('apigateway')
    api_id = config['api_id']
    report_id = config['report_resource_id']
    add('aws_s3_bucket_policy.website', config['site_bucket'])
    add('aws_api_gateway_stage.prod', api_id + '/prod')
    add('aws_api_gateway_method.report', f'{api_id}/{report_id}/POST')
    for verb in config['report_responses']:
        add('aws_api_gateway_integration_response.report["' + verb + '"]', f'{api_id}/{report_id}/{verb}/200')
    paths = {x['path']: x for page in api.get_paginator('get_resources').paginate(restApiId=api_id) for x in page['items']}
    if '/bean_products' in paths:
        bean = paths['/bean_products']
        add('aws_api_gateway_resource.beans', f"{api_id}/{bean['id']}")
        for verb, suffix in (('GET', 'beans'), ('OPTIONS', 'beans_options')):
            if verb in bean.get('resourceMethods', {}):
                add('aws_api_gateway_method.' + suffix, f"{api_id}/{bean['id']}/{verb}")
                try:
                    method = api.get_method(restApiId=api_id, resourceId=bean['id'], httpMethod=verb)
                    if method.get('methodIntegration'):
                        add('aws_api_gateway_integration.' + suffix, f"{api_id}/{bean['id']}/{verb}")
                    if verb == 'OPTIONS' and '200' in method.get('methodResponses', {}):
                        add('aws_api_gateway_method_response.beans_options', f"{api_id}/{bean['id']}/OPTIONS/200")
                    if verb == 'OPTIONS' and '200' in method.get('methodIntegration', {}).get('integrationResponses', {}):
                        add('aws_api_gateway_integration_response.beans_options', f"{api_id}/{bean['id']}/OPTIONS/200")
                except api.exceptions.NotFoundException:
                    pass
    for x in api.get_authorizers(restApiId=api_id)['items']:
        if x['name'] == 'cafe_lockdown':
            add('aws_api_gateway_authorizer.cafe', api_id + '/' + x['id'])
    waf = session.client('wafv2')
    for key, scope in (('global', 'CLOUDFRONT'), ('regional', 'REGIONAL')):
        ip_name = 'office' if key == 'global' else 'office_regional'
        acl_name = 'cafe-website-office-only-during-dev' if key == 'global' else 'website-api-gw-office-only-during-dev'
        for x in waf.list_ip_sets(Scope=scope)['IPSets']:
            if x['Name'] == ip_name:
                add('aws_wafv2_ip_set.office["' + key + '"]', x['Id'] + '/' + ip_name + '/' + scope)
        for x in waf.list_web_acls(Scope=scope)['WebACLs']:
            if x['Name'] == acl_name:
                add('aws_wafv2_web_acl.office["' + key + '"]', x['Id'] + '/' + acl_name + '/' + scope)
    arn = f"arn:aws:apigateway:{config['region']}::/restapis/{api_id}/stages/prod"
    association = waf.get_web_acl_for_resource(ResourceArn=arn).get('WebACL')
    if association:
        add('aws_wafv2_web_acl_association.api', association['ARN'] + ',' + arn)
    cognito = session.client('cognito-idp')
    pools = [x for page in cognito.get_paginator('list_user_pools').paginate(MaxResults=60) for x in page['UserPools'] if x['Name'] == 'the_cafe']
    if len(pools) > 1:
        raise RuntimeError('Multiple the_cafe user pools exist; select the intended pool before retrying.')
    if pools:
        pool_id = pools[0]['Id']
        add('aws_cognito_user_pool.cafe', pool_id)
        pool = cognito.describe_user_pool(UserPoolId=pool_id)['UserPool']
        if pool.get('Domain'):
            add('aws_cognito_user_pool_domain.cafe', pool['Domain'])
        for x in cognito.list_user_pool_clients(UserPoolId=pool_id, MaxResults=60)['UserPoolClients']:
            if x['ClientName'] == 'the_cafe_app_client':
                add('aws_cognito_user_pool_client.cafe', pool_id + '/' + x['ClientId'])
        for x in cognito.list_resource_servers(UserPoolId=pool_id, MaxResults=50)['ResourceServers']:
            if x['Name'] == 'cafe_resource_server':
                add('aws_cognito_resource_server.cafe', pool_id + '|' + x['Identifier'])
        try:
            cognito.admin_get_user(UserPoolId=pool_id, Username='frank')
            add('aws_cognito_user.frank', pool_id + '/frank')
        except cognito.exceptions.UserNotFoundException:
            pass


def apply_plan(execute):
    execute('plan', '-input=false', '-no-color', '-out=.tools/apply.tfplan')
    plan = json.loads(execute('show', '-json', '.tools/apply.tfplan', capture=True))
    for change in plan.get('resource_changes', []):
        if 'delete' in change['change']['actions'] and change['type'] != 'aws_api_gateway_deployment':
            raise RuntimeError('Refusing an unexpected resource replacement/deletion: ' + change['address'])
    execute('apply', '-input=false', '-no-color', '.tools/apply.tfplan')


def attach_cloudfront(session, config, values):
    cf = session.client('cloudfront')
    item = cf.get_distribution_config(Id=config['cloudfront_id'])
    if item['DistributionConfig'].get('WebACLId') != values['cloudfront_acl_arn']:
        item['DistributionConfig']['WebACLId'] = values['cloudfront_acl_arn']
        cf.update_distribution(Id=config['cloudfront_id'], IfMatch=item['ETag'], DistributionConfig=item['DistributionConfig'])
    invalidation = cf.create_invalidation(DistributionId=config['cloudfront_id'], InvalidationBatch={
        'Paths': {'Quantity': 1, 'Items': ['/*']}, 'CallerReference': 'lab12-' + str(time.time_ns())})['Invalidation']['Id']
    wait_for('CloudFront to deploy the website and office access rules', lambda: cf.get_distribution(Id=config['cloudfront_id'])['Distribution']['Status'] == 'Deployed')
    wait_for('CloudFront to refresh the website files', lambda: cf.get_invalidation(DistributionId=config['cloudfront_id'], Id=invalidation)['Invalidation']['Status'] == 'Completed')


def http(url, method='GET', data=None, headers=None):
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers)


def verify(session, config, values, password):
    cognito = session.client('cognito-idp')
    token = cognito.initiate_auth(ClientId=values['client_id'], AuthFlow='USER_PASSWORD_AUTH',
                                  AuthParameters={'USERNAME': 'frank', 'PASSWORD': password})['AuthenticationResult']['IdToken']
    url = values['create_report_url']
    def unauthorized():
        status, data, _ = http(url, 'POST', b'{}', {'Content-Type': 'application/json'})
        return status == 401 and json.loads(data).get('message') == 'Unauthorized'
    wait_for('the deployed Cognito authorizer to reject unsigned requests', unauthorized, timeout=600)
    status, _, _ = http(url, 'POST', b'{}', {'Content-Type': 'application/json', 'Authorization': 'Bearer invalid'})
    if status != 401:
        raise RuntimeError('An invalid authorization token was not rejected.')
    status, _, headers = http(url, 'OPTIONS', headers={'Origin': values['website_url'], 'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'authorization,content-type'})
    if status != 200 or headers.get('Access-Control-Allow-Origin') != values['website_url']:
        raise RuntimeError('The report API CORS preflight failed.')
    status, data, _ = http(url, 'POST', b'{}', {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
    if status != 200 or not json.loads(data).get('executionArn'):
        raise RuntimeError('An authenticated report request failed: ' + str(status) + ' ' + data.decode())
    steps = session.client('stepfunctions')
    result = steps.start_sync_execution(stateMachineArn=config['state_machine_arn'], input='{}')
    if result['status'] != 'SUCCEEDED':
        raise RuntimeError('The report workflow failed: ' + str(result.get('error')) + ' ' + str(result.get('cause')))
    invocation = session.client('lambda').invoke(FunctionName='GeneratePresignedURL', Payload=b'{}')
    payload = invocation['Payload'].read().decode()
    if invocation.get('FunctionError'):
        raise RuntimeError('Signed URL generation failed: ' + payload)
    links = re.findall(r'https://[^\s\"<>]+', payload)
    report_link = next((x for x in links if 'report.html' in x and 'X-Amz-' in x), None)
    if not report_link:
        raise RuntimeError('The report workflow returned no signed report URL.')
    status, data, _ = http(report_link)
    if status != 200 or b'<html' not in data.lower():
        raise RuntimeError('The generated signed report could not be opened.')
    status, _, _ = http(values['website_url'] + '/report.html')
    if status != 403:
        raise RuntimeError('The report must require a signed URL.')
    for path in ('/', '/config.js', '/callback.html', '/sign_out.html'):
        status, data, _ = http(values['website_url'] + path)
        if status != 200:
            raise RuntimeError('Website verification failed for ' + path)
        if path == '/config.js' and values['client_id'].encode() not in data:
            raise RuntimeError('CloudFront is serving an outdated Cognito configuration.')
    status, data, _ = http(values['login_url'])
    if status != 200 or b'username' not in data.lower():
        raise RuntimeError('The Cognito hosted login page is unavailable.')
    print('Verified: Cognito login, required Authorization header, rejected missing/invalid tokens, CORS, authenticated report execution and private signed report.', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    environment = load_environment()
    session = aws_session(environment)
    config, outputs = discover(session, environment)
    print('Lab 12 account: ' + config['account_id'] + '; stack: ' + config['stack_name'], flush=True)
    guard_state(config)
    password = login_password(environment)
    keys = ('account_id', 'site_bucket', 'bucket_statements', 'api_id', 'api_root_id', 'report_resource_id',
            'cloudfront_domain', 'application_url', 'client_cidr', 'report_email', 'report_method', 'report_responses')
    for key in keys:
        environment['TF_VAR_' + key] = config[key] if isinstance(config[key], str) else json.dumps(config[key])
    environment['TF_VAR_frank_password'] = password
    environment['TF_IN_AUTOMATION'] = '1'
    environment['AWS_PAGER'] = ''
    terraform = find_terraform()
    def execute(*arguments, capture=False):
        result = subprocess.run([terraform, *arguments], cwd=ROOT, env=environment, check=True, text=True,
                                stdout=subprocess.PIPE if capture else None)
        return result.stdout if capture else None
    execute('init', '-input=false', '-no-color')
    execute('fmt', '-check', '-no-color')
    execute('validate', '-no-color')
    if not args.verify_only:
        subscription_ready = prepare_subscription(session, config)
        prepare_application(session, config, outputs)
        import_existing(session, config, execute)
        apply_plan(execute)
        values = {k: v['value'] for k, v in json.loads(execute('output', '-json', capture=True)).items()}
        attach_cloudfront(session, config, values)
        wait_for('SNS email confirmation for ' + config['report_email'], subscription_ready, timeout=1200)
    else:
        values = {k: v['value'] for k, v in json.loads(execute('output', '-json', capture=True)).items()}
    verify(session, config, values, password)
    print('Lab 12 infrastructure and report tests completed. Submit in AWS Academy to record your grade.')
    print('Website: ' + values['website_url'])
    print('Username: frank')
    print('Password saved in: ' + str(TOOLS / 'login.json'))


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print('Lab 12 failed: ' + str(error), file=sys.stderr)
        sys.exit(1)
