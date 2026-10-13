import argparse
import base64
import hashlib
import io
import ipaddress
import json
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
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

    stack = wait_for('AWS Academy to finish creating Lab 13', ready)
    resources = [item for page in cf.get_paginator('list_stack_resources').paginate(StackName=name)
                 for item in page['StackResourceSummaries']]
    resource = {item['LogicalResourceId']: item['PhysicalResourceId'] for item in resources}
    expected = ('S3Bucket', 'ApiGatewayRestApi', 'CafeCloudfront', 'CafeOAI', 'S3PipelineBucket', 'RoleForCodepipeline', 'CognitoUserPool', 'CognitoUserPoolClient', 'SNSForSendingEmail')
    if not all(key in resource for key in expected):
        raise RuntimeError('These credentials do not belong to the expected Module 13 CI/CD lab.')
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
        raise RuntimeError('Set report_email in Module-13/.env to the email address that will receive reports.')
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
    cognito = session.client('cognito-idp')
    pool = cognito.describe_user_pool(UserPoolId=resource['CognitoUserPool'])['UserPool']
    if not pool.get('Domain'):
        raise RuntimeError('The provided Cognito pool must have its hosted login domain.')
    role = session.client('iam').get_role(RoleName=resource['RoleForCodepipeline'])['Role']
    author = environment.get('COMMIT_AUTHOR')
    if not author or author == 'Your Name':
        result = subprocess.run(['git', 'config', 'user.name'], capture_output=True, text=True) if shutil.which('git') else None
        author = result.stdout.strip() if result and result.returncode == 0 else email.split('@')[0]
    config = dict(account_id=identity['Account'], stack_name=name, region=session.region_name,
                  site_bucket=resource['S3Bucket'], bucket_statements=statements,
                  artifact_bucket=resource['S3PipelineBucket'], pipeline_role_arn=role['Arn'],
                  api_id=api_id, api_root_id=paths['/']['id'],
                  cloudfront_id=resource['CafeCloudfront'], cloudfront_domain=distribution['DomainName'],
                  application_url='http://' + application['CNAME'], environment_id=application['EnvironmentId'],
                  repository_url=repository['repositoryUri'], database_endpoint=cluster['Endpoint'],
                  cache_endpoint=cache['ConfigurationEndpoint']['Address'], client_cidr=cidr, report_email=email,
                  topic_arn=resource['SNSForSendingEmail'], cognito_pool_id=pool['Id'],
                  cognito_client_id=resource['CognitoUserPoolClient'], cognito_domain=pool['Domain'],
                  commit_author=author, image_tag='lab13-node22-94a9dd242b57d01d')
    config['distribution_id'] = config['cloudfront_id']
    config['website_url'] = 'https://' + config['cloudfront_domain']
    config['login_url'] = f"https://{config['cognito_domain']}.auth.{config['region']}.amazoncognito.com/login?" + urllib.parse.urlencode({'client_id': config['cognito_client_id'], 'response_type': 'token', 'scope': 'email openid', 'redirect_uri': config['website_url'] + '/callback.html'})
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
        for filename in ('progress.json', 'prepared.json'):
            if (TOOLS / filename).exists():
                shutil.move(str(TOOLS / filename), archive / filename)
        print('New lab detected; previous local state archived.', flush=True)
    if not path.exists() and (ROOT / 'terraform.tfstate').exists():
        raise RuntimeError('Existing state has no lab identity. Restore .tools/lab.json before continuing.')
    path.write_text(json.dumps(identity))


def prepare_subscription(session, config):
    sns = session.client('sns')
    existing = [item for page in sns.get_paginator('list_subscriptions_by_topic').paginate(TopicArn=config['topic_arn'])
                for item in page['Subscriptions'] if item['Protocol'] == 'email'
                and item['Endpoint'].lower() == config['report_email'].lower()]
    if not existing:
        sns.subscribe(TopicArn=config['topic_arn'], Protocol='email', Endpoint=config['report_email'], ReturnSubscriptionArn=True)
        print('Confirm the AWS Notifications email sent to ' + config['report_email'] + '.', flush=True)
    def confirmed():
        return next((item['SubscriptionArn'] for page in sns.get_paginator('list_subscriptions_by_topic').paginate(TopicArn=config['topic_arn'])
                     for item in page['Subscriptions'] if item['Protocol'] == 'email'
                     and item['Endpoint'].lower() == config['report_email'].lower()
                     and item['SubscriptionArn'] != 'PendingConfirmation'), None)
    return confirmed


