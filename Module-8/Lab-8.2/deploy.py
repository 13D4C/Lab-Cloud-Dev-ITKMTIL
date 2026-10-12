import argparse
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
import urllib.parse
import urllib.request
import uuid
import zipfile


ROOT = Path('/home/ec2-user/environment')
WORK = ROOT / '.lab82'
SOURCE_URL = 'https://aws-tc-largeobjects.s3.us-west-2.amazonaws.com/CUR-TF-200-ACCDEV-2-91558/07-lab-deploy/code.zip'
SOURCE_SHA256 = '86d328325b89c34a21770aa49086747732b0ff644eb55bdaf00ae577befb4cb4'


def command(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def dependencies():
    sys.path.insert(0, site.getusersitepackages())
    try:
        import boto3
        import pymysql
    except ImportError:
        command(sys.executable, '-m', 'pip', 'install', '--user', 'boto3>=1.34,<2', 'PyMySQL>=1.1,<2')


def prepare(config):
    import boto3
    archive = WORK / 'code.zip'
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != SOURCE_SHA256:
        print('Downloading official Lab 8.2 application...', flush=True)
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
    policy = (ROOT / 'resources/public_policy.json').read_text().replace('<FMI_1>', config['site_bucket']).replace('<FMI_2>/32', config['client_cidr'])
    s3.put_bucket_policy(Bucket=config['site_bucket'], Policy=policy)
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
    code = ROOT / 'resources/codebase_partner'
    (code / 'Dockerfile').write_text('FROM node:22-alpine\nWORKDIR /usr/src/app\nCOPY package*.json ./\nRUN npm ci --omit=dev\nCOPY . .\nEXPOSE 3000\nCMD ["npm", "start"]\n')
    (code / '.dockerignore').write_text('node_modules\n.git\n.env\nnetwork.template\nREADME.md\n')
    command('docker', 'build', '-t', 'cafe/node-web-app:latest', str(code))
    ecr = session.client('ecr')
    import base64
    token = ecr.get_authorization_token()['authorizationData'][0]
    user, password = base64.b64decode(token['authorizationToken']).decode().split(':', 1)
    command('docker', 'login', '--username', user, '--password-stdin', token['proxyEndpoint'], input=password)
    command('docker', 'tag', 'cafe/node-web-app:latest', config['repository_url'] + ':latest')
    command('docker', 'push', config['repository_url'] + ':latest')
    print(f'Website uploaded; {len(products)} food products prepared; ECR image pushed.', flush=True)
    print('LAB82_PREPARED', flush=True)


def request(path, data=None):
    if data is not None:
        data = urllib.parse.urlencode(data).encode()
    with urllib.request.urlopen('http://127.0.0.1:8000' + path, data=data, timeout=30) as response:
        return response.read().decode()


def database(config):
    import pymysql
    from pymysql.constants import CLIENT
    connection = pymysql.connect(host=config['database_endpoint'], user='admin', password='coffee_beans_for_all',
                                 autocommit=True, client_flag=CLIENT.MULTI_STATEMENTS, connect_timeout=30,
                                 ssl={'check_hostname': False})
    with connection:
        with connection.cursor() as cursor:
            cursor.execute("CREATE USER IF NOT EXISTS 'nodeapp'@'%' IDENTIFIED WITH mysql_native_password BY 'coffee'")
            cursor.execute("ALTER USER 'nodeapp'@'%' IDENTIFIED WITH mysql_native_password BY 'coffee'")
            cursor.execute('CREATE DATABASE IF NOT EXISTS COFFEE')
            cursor.execute("GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, RELOAD, PROCESS, REFERENCES, INDEX, ALTER, SHOW DATABASES, CREATE TEMPORARY TABLES, LOCK TABLES, EXECUTE, REPLICATION SLAVE, REPLICATION CLIENT, CREATE VIEW, SHOW VIEW, CREATE ROUTINE, ALTER ROUTINE, CREATE USER, EVENT, TRIGGER ON *.* TO 'nodeapp'@'%' WITH GRANT OPTION")
            cursor.execute("SHOW GRANTS FOR 'nodeapp'@'%'")
            if not any('ON *.*' in row[0] and 'WITH GRANT OPTION' in row[0] for row in cursor.fetchall()):
                raise RuntimeError('Task 6 database grants do not match the lab instructions.')
            dump = (ROOT / 'resources/coffee_db_dump.sql').read_text()
            tables = re.findall(r'CREATE TABLE `(?:beans|suppliers)`.*?;', dump, re.DOTALL)
            inserts = re.findall(r'INSERT INTO `(?:beans|suppliers)` VALUES .*?;', dump, re.DOTALL)
            if len(tables) != 2 or len(inserts) != 2:
                raise RuntimeError('Unexpected SQL dump structure.')
            cursor.execute('USE COFFEE')
            for statement in tables:
                cursor.execute(statement.replace('CREATE TABLE ', 'CREATE TABLE IF NOT EXISTS ', 1))
            for statement in inserts:
                cursor.execute(statement.replace('INSERT INTO ', 'INSERT IGNORE INTO ', 1))
            cursor.execute('SELECT COUNT(*) FROM COFFEE.suppliers')
            suppliers = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM COFFEE.beans')
            beans = cursor.fetchone()[0]
            if suppliers < 8 or beans < 14:
                raise RuntimeError(f'Unexpected database rows: {suppliers} suppliers, {beans} beans.')
            print(f'Aurora: {suppliers} suppliers, {beans} beans.', flush=True)
    subprocess.run(['docker', 'rm', '-f', 'node-web-app-1'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    command('docker', 'run', '-d', '--name', 'node-web-app-1', '-p', '8000:3000',
            '-e', 'APP_DB_HOST=' + config['database_endpoint'], 'cafe/node-web-app:latest')
    try:
        deadline = time.monotonic() + 120
        while True:
            try:
                assert 'Coffee suppliers' in request('/')
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(3)
        if 'AnyCompany coffee suppliers' not in request('/suppliers'):
            raise RuntimeError('Container did not return supplier data.')
        inventory = json.loads(request('/beans.json'))
        if len(inventory) < 14 or not any(bean['product_name'] == 'Best bean' for bean in inventory):
            raise RuntimeError('Container did not return coffee beans.')
        supplier = {'name': 'Lab82 Test ' + uuid.uuid4().hex[:12], 'address': '100 Test Street', 'city': 'Bangkok',
                    'state': 'BK', 'email': 'lab82@example.com', 'phone': '+12025550123'}
        connection = pymysql.connect(host=config['database_endpoint'], user='nodeapp', password='coffee',
                                     database='COFFEE', autocommit=True, ssl={'check_hostname': False})
        with connection:
            with connection.cursor() as cursor:
                test_id = None
                try:
                    request('/supplier-add', supplier)
                    cursor.execute('SELECT id FROM suppliers WHERE name=%s', (supplier['name'],))
                    record = cursor.fetchone()
                    if not record:
                        raise RuntimeError('Supplier creation through the container failed.')
                    test_id = record[0]
                    supplier.update(id=test_id, address='200 Updated Street')
                    request('/supplier-update', supplier)
                    cursor.execute('SELECT address FROM suppliers WHERE id=%s', (test_id,))
                    if cursor.fetchone()[0] != supplier['address']:
                        raise RuntimeError('Supplier update through the container failed.')
                    request(f'/supplier-remove/{test_id}', {})
                    cursor.execute('SELECT COUNT(*) FROM suppliers WHERE id=%s', (test_id,))
                    if cursor.fetchone()[0] != 0:
                        raise RuntimeError('Supplier deletion through the container failed.')
                finally:
                    if test_id is not None:
                        cursor.execute('DELETE FROM suppliers WHERE id=%s', (test_id,))
        print('Container database connection, inventory and supplier CRUD passed.', flush=True)
    finally:
        command('docker', 'rm', '-f', 'node-web-app-1')
    print('LAB82_DATABASE_READY', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'database'])
    options = parser.parse_args()
    dependencies()
    config = json.loads((WORK / 'config.json').read_text())
    {'prepare': prepare, 'database': database}[options.action](config)
