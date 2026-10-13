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
WORK = ROOT / '.lab13'
SOURCE_URL = 'https://aws-tc-largeobjects.s3.us-west-2.amazonaws.com/CUR-TF-200-ACCDEV-2-91558/13-lab-ci-cd/code.zip'
SOURCE_SHA256 = '94a9dd242b57d01da94b4259066ceaf409d99471081f18d0129716629d372685'


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
    archive = WORK / 'code.zip'
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != SOURCE_SHA256:
        print('Downloading official Lab 13 application...', flush=True)
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
    pipeline_path = ROOT / 'resources/cafe_website_front_end_pipeline.json'
    pipeline_path.write_text(pipeline_path.read_text().replace('<FMI_1>', config['account']).replace('<FMI_2>', config['site_bucket']))
    base_url = f"https://{config['api_id']}.execute-api.{config['region']}.amazonaws.com/prod"
    (website / 'config.js').write_text('window.COFFEE_CONFIG = ' + json.dumps({
        'API_GW_BASE_URL_STR': base_url, 'COGNITO_LOGIN_BASE_URL_STR': config['login_url']
    }) + ';\n')
    session = boto3.Session(region_name=config['region'])
    if session.client('sts').get_caller_identity()['Account'] != config['account']:
        raise RuntimeError('Lab IDE belongs to a different AWS account.')
    verify_cli()
    prepare_website_access(session, config)
    session.client('cognito-idp').admin_update_user_attributes(UserPoolId=config['cognito_pool_id'], Username='frank', UserAttributes=[{'Name':'email','Value':config['report_email']}])
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
    code = ROOT / 'resources/codebase_partner'
    (code / 'Dockerfile').write_text('FROM node:22-alpine\nWORKDIR /usr/src/app\nCOPY package*.json ./\nRUN npm ci --omit=dev\nCOPY . .\nEXPOSE 3000\nCMD ["npm", "start"]\n')
    (code / '.dockerignore').write_text('node_modules\n.git\n.env\nnetwork.template\nREADME.md\n')
    ecr = session.client('ecr')
    try:
        ecr.describe_images(repositoryName='cafe/node-web-app', imageIds=[{'imageTag': config['image_tag']}])
        print('Reusing the prepared Lab 13 application image.', flush=True)
        print('LAB13_PREPARED', flush=True)
        return
    except ecr.exceptions.ImageNotFoundException:
        pass
    docker('build', '--progress=plain', '-t', 'cafe/node-web-app:latest', str(code))
    ecr = session.client('ecr')
    import base64
    token = ecr.get_authorization_token()['authorizationData'][0]
    user, password = base64.b64decode(token['authorizationToken']).decode().split(':', 1)
    docker('login', '--username', user, '--password-stdin', token['proxyEndpoint'], input=password)
    docker('tag', 'cafe/node-web-app:latest', config['repository_url'] + ':latest')
    docker('push', config['repository_url'] + ':latest')
    docker('tag', 'cafe/node-web-app:latest', config['repository_url'] + ':' + config['image_tag'])
    docker('push', config['repository_url'] + ':' + config['image_tag'])
    print(f'Website uploaded; {len(products)} food products prepared; ECR image pushed.', flush=True)
    print('LAB13_PREPARED', flush=True)


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


def wait_for(description, operation, timeout=900):
    deadline = time.monotonic() + timeout
    next_message = 0
    while time.monotonic() < deadline:
        result = operation()
        if result:
            return result
        if time.monotonic() >= next_message:
            print('Waiting for ' + description + '...', flush=True)
            next_message = time.monotonic() + 30
        time.sleep(5)
    raise TimeoutError('Timed out waiting for ' + description)