def bundle(config, action):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        archive.add(ROOT / 'deploy.py', arcname='.lab13/deploy.py')
        data = json.dumps(dict(config, account=config['account_id'])).encode()
        entry = tarfile.TarInfo('.lab13/config.json')
        entry.mode = 0o600
        entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))
    payload = base64.b64encode(output.getvalue()).decode()
    return "set -euo pipefail\nexport PIP_PROGRESS_BAR=off\ncd /home/ec2-user/environment\nmkdir -p .lab13\nbase64 -d <<'LAB13_BUNDLE' | tar xz\n" + payload + "\nLAB13_BUNDLE\nif ! python3.12 -m pip --version >/dev/null 2>&1; then sudo dnf install -y python3.12 python3.12-pip; fi\npython3.12 -u .lab13/deploy.py " + action + '\n'


def remote_action(config, outputs, action):
    from lab_ide import LabIDE
    remote = LabIDE(outputs['LabIDEURL'], outputs['LabIDEPassword'])
    try:
        result = remote.run(bundle(config, action), timeout=2400)
    finally:
        remote.close()
    if action == 'prepare':
        if 'LAB13_PREPARED' not in result:
            raise RuntimeError('Application preparation did not finish.')
        return
    matches = re.findall(r'LAB13_GIT_RESULT (\{[^\r\n]+\})', result)
    if not matches:
        raise RuntimeError('The Git operation did not return a verified commit.')
    details = json.loads(matches[-1])
    if not details['clean']:
        raise RuntimeError('The Lab IDE Git checkout has uncommitted changes.')
    return details


def prepare_application(session, config, outputs):
    marker = TOOLS / 'prepared.json'
    fingerprint = hashlib.sha256((ROOT / 'deploy.py').read_bytes()).hexdigest()
    identity = {'stack': config['stack_name'], 'fingerprint': fingerprint, 'client_cidr': config['client_cidr'], 'email': config['report_email']}
    if not marker.exists() or json.loads(marker.read_text()) != identity:
        remote_action(config, outputs, 'prepare')
        marker.write_text(json.dumps(identity))
    else:
        print('Reusing the prepared application, database and office access configuration.', flush=True)
    image = session.client('ecr').describe_images(repositoryName='cafe/node-web-app', imageIds=[{'imageTag': config['image_tag']}])['imageDetails'][0]
    document = {'AWSEBDockerrunVersion': '1', 'Image': {'Name': config['repository_url'] + '@' + image['imageDigest'], 'Update': 'true'},
                'Ports': [{'ContainerPort': 3000}]}
    data = json.dumps(document, sort_keys=True).encode()
    version = 'Lab13-' + hashlib.sha256(data).hexdigest()[:24]
    key = 'lab13/' + version + '.zip'
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        entry = zipfile.ZipInfo('Dockerrun.aws.json', date_time=(2020, 1, 1, 0, 0, 0))
        archive.writestr(entry, data)
    eb = session.client('elasticbeanstalk')
    if not eb.describe_application_versions(ApplicationName='MyNodeApp', VersionLabels=[version])['ApplicationVersions']:
        session.client('s3').put_object(Bucket=config['site_bucket'], Key=key, Body=output.getvalue())
        eb.create_application_version(ApplicationName='MyNodeApp', VersionLabel=version, SourceBundle={'S3Bucket': config['site_bucket'], 'S3Key': key})
    item = wait_for('the provided MyEnv environment', lambda: next((x for x in eb.describe_environments(EnvironmentIds=[config['environment_id']])['Environments'] if x['Status'] == 'Ready'), None))
    settings = eb.describe_configuration_settings(ApplicationName='MyNodeApp', EnvironmentName='MyEnv')['ConfigurationSettings'][0]['OptionSettings']
    current = {x['OptionName']: x.get('Value', '') for x in settings if x['Namespace'] == 'aws:elasticbeanstalk:application:environment'}
    desired = {'APP_DB_HOST': config['database_endpoint'], 'MEMC_HOST': config['cache_endpoint'] + ':11211'}
    if item.get('VersionLabel') != version or any(current.get(key) != value for key, value in desired.items()):
        eb.update_environment(EnvironmentId=config['environment_id'], VersionLabel=version, OptionSettings=[
            {'Namespace': 'aws:elasticbeanstalk:application:environment', 'OptionName': key, 'Value': value} for key, value in desired.items()])
    wait_for('MyEnv to deploy the café application', lambda: next((x for x in eb.describe_environments(EnvironmentIds=[config['environment_id']])['Environments'] if x['Status'] == 'Ready' and x.get('VersionLabel') == version), None))
    cf = session.client('cloudfront')
    wait_for('the provided café CloudFront distribution', lambda: cf.get_distribution(Id=config['cloudfront_id'])['Distribution']['Status'] == 'Deployed')


