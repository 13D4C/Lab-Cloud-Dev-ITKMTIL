import argparse
import importlib
import hashlib
import json
import mimetypes
from pathlib import Path
import re
import shutil
import site
import subprocess
import sys
import time
import urllib.request
import zipfile


ROOT = Path('/home/ec2-user/environment')
WORK = ROOT / '.lab101'
SOURCE_URL = 'https://aws-tc-largeobjects.s3.us-west-2.amazonaws.com/CUR-TF-200-ACCDEV-2-91558/10-lab-sqs/code.zip'
SOURCE_SHA256 = 'b9a9ae2623144b07047e7b640b79d0c9d1eadaa2a2692d8981132194601f598b'


def command(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


DOCKER = None


def docker(*args, **kwargs):
    global DOCKER
    if DOCKER is None:
        if not shutil.which('docker'):
            command('sudo', 'dnf', 'install', '-y', 'docker')
        command('sudo', 'systemctl', 'start', 'docker')
        test = subprocess.run(['docker', 'info'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        DOCKER = ['docker'] if test.returncode == 0 else ['sudo', 'docker']
    return command(*DOCKER, *args, **kwargs)


def dependencies():
    sys.path.insert(0, site.getusersitepackages())
    try:
        import boto3
        import pymysql
    except ImportError:
        command(sys.executable, '-m', 'pip', 'install', '--user', 'boto3>=1.34,<2', 'PyMySQL>=1.1,<2')
        importlib.invalidate_caches()


def prepare(config):
    import boto3
    verify_cli()
    archive = WORK / 'code.zip'
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != SOURCE_SHA256:
        print('Downloading official Lab 10.1 application...', flush=True)
        with urllib.request.urlopen(SOURCE_URL, timeout=120) as response, archive.open('wb') as output:
            shutil.copyfileobj(response, output)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SOURCE_SHA256:
        raise RuntimeError('Lab source checksum does not match.')
    with zipfile.ZipFile(archive) as source:
        for name in source.namelist():
            path = (ROOT / name).resolve()
            if not path.is_relative_to(ROOT):
                raise RuntimeError('Invalid path in lab archive.')
        source.extractall(ROOT)
    website = ROOT / 'resources/website'
    base_url = f"https://{config['api_id']}.execute-api.{config['region']}.amazonaws.com/prod"
    (website / 'config.js').write_text('window.COFFEE_CONFIG = ' + json.dumps({
        'API_GW_BASE_URL_STR': base_url, 'COGNITO_LOGIN_BASE_URL_STR': None
    }) + ';\n')
    session = boto3.Session(region_name=config['region'])
    if session.client('sts').get_caller_identity()['Account'] != config['account']:
        raise RuntimeError('Lab IDE belongs to a different AWS account.')
    s3 = session.client('s3')
    for path in sorted(website.rglob('*')):
        if path.is_file() and path.suffix != '.md':
            s3.upload_file(str(path), config['site_bucket'], str(path.relative_to(website)), ExtraArgs={
                'ContentType': mimetypes.guess_type(path.name)[0] or 'application/octet-stream',
                'CacheControl': 'max-age=0'
            })
    table = session.resource('dynamodb').Table('FoodProducts')
    products = json.loads((website / 'all_products.json').read_text())['product_item_arr']
    for product in products:
        item = {
            'product_id': product['product_id_str'], 'product_name': product['product_name_str'],
            'price_in_cents': product['price_in_cents_int'], 'description': product['description_str'],
            'tags': product['tag_str_arr']
        }
        if 'special_int' in product:
            item['special'] = product['special_int']
        try:
            table.put_item(Item=item, ConditionExpression='attribute_not_exists(product_id)')
        except session.client('dynamodb').exceptions.ConditionalCheckFailedException:
            pass
    configure_database(config)
    publisher = ROOT / 'resources/sqs-sns/send_beans_update.py'
    publisher.write_text(publisher.read_text().replace('<FMI_1>', config['account']))
    for filename in ['create-beans-queue.json', 'dlq-policy.json', 'beans-queue-policy.json']:
        path = ROOT / 'resources/sqs-sns' / filename
        path.write_text(path.read_text().replace('<FMI_1>', config['account']))
    path = ROOT / 'resources/sqs-sns/beans-topic-policy.json'
    document = json.loads(path.read_text().replace('<FMI_1>', config['topic_arn']).replace('<FMI_2>', config['account']))
    policy = json.loads(document['AttributeValue'])
    policy['Statement'][-1]['Principal'] = {'AWS': f"arn:aws:iam::{config['account']}:root"}
    document['AttributeValue'] = json.dumps(policy)
    path.write_text(json.dumps(document, indent=2))
    prepare_website_access(session, config)
    code = ROOT / 'resources/codebase_partner'
    (code / 'Dockerfile').write_text('FROM node:22-alpine\nWORKDIR /usr/src/app\nCOPY package*.json ./\nRUN npm ci --omit=dev\nCOPY . .\nEXPOSE 3000\nCMD ["npm", "start"]\n')
    (code / '.dockerignore').write_text('node_modules\n.git\n.env\nnetwork.template\nREADME.md\n')
    ecr = session.client('ecr')
    try:
        ecr.describe_images(repositoryName='cafe/node-web-app', imageIds=[{'imageTag': config['image_tag']}])
        print('Reusing the verified Lab 10.1 application image.', flush=True)
        print('LAB101_PREPARED', flush=True)
        return
    except ecr.exceptions.ImageNotFoundException:
        pass
    docker('build', '--progress=plain', '-t', 'cafe/node-web-app:latest', str(code))
    import base64
    token = ecr.get_authorization_token()['authorizationData'][0]
    user, password = base64.b64decode(token['authorizationToken']).decode().split(':', 1)
    docker('login', '--username', user, '--password-stdin', token['proxyEndpoint'], input=password)
    docker('tag', 'cafe/node-web-app:latest', config['repository_url'] + ':latest')
    docker('push', config['repository_url'] + ':latest')
    docker('tag', 'cafe/node-web-app:latest', config['repository_url'] + ':' + config['image_tag'])
    docker('push', config['repository_url'] + ':' + config['image_tag'])
    print(f'Website uploaded; {len(products)} food products prepared; ECR image pushed.', flush=True)
    print('LAB101_PREPARED', flush=True)


def verify_cli():
    executable = shutil.which('aws')
    if executable:
        result = command(executable, '--version', capture_output=True)
        if 'aws-cli/2.' in result.stdout + result.stderr:
            print(result.stdout.strip() or result.stderr.strip(), flush=True)
            return
    archive = WORK / 'awscliv2.zip'
    with urllib.request.urlopen('https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip', timeout=120) as response, archive.open('wb') as output:
        shutil.copyfileobj(response, output)
    with zipfile.ZipFile(archive) as source:
        source.extractall(WORK)
    installer = WORK / 'aws/install'
    installer.chmod(0o755)
    for path in (WORK / 'aws/dist').rglob('*'):
        if path.is_file() and not path.suffix:
            path.chmod(0o755)
    command('sudo', str(installer), '--update')
    command('/usr/local/bin/aws', '--version')


def prepare_website_access(session, config):
    arns = {}
    for scope, region, ip_name, filename in [
        ('CLOUDFRONT', 'us-east-1', 'office', 'global_waf_acl.json'),
        ('REGIONAL', config['region'], 'office_regional', 'regional_waf_acl.json')
    ]:
        waf = session.client('wafv2', region_name=region)
        sets = [item for item in waf.list_ip_sets(Scope=scope, Limit=100)['IPSets'] if item['Name'] == ip_name]
        if len(sets) > 1:
            raise RuntimeError('Multiple office WAF IP sets found.')
        if sets:
            ip_set = waf.get_ip_set(Name=ip_name, Scope=scope, Id=sets[0]['Id'])
            if ip_set['IPSet']['Addresses'] != [config['client_cidr']]:
                waf.update_ip_set(Name=ip_name, Scope=scope, Id=sets[0]['Id'],
                                  Addresses=[config['client_cidr']], LockToken=ip_set['LockToken'])
            ip_arn = ip_set['IPSet']['ARN']
        else:
            ip_arn = waf.create_ip_set(Name=ip_name, Scope=scope, IPAddressVersion='IPV4',
                                       Addresses=[config['client_cidr']])['Summary']['ARN']
        document = json.loads((ROOT / 'resources' / filename).read_text().replace('<FMI_4>', ip_arn).replace('<FMI_5>', ip_arn))
        acls = [item for item in waf.list_web_acls(Scope=scope, Limit=100)['WebACLs'] if item['Name'] == document['Name']]
        if len(acls) > 1:
            raise RuntimeError('Multiple lab WAF ACLs found.')
        def write_acl():
            try:
                if acls:
                    current = waf.get_web_acl(Name=document['Name'], Scope=scope, Id=acls[0]['Id'])
                    fields = ['Rules', 'DefaultAction', 'VisibilityConfig']
                    if any(current['WebACL'][key] != document[key] for key in fields):
                        waf.update_web_acl(**document, Id=acls[0]['Id'], LockToken=current['LockToken'])
                    arns[scope] = current['WebACL']['ARN']
                else:
                    arns[scope] = waf.create_web_acl(**document)['Summary']['ARN']
                return True
            except waf.exceptions.WAFUnavailableEntityException:
                return False
        wait_for('the lab office WAF ACL', write_acl, timeout=180)
    cloudfront = session.client('cloudfront')
    response = cloudfront.get_distribution_config(Id=config['distribution_id'])
    document = response['DistributionConfig']
    if not any(origin['DomainName'].startswith(config['site_bucket'] + '.s3') for origin in document['Origins']['Items']):
        raise RuntimeError('Refusing to change a CloudFront distribution outside the café lab.')
    if document.get('WebACLId') != arns['CLOUDFRONT'] or document.get('IsIPV6Enabled'):
        document['WebACLId'] = arns['CLOUDFRONT']
        document['IsIPV6Enabled'] = False
        cloudfront.update_distribution(Id=config['distribution_id'], IfMatch=response['ETag'], DistributionConfig=document)
    waf = session.client('wafv2')
    resource = f"arn:aws:apigateway:{config['region']}::/restapis/{config['api_id']}/stages/prod"
    try:
        association = waf.get_web_acl_for_resource(ResourceArn=resource).get('WebACL')
    except waf.exceptions.WAFNonexistentItemException:
        association = None
    if not association or association['ARN'] != arns['REGIONAL']:
        def associate():
            try:
                waf.associate_web_acl(WebACLArn=arns['REGIONAL'], ResourceArn=resource)
                return True
            except (waf.exceptions.WAFUnavailableEntityException, waf.exceptions.WAFNonexistentItemException):
                return False
        wait_for('the API Gateway WAF association', associate, timeout=180)
    cloudfront.create_invalidation(DistributionId=config['distribution_id'], InvalidationBatch={
        'Paths': {'Quantity': 1, 'Items': ['/*']}, 'CallerReference': str(time.time_ns())})
    print('Provided website OAI and office-only WAF setup prepared through the Lab IDE role.', flush=True)


def configure_database(config):
    import pymysql
    connection = pymysql.connect(host=config['database_endpoint'], user='admin', password='coffee_beans_for_all',
                                 autocommit=True, connect_timeout=30, ssl={'check_hostname': False})
    with connection, connection.cursor() as cursor:
        cursor.execute("CREATE USER IF NOT EXISTS 'nodeapp'@'%' IDENTIFIED WITH mysql_native_password BY 'coffee'")
        cursor.execute("ALTER USER 'nodeapp'@'%' IDENTIFIED WITH mysql_native_password BY 'coffee'")
        cursor.execute('CREATE DATABASE IF NOT EXISTS COFFEE')
        cursor.execute("GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, RELOAD, PROCESS, REFERENCES, INDEX, ALTER, SHOW DATABASES, CREATE TEMPORARY TABLES, LOCK TABLES, EXECUTE, REPLICATION SLAVE, REPLICATION CLIENT, CREATE VIEW, SHOW VIEW, CREATE ROUTINE, ALTER ROUTINE, CREATE USER, EVENT, TRIGGER ON *.* TO 'nodeapp'@'%' WITH GRANT OPTION")
        dump = (ROOT / 'resources/coffee_db_dump.sql').read_text()
        tables = re.findall(r'CREATE TABLE `(?:beans|suppliers)`.*?;', dump, re.DOTALL)
        inserts = re.findall(r'INSERT INTO `(?:beans|suppliers)` VALUES .*?;', dump, re.DOTALL)
        if len(tables) != 2 or len(inserts) != 2:
            raise RuntimeError('Unexpected SQL dump structure.')
        cursor.execute('USE COFFEE')
        for statement in tables:
            cursor.execute(statement.replace('CREATE TABLE ', 'CREATE TABLE IF NOT EXISTS ', 1))
        for statement in inserts:
            if statement.startswith('INSERT INTO `beans`'):
                cursor.execute('SELECT COUNT(*) FROM beans')
                if cursor.fetchone()[0]:
                    continue
            cursor.execute(statement.replace('INSERT INTO ', 'INSERT IGNORE INTO ', 1))
        cursor.execute('SELECT COUNT(*) FROM suppliers')
        if cursor.fetchone()[0] < 8:
            raise RuntimeError('Supplier seed data is incomplete.')
    print('Provided Aurora database configured and seeded.', flush=True)


def inventory(config):
    import pymysql
    connection = pymysql.connect(host=config['database_endpoint'], user='nodeapp', password='coffee',
                                 database='COFFEE', connect_timeout=30, ssl={'check_hostname': False})
    with connection, connection.cursor() as cursor:
        cursor.execute("SELECT supplier_id, type, quantity FROM beans WHERE (supplier_id=3 AND type='Excelsa') OR (supplier_id=6 AND type='Arabica')")
        values = {f'{supplier}:{kind}': int(quantity) for supplier, kind, quantity in cursor.fetchall()}
        cursor.execute('SELECT COUNT(*) FROM beans')
        bean_count = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(*) FROM suppliers')
        supplier_count = cursor.fetchone()[0]
    if len(values) != 2 or bean_count != 14 or supplier_count != 8:
        raise RuntimeError(f'Unexpected coffee inventory: {values}, beans={bean_count}, suppliers={supplier_count}')
    return values


def wait_for(description, operation, timeout=900):
    deadline = time.monotonic() + timeout
    last_report = 0
    while True:
        result = operation()
        if result:
            return result
        if time.monotonic() > deadline:
            raise TimeoutError('Timed out waiting for ' + description + '. Run again to resume.')
        if time.monotonic() - last_report >= 30:
            print('Waiting for ' + description + '...', flush=True)
            last_report = time.monotonic()
        time.sleep(5)


def queue_counts(sqs, url):
    attributes = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=[
        'ApproximateNumberOfMessages', 'ApproximateNumberOfMessagesNotVisible', 'ApproximateNumberOfMessagesDelayed'
    ])['Attributes']
    return sum(int(value) for value in attributes.values())


def configure_consumer(session, config, enabled):
    eb = session.client('elasticbeanstalk')
    settings = eb.describe_configuration_settings(ApplicationName='MyNodeApp', EnvironmentName='MyEnv')['ConfigurationSettings'][0]['OptionSettings']
    namespace = 'aws:elasticbeanstalk:application:environment'
    current = {item['OptionName']: item.get('Value', '') for item in settings if item['Namespace'] == namespace}
    if enabled:
        desired = {'SQS_ENDPOINT': config['queue_url'], 'SQS_REGION': config['region']}
        if any(current.get(key) != value for key, value in desired.items()):
            print('Enabling the application SQS consumer...', flush=True)
            eb.update_environment(EnvironmentId=config['environment_id'], OptionSettings=[
                {'Namespace': namespace, 'OptionName': key, 'Value': value} for key, value in desired.items()
            ])
    elif 'SQS_ENDPOINT' in current:
        print('Pausing the consumer for the Task 6 deduplication test...', flush=True)
        eb.update_environment(EnvironmentId=config['environment_id'], OptionsToRemove=[
            {'Namespace': namespace, 'OptionName': 'SQS_ENDPOINT'}
        ])
    def ready():
        result = eb.describe_environments(EnvironmentIds=[config['environment_id']])['Environments'][0]
        if result['Status'] in ('Terminating', 'Terminated'):
            raise RuntimeError('The provided application was terminated.')
        return result['Status'] == 'Ready'
    wait_for('Elastic Beanstalk consumer configuration', ready)
    settings = eb.describe_configuration_settings(ApplicationName='MyNodeApp', EnvironmentName='MyEnv')['ConfigurationSettings'][0]['OptionSettings']
    current = {item['OptionName']: item.get('Value', '') for item in settings if item['Namespace'] == namespace}
    if enabled and any(current.get(key) != value for key, value in desired.items()):
        raise RuntimeError('Elastic Beanstalk did not accept the SQS configuration.')
    if not enabled and 'SQS_ENDPOINT' in current:
        raise RuntimeError('The application consumer is still enabled during the publishing test.')


def publish(client, config, body):
    arguments = dict(TopicArn=config['topic_arn'], Message=body,
                     Subject='New bean delivery', MessageGroupId='bean_message_group')
    if int(body.split(':')[-1]) <= 0:
        arguments['MessageAttributes'] = {
            'inventory_alert': {'DataType': 'String', 'StringValue': 'out_of_stock'}
        }
    result = client.publish(**arguments)
    print(f"Published {body}: {result['SequenceNumber']}", flush=True)
    return result


def records(number):
    return [line.strip() for line in (ROOT / f'resources/sqs-sns/beans_update_{number}.txt').read_text().splitlines() if line.strip()]


def read_envelope(message, config):
    envelope = json.loads(message['Body'])
    if envelope.get('Type') != 'Notification' or envelope.get('TopicArn') != config['topic_arn']:
        raise RuntimeError('A queue message did not come from the lab SNS topic.')
    if envelope.get('Subject') != 'New bean delivery':
        raise RuntimeError('SNS message subject is missing.')
    body = envelope['Message']
    if int(body.split(':')[-1]) <= 0:
        attribute = envelope.get('MessageAttributes', {}).get('inventory_alert', {})
        if attribute.get('Value') != 'out_of_stock':
            raise RuntimeError('SNS out_of_stock metadata is missing.')
    return body


def inspect_dlq(sqs, config):
    expected = set(records(2))
    seen, handles = set(), []
    deadline = time.monotonic() + 45
    try:
        while time.monotonic() < deadline and seen != expected:
            response = sqs.receive_message(QueueUrl=config['dead_letter_queue_url'], MaxNumberOfMessages=10,
                                           WaitTimeSeconds=2, VisibilityTimeout=60)
            for message in response.get('Messages', []):
                handles.append(message['ReceiptHandle'])
                body = read_envelope(message, config)
                if body not in expected or body in seen:
                    raise RuntimeError(f'Unexpected or duplicated dead-letter message: {body}')
                seen.add(body)
    finally:
        for handle in handles:
            sqs.change_message_visibility(QueueUrl=config['dead_letter_queue_url'], ReceiptHandle=handle, VisibilityTimeout=0)
    if seen != expected:
        raise RuntimeError(f'Dead-letter messages do not match the three invalid records: {seen}')
    print('Three original invalid messages and SNS metadata verified in the DLQ; messages retained.', flush=True)


def verify(config, session):
    expected = {'3:Excelsa': 209, '6:Arabica': 306}
    current = inventory(config)
    if current != expected:
        raise RuntimeError(f'Inventory does not match the lab targets: {current}')
    sqs = session.client('sqs')
    wait_for('three dead-letter records and an empty processing queue', lambda:
             queue_counts(sqs, config['dead_letter_queue_url']) == 3 and queue_counts(sqs, config['queue_url']) == 0, timeout=180)
    inspect_dlq(sqs, config)
    configure_consumer(session, config, True)
    print('LAB101_INVENTORY ' + json.dumps(current, sort_keys=True), flush=True)
    print('LAB101_MESSAGING_VERIFIED', flush=True)


def messaging(config, session):
    progress_path = WORK / 'progress.json'
    progress = json.loads(progress_path.read_text()) if progress_path.exists() else {}
    identity = {key: config[key] for key in ('account', 'stack_name')}
    if progress and progress.get('identity') != identity:
        raise RuntimeError('Saved message progress belongs to a different lab.')
    progress['identity'] = identity
    def save():
        temporary = progress_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(progress, indent=2))
        temporary.replace(progress_path)
    sqs, sns = session.client('sqs'), session.client('sns')
    initial = inventory(config)
    baseline = {'3:Excelsa': 200, '6:Arabica': 300}
    targets = {'3:Excelsa': 209, '6:Arabica': 306}
    if any(value not in (baseline[key], targets[key]) for key, value in initial.items()):
        raise RuntimeError(f'Unexpected inventory quantities; no messages were published: {initial}')
    if initial == targets and queue_counts(sqs, config['dead_letter_queue_url']) == 3:
        print('The lab inventory is already updated; skipping all message publishing.', flush=True)
        verify(config, session)
        return
    if not progress.get('dedup_verified'):
        configure_consumer(session, config, False)
        first = records(1)
        responses = [publish(sns, config, body) for body in first]
        if responses[0]['SequenceNumber'] != responses[2]['SequenceNumber'] or len({item['SequenceNumber'] for item in responses}) != 3:
            raise RuntimeError('SNS did not deduplicate the repeated inventory record.')
        expected = set(first)
        seen = set(progress.get('dedup_seen', []))
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            response = sqs.receive_message(QueueUrl=config['queue_url'], MaxNumberOfMessages=10,
                                           WaitTimeSeconds=5, VisibilityTimeout=30)
            messages = response.get('Messages', [])
            for message in messages:
                body = read_envelope(message, config)
                if body not in expected:
                    sqs.change_message_visibility(QueueUrl=config['queue_url'], ReceiptHandle=message['ReceiptHandle'], VisibilityTimeout=0)
                    raise RuntimeError('Found a message outside the Task 6 test; it was retained.')
                seen.add(body)
                progress['dedup_seen'] = sorted(seen)
                save()
                sqs.delete_message(QueueUrl=config['queue_url'], ReceiptHandle=message['ReceiptHandle'])
            if seen == expected and not messages:
                break
        if seen != expected:
            raise RuntimeError(f'Task 6 expected three distinct SQS messages; received {seen}. Run again to resume.')
        progress['dedup_verified'] = True
        save()
        print('Task 6: four publishes produced three unique SNS/SQS messages; duplicate sequence and out_of_stock metadata verified.', flush=True)
    if not progress.get('invalid_sent'):
        for body in records(2):
            sent = progress.setdefault('invalid_records', [])
            if body not in sent:
                publish(sns, config, body)
                sent.append(body)
                save()
        progress['invalid_sent'] = True
        save()
    configure_consumer(session, config, True)
    wait_for('the application to redrive all three invalid messages into the DLQ', lambda:
             queue_counts(sqs, config['dead_letter_queue_url']) == 3 and queue_counts(sqs, config['queue_url']) == 0, timeout=420)
    inspect_dlq(sqs, config)
    for body in records(3):
        key = ':'.join(body.split(':')[:2])
        quantity = inventory(config)[key]
        if quantity == targets[key]:
            continue
        attempted = progress.setdefault('valid_attempted', [])
        if body not in attempted:
            attempted.append(body)
            save()
            publish(sns, config, body)
        wait_for('the application to update ' + key, lambda: inventory(config)[key] == targets[key], timeout=180)
    verify(config, session)
    progress['complete'] = True
    save()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'messaging', 'verify'])
    args = parser.parse_args()
    dependencies()
    config = json.loads((WORK / 'config.json').read_text())
    import boto3
    session = boto3.Session(region_name=config['region'])
    if session.client('sts').get_caller_identity()['Account'] != config['account']:
        raise RuntimeError('Lab IDE belongs to a different AWS account.')
    if args.action == 'prepare':
        prepare(config)
    elif args.action == 'messaging':
        messaging(config, session)
    else:
        verify(config, session)


if __name__ == '__main__':
    main()


