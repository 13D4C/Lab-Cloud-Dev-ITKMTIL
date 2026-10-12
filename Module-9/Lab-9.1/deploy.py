import argparse
import contextlib
import importlib.util
import io
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
WORK = ROOT / '.lab91'
SOURCE_URL = 'https://aws-tc-largeobjects.s3.us-west-2.amazonaws.com/CUR-TF-200-ACCDEV-2-91558/08-lab-db-caching/code.zip'
SOURCE_SHA256 = '7439f3c84d23f68558827a42691767ac0d6be8631aaab5f874500198431bcb1e'


def command(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def dependencies():
    sys.path.insert(0, site.getusersitepackages())
    try:
        import boto3
        import pymysql
        import pymemcache
    except ImportError:
        command(sys.executable, '-m', 'pip', 'install', '--user', 'boto3>=1.34,<2', 'PyMySQL>=1.1,<2', 'pymemcache>=4,<5')
        importlib.invalidate_caches()


def prepare(config):
    import boto3
    verify_cli()
    archive = WORK / 'code.zip'
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != SOURCE_SHA256:
        print('Downloading official Lab 9.1 application...', flush=True)
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
    configure_database(config)
    configure_scripts(config)
    code = ROOT / 'resources/codebase_partner'
    shutil.copyfile(ROOT / 'resources/bean.controller_2.js', code / 'app/controller/bean.controller.js')
    (code / 'Dockerfile').write_text('FROM node:22-alpine\nWORKDIR /usr/src/app\nCOPY package*.json ./\nRUN npm ci --omit=dev\nCOPY . .\nEXPOSE 3000\nCMD ["npm", "start"]\n')
    (code / '.dockerignore').write_text('node_modules\n.git\n.env\nnetwork.template\nREADME.md\n')
    command('docker', 'build', '--progress=plain', '-t', 'cafe/node-web-app:latest', str(code))
    ecr = session.client('ecr')
    import base64
    token = ecr.get_authorization_token()['authorizationData'][0]
    user, password = base64.b64decode(token['authorizationToken']).decode().split(':', 1)
    command('docker', 'login', '--username', user, '--password-stdin', token['proxyEndpoint'], input=password)
    command('docker', 'tag', 'cafe/node-web-app:latest', config['repository_url'] + ':latest')
    command('docker', 'push', config['repository_url'] + ':latest')
    print(f'Website uploaded; {len(products)} food products prepared; ECR image pushed.', flush=True)
    print('LAB91_PREPARED', flush=True)


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


def configure_scripts(config):
    for name in ('find_all', 'update_item', 'create_item', 'delete_item'):
        path = ROOT / 'python_3' / (name + '.py')
        source = path.read_text().replace('<FMI_2>', config['database_endpoint'])
        source = source.replace('database="COFFEE"\n)', 'database="COFFEE", autocommit=True, connect_timeout=30\n)')
        if config.get('cache_endpoint'):
            source = source.replace('<FMI_1>', config['cache_endpoint'])
        source = source.replace("11211))", "11211), connect_timeout=10, timeout=10, default_noreply=False)")
        if name == 'create_item':
            source = source.replace(
                'VALUES (%s, %s, %s, %s, %s, %s)',
                "SELECT %s, %s, %s, %s, %s, %s FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM beans WHERE product_name='Worlds greatest bean' AND supplier_id=1)"
            )
        path.write_text(source)


def verify_cache(config):
    import pymysql
    from pymemcache.client import base
    configure_scripts(config)
    nodes = [base.Client((item['address'], item['port']), connect_timeout=10, timeout=10, default_noreply=False)
             for item in config['cache_nodes']]
    if len(nodes) != 3:
        raise RuntimeError('Task 3 requires three Memcached nodes.')
    shared = base.Client((config['cache_endpoint'], 11211), connect_timeout=10, timeout=10, default_noreply=False)
    connection = pymysql.connect(host=config['database_endpoint'], user='nodeapp', password='coffee',
                                 database='COFFEE', autocommit=True, connect_timeout=30,
                                 cursorclass=pymysql.cursors.DictCursor)
    def query(sql, args=None):
        with connection.cursor() as cursor:
            cursor.execute(sql, args)
            return cursor.fetchall()
    def script(name):
        stream = io.StringIO()
        module = None
        original = None
        try:
            with contextlib.redirect_stdout(stream):
                spec = importlib.util.spec_from_file_location('lab91_' + name, ROOT / 'python_3' / (name + '.py'))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                original = module.memcached_client
                module.memcached_client = shared
                module.main()
        finally:
            if original:
                original.close()
            if module and hasattr(module, 'mydb'):
                module.mydb.close()
        return stream.getvalue()
    def refreshed():
        output = script('find_all')
        if 'Data not found in cache' not in output:
            raise RuntimeError('Expected a cache miss after changing the database.')
        cached = json.loads(shared.get('all_beans'))
        rows = query('SELECT * FROM beans ORDER BY id')
        if sorted(cached, key=lambda item: item['id']) != rows:
            raise RuntimeError('Cached inventory does not match the Aurora database.')
        if 'Data returned from cache' not in script('find_all'):
            raise RuntimeError('Expected a cache hit on the next read.')
        return cached
    try:
        for index, node in enumerate(nodes, 1):
            key = 'lab91_probe_' + uuid.uuid4().hex
            if not node.set(key, 'ready', expire=30) or node.get(key) != b'ready':
                raise RuntimeError(f'Memcached node {index} did not pass its read/write test.')
            node.delete(key)
            for key in ('all_beans', 'beans_all', 'beans_1', 'beans_14'):
                node.delete(key)
        key = 'lab91_ttl_' + uuid.uuid4().hex
        shared.set(key, 'expires', expire=2)
        if shared.get(key) != b'expires':
            raise RuntimeError('Memcached TTL probe could not be read.')
        time.sleep(3)
        if shared.get(key) is not None:
            raise RuntimeError('Memcached did not expire the TTL probe.')
        shared.delete('all_beans')
        refreshed()
        print('Task 4: lazy loading, cache hit and TTL verified on the three-node cluster.', flush=True)
        script('update_item')
        if shared.get('all_beans') is not None:
            raise RuntimeError('Task 5 did not invalidate the cached list.')
        beans = refreshed()
        row = next(item for item in beans if item['id'] == 1)
        expected = dict(supplier_id=1, type='Arabica Arabica', product_name='Best bean EVER',
                        price='28.00', description='So delicious, smooth coffee.', quantity=800)
        if any(row.get(key) != value for key, value in expected.items()):
            raise RuntimeError('Task 5 bean values do not match the assignment.')
        print('Task 5: bean 1 updated; cache invalidation and refresh verified.', flush=True)
        before = len(beans)
        existed = any(item['product_name'] == 'Worlds greatest bean' and item['supplier_id'] == 1 for item in beans)
        script('create_item')
        if shared.get('all_beans') is not None:
            raise RuntimeError('Task 6 did not invalidate the cached list.')
        beans = refreshed()
        created = [item for item in beans if item['product_name'] == 'Worlds greatest bean' and item['supplier_id'] == 1]
        if len(created) != 1 or len(beans) != before + (0 if existed else 1):
            raise RuntimeError('Task 6 did not create exactly one new bean.')
        row = created[0]
        if row['type'] != 'Java Java' or row['price'] != '38.00' or row['quantity'] != 400 or row['description'] != 'Nutty tasting coffee.':
            raise RuntimeError('Task 6 bean values do not match the assignment.')
        print(f"Task 6: Worlds greatest bean (id {row['id']}) created without duplicates; cache refreshed.", flush=True)
        script('delete_item')
        if shared.get('all_beans') is not None:
            raise RuntimeError('Task 7 did not invalidate the cached list.')
        beans = refreshed()
        if any(item['id'] == 14 for item in beans) or len(beans) != 14:
            raise RuntimeError('Task 7 must remove bean 14 and leave 14 beans in the completed lab.')
        print('Task 7: bean 14 deleted; 14 remaining beans verified in both Aurora and Memcached.', flush=True)
        for node in nodes:
            for key in ('beans_all', 'beans_1', 'beans_14'):
                node.delete(key)
        print('LAB91_CACHE_VERIFIED', flush=True)
    finally:
        connection.close()
        shared.close()
        for node in nodes:
            node.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'cache'])
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
        verify_cache(config)


if __name__ == '__main__':
    main()