def import_existing(session, config, execute, pipeline_exists):
    tracked = set(execute('state', 'list', capture=True).splitlines()) if (ROOT / 'terraform.tfstate').exists() else set()
    def add(address, identifier):
        if address not in tracked:
            execute('import', '-input=false', '-no-color', address, identifier)
            tracked.add(address)
    s3 = session.client('s3')
    try:
        s3.get_bucket_policy(Bucket=config['site_bucket'])
        add('aws_s3_bucket_policy.website', config['site_bucket'])
    except s3.exceptions.ClientError as error:
        if error.response['Error']['Code'] != 'NoSuchBucketPolicy':
            raise
    codecommit = session.client('codecommit')
    try:
        codecommit.get_repository(repositoryName='front_end_website')
        add('aws_codecommit_repository.website', 'front_end_website')
    except codecommit.exceptions.RepositoryDoesNotExistException:
        pass
    if pipeline_exists:
        add('aws_codepipeline.website[0]', 'cafe_website_front_end_pipeline')
    api = session.client('apigateway')
    paths = [item for page in api.get_paginator('get_resources').paginate(restApiId=config['api_id']) for item in page['items']]
    for item in paths:
        if item['path'] != '/bean_products':
            continue
        prefix = config['api_id'] + '/' + item['id']
        add('aws_api_gateway_resource.beans', prefix)
        for verb, suffix in (('GET', 'beans'), ('OPTIONS', 'cors')):
            if verb not in item.get('resourceMethods', {}):
                continue
            add('aws_api_gateway_method.' + suffix, prefix + '/' + verb)
            method = api.get_method(restApiId=config['api_id'], resourceId=item['id'], httpMethod=verb)
            if method.get('methodIntegration'):
                add('aws_api_gateway_integration.' + suffix, prefix + '/' + verb)
            if verb == 'OPTIONS':
                if '200' in method.get('methodResponses', {}):
                    add('aws_api_gateway_method_response.cors', prefix + '/OPTIONS/200')
                if '200' in method.get('methodIntegration', {}).get('integrationResponses', {}):
                    add('aws_api_gateway_integration_response.cors', prefix + '/OPTIONS/200')
    add('aws_api_gateway_stage.prod', config['api_id'] + '/prod')


def apply_plan(execute):
    execute('plan', '-input=false', '-no-color', '-out=.tools/apply.tfplan')
    plan = json.loads(execute('show', '-json', '.tools/apply.tfplan', capture=True))
    for item in plan.get('resource_changes', []):
        if 'delete' in item['change']['actions']:
            raise RuntimeError('The plan would delete or replace lab resources: ' + item['address'])
    execute('apply', '-input=false', '-no-color', '.tools/apply.tfplan')


def progress(config):
    path = TOOLS / 'progress.json'
    identity = {key: config[key] for key in ('account_id', 'stack_name')}
    state = json.loads(path.read_text()) if path.exists() else {'identity': identity}
    if state['identity'] != identity:
        raise RuntimeError('The recorded commits belong to another lab.')
    def save():
        target = path.with_suffix('.tmp')
        target.write_text(json.dumps(state, indent=2))
        target.replace(path)
    return state, save


TEST_PAGE = '<!DOCTYPE html>\n<html>\n    <head>\n    <title>Test page</title>\n    </head>\n    <body>\n        <h1>\n            This is a sample HTML page.\n        </h1>\n    </body>\n</html>\n'


def repository_state(session):
    codecommit = session.client('codecommit')
    try:
        head = codecommit.get_branch(repositoryName='front_end_website', branchName='main')['branch']['commitId']
    except (codecommit.exceptions.BranchDoesNotExistException, codecommit.exceptions.RepositoryDoesNotExistException):
        return None, 'empty'
    try:
        codecommit.get_file(repositoryName='front_end_website', commitSpecifier=head, filePath='index.html')
        return head, 'website'
    except codecommit.exceptions.FileDoesNotExistException:
        pass
    contents = codecommit.get_file(repositoryName='front_end_website', commitSpecifier=head, filePath='test.html')['fileContent'].decode()
    if contents == TEST_PAGE:
        return head, 'sample'
    if contents == TEST_PAGE.replace('Test page', 'Best test page ever.'):
        return head, 'title'
    raise RuntimeError('The existing repository contains an unexpected test page. Refusing to overwrite it.')


