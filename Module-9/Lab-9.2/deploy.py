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
WORK = ROOT / '.lab92'
SOURCE_URL = 'https://aws-tc-largeobjects.s3.us-west-2.amazonaws.com/CUR-TF-200-ACCDEV-2-91558/09-lab-cloudfront/code.zip'
SOURCE_SHA256 = '3ba54d74b031a7cd45778459d2260295d8cac565af2b6bd30598340dd24a36a5'


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
        print('Downloading official Lab 9.2 application...', flush=True)
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
                'CacheControl': 'max-age=180'
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
    code = ROOT / 'resources/codebase_partner'
    (code / 'Dockerfile').write_text('FROM node:22-alpine\nWORKDIR /usr/src/app\nCOPY package*.json ./\nRUN npm ci --omit=dev\nCOPY . .\nEXPOSE 3000\nCMD ["npm", "start"]\n')
    (code / '.dockerignore').write_text('node_modules\n.git\n.env\nnetwork.template\nREADME.md\n')
    docker('build', '--progress=plain', '-t', 'cafe/node-web-app:latest', str(code))
    ecr = session.client('ecr')
    import base64
    token = ecr.get_authorization_token()['authorizationData'][0]
    user, password = base64.b64decode(token['authorizationToken']).decode().split(':', 1)
    docker('login', '--username', user, '--password-stdin', token['proxyEndpoint'], input=password)
    docker('tag', 'cafe/node-web-app:latest', config['repository_url'] + ':latest')
    docker('push', config['repository_url'] + ':latest')
    print(f'Website uploaded; {len(products)} food products prepared; ECR image pushed.', flush=True)
    print('LAB92_PREPARED', flush=True)


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


def verify_external_blocking(config):
    deadline = time.monotonic() + 300
    urls = [config['website_url'], config['beans_api_url']]
    import urllib.error
    while True:
        statuses = []
        for url in urls:
            try:
                with urllib.request.urlopen(url, timeout=30) as response:
                    statuses.append(response.status)
            except urllib.error.HTTPError as error:
                statuses.append(error.code)
        if statuses == [403, 403]:
            print('Outside-office network: CloudFront 403 and API Gateway 403 verified from Lab IDE.', flush=True)
            print('LAB92_BLOCKING_VERIFIED', flush=True)
            return
        if time.monotonic() > deadline:
            raise RuntimeError(f'Expected WAF to block the Lab IDE network; received {statuses}.')
        print(f'Waiting for WAF propagation; remote status codes: {statuses}', flush=True)
        time.sleep(10)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'verify'])
    args = parser.parse_args()
    dependencies()
    config = json.loads((WORK / 'config.json').read_text())
    import boto3
    session = boto3.Session(region_name=config['region'])
    if session.client('sts').get_caller_identity()['Account'] != config['account']:
        raise RuntimeError('Lab IDE belongs to a different AWS account.')
    if args.action == 'prepare':
        prepare(config)
    else:
        verify_external_blocking(config)


if __name__ == '__main__':
    main()