def git(config, *arguments, cwd=None, capture=False):
    import os
    environment = os.environ.copy()
    environment['AWS_DEFAULT_REGION'] = config['region']
    environment['PATH'] = str(Path.home() / '.local/bin') + os.pathsep + environment.get('PATH', '')
    result = subprocess.run(['git', *arguments], cwd=cwd or ROOT, env=environment, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.rstrip('\r\n') if capture else None


def clone_repository(config):
    import importlib.util
    if importlib.util.find_spec('git_remote_codecommit') is None:
        command(sys.executable, '-m', 'pip', 'install', '--user', 'git-remote-codecommit')
    repository = ROOT / 'front_end_website'
    remote = 'codecommit::' + config['region'] + '://front_end_website'
    if not repository.exists():
        git(config, 'clone', remote, str(repository))
    if not (repository / '.git').is_dir():
        raise RuntimeError('The existing front_end_website directory is not a Git checkout.')
    if git(config, 'remote', 'get-url', 'origin', cwd=repository, capture=True) != remote:
        raise RuntimeError('The local checkout points to another repository.')
    git(config, 'config', 'user.name', config['commit_author'], cwd=repository)
    git(config, 'config', 'user.email', config['report_email'], cwd=repository)
    git(config, 'fetch', 'origin', 'main', cwd=repository)
    if git(config, 'branch', '--show-current', cwd=repository, capture=True) != 'main':
        raise RuntimeError('The lab checkout must use main.')
    if not git(config, 'status', '--porcelain', cwd=repository, capture=True):
        git(config, 'merge', '--ff-only', 'origin/main', cwd=repository)
    return repository


def push_title(config):
    repository = clone_repository(config)
    path = repository / 'test.html'
    if not path.exists():
        raise RuntimeError('test.html is missing before the title exercise.')
    contents = path.read_text()
    if '<title>Test page</title>' in contents:
        dirty = git(config, 'status', '--porcelain', cwd=repository, capture=True)
        if dirty:
            raise RuntimeError('The repository has unrelated local changes before the title exercise.')
        path.write_text(contents.replace('<title>Test page</title>', '<title>Best test page ever.</title>'))
    elif '<title>Best test page ever.</title>' not in contents:
        raise RuntimeError('The test page contains an unexpected title.')
    changed = git(config, 'status', '--porcelain', cwd=repository, capture=True).splitlines()
    if any(line[3:] != 'test.html' for line in changed):
        raise RuntimeError('The repository has unrelated changes; refusing to include them in the lab commit.')
    if changed:
        git(config, 'add', '--', 'test.html', cwd=repository)
        git(config, 'commit', '-m', 'Updated the title', cwd=repository)
    git(config, 'push', 'origin', 'main', cwd=repository)
    report_git(config, repository)


def publish_website(config):
    repository = clone_repository(config)
    website = ROOT / 'resources/website'
    if (repository / 'index.html').exists() and not (repository / 'test.html').exists():
        if git(config, 'status', '--porcelain', cwd=repository, capture=True):
            raise RuntimeError('The published website checkout has local changes.')
        if website.exists():
            shutil.rmtree(website)
        report_git(config, repository)
        return
    if not website.is_dir():
        raise RuntimeError('The prepared website directory is missing.')
    expected = {path.relative_to(website).as_posix(): path for path in website.rglob('*')
                if path.is_file() and path.suffix != '.md'}
    dirty = git(config, 'status', '--porcelain', cwd=repository, capture=True).splitlines()
    for line in dirty:
        name = line[3:]
        if name == 'test.html' and not (repository / name).exists():
            continue
        if name not in expected or not (repository / name).is_file() or (repository / name).read_bytes() != expected[name].read_bytes():
            raise RuntimeError('Unrelated changes exist in the repository: ' + name)
    (repository / 'test.html').unlink(missing_ok=True)
    for name, source in expected.items():
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    git(config, 'add', '-A', '--', '.', cwd=repository)
    if git(config, 'status', '--porcelain', cwd=repository, capture=True):
        git(config, 'commit', '-m', 'Providing the website', cwd=repository)
    git(config, 'push', 'origin', 'main', cwd=repository)
    shutil.rmtree(website)
    report_git(config, repository)


def report_git(config, repository):
    result = {'commit_id': git(config, 'rev-parse', 'HEAD', cwd=repository, capture=True),
              'clean': not bool(git(config, 'status', '--porcelain', cwd=repository, capture=True)),
              'website_source_removed': not (ROOT / 'resources/website').exists()}
    print('LAB13_GIT_RESULT ' + json.dumps(result), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'title', 'website', 'verify'])
    args = parser.parse_args()
    dependencies()
    WORK.mkdir(exist_ok=True)
    config = json.loads((WORK / 'config.json').read_text())
    import boto3
    if boto3.Session(region_name=config['region']).client('sts').get_caller_identity()['Account'] != config['account']:
        raise RuntimeError('The Lab IDE belongs to a different account.')
    if args.action == 'prepare':
        prepare(config)
    elif args.action == 'title':
        push_title(config)
    elif args.action == 'website':
        publish_website(config)
    else:
        report_git(config, clone_repository(config))


if __name__ == '__main__':
    main()