def seed_repository(session, config, state, save):
    codecommit = session.client('codecommit')
    head, phase = repository_state(session)
    if phase == 'empty':
        result = codecommit.put_file(repositoryName='front_end_website', branchName='main', filePath='test.html',
            fileContent=TEST_PAGE.encode(), name=config['commit_author'], email=config['report_email'], commitMessage='This is my first commit.')
        head = result['commitId']
        state['initial_commit'] = head
        save()
        phase = 'sample'
    if phase == 'sample':
        state.setdefault('initial_commit', head)
        save()
    first = state.get('initial_commit')
    if first and not state.get('comment_posted'):
        token = hashlib.sha256((config['stack_name'] + first).encode()).hexdigest()
        codecommit.post_comment_for_compared_commit(repositoryName='front_end_website', afterCommitId=first,
            clientRequestToken=token, location={'filePath':'test.html','filePosition':4,'relativeFileVersion':'AFTER'},
            content='I must add a better title at some point.')
        state['comment_posted'] = True
        save()
    return head, phase


def deployment(session, commit, timeout=1200, execution_id=None):
    pipeline = session.client('codepipeline')
    def completed():
        matches = [item for page in pipeline.get_paginator('list_pipeline_executions').paginate(pipelineName='cafe_website_front_end_pipeline')
                   for item in page['pipelineExecutionSummaries']
                   if any(revision.get('revisionId') == commit for revision in item.get('sourceRevisions', []))
                   and (execution_id is None or item['pipelineExecutionId'] == execution_id)]
        for item in matches:
            if item['status'] == 'Succeeded':
                print('Pipeline deployed commit ' + commit[:12] + '.', flush=True)
                return item['pipelineExecutionId']
        if matches and matches[0]['status'] in ('Failed', 'Stopped', 'Cancelled'):
            raise RuntimeError('The automatic pipeline deployment failed for commit ' + commit)
        return None
    return wait_for('automatic pipeline deployment of ' + commit[:12], completed, timeout=timeout)


def http(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read(), dict(response.headers)


def verify_page(config, path, expected):
    def ready():
        try:
            body, headers = http(config['website_url'] + path)
            return expected.encode() in body and headers.get('Cache-Control') == 'max-age=14'
        except urllib.error.HTTPError as error:
            if error.code not in (403, 404):
                raise
            return False
    wait_for('the deployed CloudFront page ' + path, ready, timeout=300)


def pipeline_exercises(session, config, outputs, execute, environment, pipeline_exists):
    state, save = progress(config)
    execution_id = None
    head, phase = seed_repository(session, config, state, save)
    if not pipeline_exists:
        environment['TF_VAR_enable_pipeline'] = 'true'
        apply_plan(execute)
    if phase == 'sample':
        state['sample_execution'] = deployment(session, head)
        verify_page(config, '/test.html', '<title>Test page</title>')
        save()
        result = remote_action(config, outputs, 'title')
        head = result['commit_id']
        state['title_commit'] = head
        save()
        phase = 'title'
    if phase == 'title':
        state['title_execution'] = deployment(session, head)
        verify_page(config, '/test.html', '<title>Best test page ever.</title>')
        save()
        result = remote_action(config, outputs, 'website')
        if not result['website_source_removed']:
            raise RuntimeError('The duplicate resources/website directory was not removed.')
        head = result['commit_id']
        state['website_commit'] = head
        save()
    else:
        result = remote_action(config, outputs, 'website')
        head = result['commit_id']
        if not result['website_source_removed']:
            raise RuntimeError('The duplicate website source is still present.')
        meta = session.client('s3').head_object(Bucket=config['site_bucket'], Key='scripts/pastries.js')
        if meta.get('CacheControl') != 'max-age=14':
            execution_id = session.client('codepipeline').start_pipeline_execution(name='cafe_website_front_end_pipeline')['pipelineExecutionId']
    state['website_execution'] = deployment(session, head, execution_id=execution_id)
    state['website_commit'] = head
    save()
    return state


def verify(session, config, outputs, state):
    codecommit = session.client('codecommit')
    metadata = codecommit.get_repository(repositoryName='front_end_website')['repositoryMetadata']
    if metadata['defaultBranch'] != 'main':
        raise RuntimeError('The CodeCommit default branch must be main.')
    head, phase = repository_state(session)
    if phase != 'website' or head != state['website_commit']:
        raise RuntimeError('The default branch does not contain the final café website commit.')
    try:
        codecommit.get_file(repositoryName='front_end_website', commitSpecifier=head, filePath='test.html')
    except codecommit.exceptions.FileDoesNotExistException:
        pass
    else:
        raise RuntimeError('test.html must be removed from the final repository.')
    pipeline = session.client('codepipeline').get_pipeline(name='cafe_website_front_end_pipeline')['pipeline']
    source, target = pipeline['stages']
    if source['name'] != 'Source' or source['actions'][0]['actionTypeId']['provider'] != 'CodeCommit':
        raise RuntimeError('The pipeline must use CodeCommit as its source.')
    if source['actions'][0]['configuration'].get('BranchName') != 'main' or source['actions'][0]['configuration'].get('PollForSourceChanges') != 'true':
        raise RuntimeError('Automatic main-branch source detection is not enabled.')
    action = target['actions'][0]
    if target['name'] != 'Deploy' or action['actionTypeId']['provider'] != 'S3' or action['configuration']['BucketName'] != config['site_bucket']:
        raise RuntimeError('The pipeline must deploy to the provided café bucket.')
    if action['configuration']['Extract'] != 'true' or action['configuration']['CacheControl'] != 'max-age=14':
        raise RuntimeError('The deployment must extract files and apply Cache-Control max-age=14.')
    if pipeline['artifactStore']['location'] != config['artifact_bucket'] or pipeline['roleArn'] != config['pipeline_role_arn']:
        raise RuntimeError('The pipeline must use the provided artifact bucket and IAM role.')
    for path in ('index.html', 'config.js', 'scripts/pastries.js'):
        file = codecommit.get_file(repositoryName='front_end_website', commitSpecifier=head, filePath=path)['fileContent']
        object = session.client('s3').get_object(Bucket=config['site_bucket'], Key=path)
        if object['Body'].read() != file or object.get('CacheControl') != 'max-age=14':
            raise RuntimeError('The deployed object differs from CodeCommit: ' + path)
    verify_page(config, '/', '<html')
    verify_page(config, '/scripts/pastries.js', 'COFFEE_CONFIG')
    body, _ = http(config['website_url'] + '/config.js')
    if config['cognito_client_id'].encode() not in body or config['api_id'].encode() not in body:
        raise RuntimeError('The deployed website configuration points to another lab.')
    products, _ = http(f"https://{config['api_id']}.execute-api.{config['region']}.amazonaws.com/prod/products")
    if len(json.loads(products)['product_item_arr']) != 26:
        raise RuntimeError('The products API does not contain the seeded records.')
    beans, _ = http(f"https://{config['api_id']}.execute-api.{config['region']}.amazonaws.com/prod/bean_products")
    if len(json.loads(beans)) != 14:
        raise RuntimeError('The beans API does not contain the seeded inventory.')
    result = remote_action(config, outputs, 'verify')
    if result['commit_id'] != head or not result['website_source_removed']:
        raise RuntimeError('The Lab IDE checkout is not synchronized with the final commit.')
    print('Verified: CodeCommit history, automatic deployments, final café source, synchronized IDE checkout and Cache-Control max-age=14.', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    environment = load_environment()
    session = aws_session(environment)
    config, outputs = discover(session, environment)
    print('Lab 13 account: ' + config['account_id'] + '; stack: ' + config['stack_name'], flush=True)
    guard_state(config)
    for key in ('account_id', 'site_bucket', 'artifact_bucket', 'pipeline_role_arn', 'api_id', 'api_root_id', 'application_url', 'bucket_statements'):
        environment['TF_VAR_' + key] = config[key] if isinstance(config[key], str) else json.dumps(config[key])
    pipeline = session.client('codepipeline')
    try:
        pipeline.get_pipeline(name='cafe_website_front_end_pipeline')
        pipeline_exists = True
    except pipeline.exceptions.PipelineNotFoundException:
        pipeline_exists = False
    environment['TF_VAR_enable_pipeline'] = 'true' if pipeline_exists else 'false'
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
    confirmation = prepare_subscription(session, config)
    if not args.verify_only:
        prepare_application(session, config, outputs)
        import_existing(session, config, execute, pipeline_exists)
        apply_plan(execute)
        state = pipeline_exercises(session, config, outputs, execute, environment, pipeline_exists)
    else:
        state, _ = progress(config)
    verify(session, config, outputs, state)
    wait_for('SNS email confirmation for ' + config['report_email'], confirmation, timeout=1200)
    print('Lab 13 completed. Submit, review your grade, then End Lab.')
    print('Website: ' + config['website_url'])
    print('Repository: codecommit::' + config['region'] + '://front_end_website')
    print('Pipeline: cafe_website_front_end_pipeline')


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print('Lab 13 failed: ' + str(error), file=sys.stderr)
        sys.exit(1)


