import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


ROOT = Path('/home/ec2-user/environment')
LABEL = 'aws-academy.lab=8.1'


def command(*args, capture=False, environment=None, input=None):
    result = subprocess.run(args, check=True, text=True, input=input, env=environment, stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else None


def get(url, data=None):
    payload = urllib.parse.urlencode(data).encode() if data is not None else None
    with urllib.request.urlopen(url, data=payload, timeout=15) as response:
        return response.read().decode()


def wait_for(action, description, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = action()
            if result:
                return result
        except (OSError, subprocess.CalledProcessError):
            pass
        time.sleep(3)
    raise RuntimeError(f'Timed out waiting for {description}.')


def inspect(name):
    result = subprocess.run(['docker', 'inspect', name], text=True, capture_output=True)
    if result.returncode:
        return None
    container = json.loads(result.stdout)[0]
    if container['Config'].get('Labels', {}).get('aws-academy.lab') != '8.1':
        raise RuntimeError(f'{name} already exists and is not managed by this runner. Rename it before retrying.')
    return container


def database_query(sql):
    return command('docker', 'exec', '-e', 'MYSQL_PWD=rootpw', 'mysql_1', 'mysql', '-uroot', '-N', '-B', '-e', sql, capture=True)


def verify_website(base, migrated=False):
    page = wait_for(lambda: get(base + '/suppliers'), 'supplier website')
    if 'Nikki Wolf' not in page or (migrated and '100 Container Street' not in page):
        raise RuntimeError('The migrated supplier record was not found.')
    marker = 'OpenTofu Verify ' + uuid.uuid4().hex[:8]
    fields = {'name': marker, 'address': '200 Test Street', 'city': 'Anytown', 'state': 'CA', 'email': 'verify@example.com', 'phone': '4155551212'}
    supplier_id = None
    try:
        page = get(base + '/supplier-add', fields)
        match = re.search(r'<tr[^>]*>\s*<td[^>]*>\s*' + re.escape(marker) + r'.*?/supplier-update/(\d+)', page, re.S)
        if not match:
            for row in re.findall(r'<tr[^>]*>(.*?)</tr>', page, re.S):
                if marker in row:
                    match = re.search(r'/supplier-update/(\d+)', row)
                    break
        if not match:
            raise RuntimeError('Creating a supplier through the website failed.')
        supplier_id = match.group(1)
        fields.update(id=supplier_id, address='201 Updated Street', phone='4155551213')
        page = get(base + '/supplier-update', fields)
        if '201 Updated Street' not in page or '4155551213' not in page:
            raise RuntimeError('Updating a supplier through the website failed.')
    finally:
        if supplier_id:
            get(base + '/supplier-remove/' + supplier_id, {})
    if marker in get(base + '/suppliers'):
        raise RuntimeError('Deleting the verification supplier failed.')
    print('Supplier website create/update/delete: passed', flush=True)


def main(config):
    print('Checking Lab IDE tools...', flush=True)
    for tool in ('docker', 'mysqldump', 'aws'):
        if not shutil.which(tool):
            raise RuntimeError(f'{tool} is not installed on Lab IDE.')
    command('docker', 'info', capture=True)
    node_directory = ROOT / 'containers/node_app/codebase_partner'
    mysql_directory = ROOT / 'containers/mysql'
    state_directory = ROOT / '.lab81'
    state_directory.mkdir(exist_ok=True)
    db = inspect('mysql_1')
    inspect('node_app_1')
    print('Building Node application image...', flush=True)
    command('docker', 'build', '--progress=plain', '--tag', 'node_app:latest', str(node_directory))
    if not db:
        print('Testing the original supplier application...', flush=True)
        original = f"http://{config['app_host']}"
        page = wait_for(lambda: get(original + '/suppliers'), 'original supplier application')
        if 'Nikki Wolf' not in page:
            get(original + '/supplier-add', {'name': 'Nikki Wolf', 'address': '100 Main Street', 'city': 'Anytown', 'state': 'CA', 'email': 'nwolf@example.com', 'phone': '4155551212'})
        page = get(original + '/suppliers')
        for row in re.findall(r'<tr[^>]*>(.*?)</tr>', page, re.S):
            if 'Nikki Wolf' in row:
                match = re.search(r'/supplier-update/(\d+)', row)
                if match:
                    get(original + '/supplier-update', {'id': match.group(1), 'name': 'Nikki Wolf', 'address': '100 Main Street', 'city': 'Anytown', 'state': 'CA', 'email': 'nwolf@example.com', 'phone': '4155551213'})
                break
        node = inspect('node_app_1')
        if node:
            command('docker', 'rm', '-f', 'node_app_1')
        command('docker', 'run', '-d', '--name', 'node_app_1', '--label', LABEL, '-p', '3000:3000', '-e', f"APP_DB_HOST={config['mysql_host']}", 'node_app:latest')
        verify_website('http://127.0.0.1:3000')
        print('Exporting the original COFFEE database...', flush=True)
        environment = os.environ.copy()
        environment['MYSQL_PWD'] = 'coffee'
        dump = command('mysqldump', '-h', config['mysql_host'], '-P', '3306', '-u', 'nodeapp', '--single-transaction', '--skip-lock-tables', '--databases', 'COFFEE', capture=True, environment=environment)
        dump = '\n'.join(line for line in dump.splitlines() if 'enable the sandbox mode' not in line)
        if 'Nikki Wolf' not in dump or '100 Main Street' not in dump:
            raise RuntimeError('Original supplier data was not present in the database export.')
        dump = dump.replace('100 Main Street', '100 Container Street')
        (mysql_directory / 'my_sql.sql').write_text(dump + '\n')
        print('Building MySQL image...', flush=True)
        command('docker', 'build', '--progress=plain', '--tag', 'mysql_server:latest', str(mysql_directory))
        existing_volume = subprocess.run(['docker', 'volume', 'inspect', 'lab81_mysql_data'], text=True, capture_output=True)
        if existing_volume.returncode == 0 and (json.loads(existing_volume.stdout)[0].get('Labels') or {}).get('aws-academy.lab') != '8.1':
            raise RuntimeError('lab81_mysql_data already exists and is not managed by this runner.')
        command('docker', 'volume', 'create', '--label', LABEL, 'lab81_mysql_data')
        command('docker', 'run', '-d', '--name', 'mysql_1', '--label', LABEL, '--restart', 'unless-stopped', '-p', '3306:3306', '-e', 'MYSQL_ROOT_PASSWORD=rootpw', '-v', 'lab81_mysql_data:/var/lib/mysql', 'mysql_server:latest')
    else:
        command('docker', 'start', 'mysql_1')
    wait_for(lambda: database_query('SELECT 1') == '1', 'MySQL container', timeout=240)
    migrated = database_query("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='COFFEE' AND table_name='suppliers'")
    if migrated == '0':
        print('Importing COFFEE into MySQL container...', flush=True)
        dump_path = mysql_directory / 'my_sql.sql'
        if not dump_path.exists():
            raise RuntimeError('Database export is missing. The existing MySQL volume was preserved.')
        command('docker', 'exec', '-i', '-e', 'MYSQL_PWD=rootpw', 'mysql_1', 'mysql', '-uroot', input=dump_path.read_text())
    database_query("CREATE USER IF NOT EXISTS 'nodeapp'@'%' IDENTIFIED WITH mysql_native_password BY 'coffee'; ALTER USER 'nodeapp'@'%' IDENTIFIED WITH mysql_native_password BY 'coffee'; GRANT ALL PRIVILEGES ON COFFEE.* TO 'nodeapp'@'%';")
    mysql_ip = json.loads(command('docker', 'inspect', 'mysql_1', capture=True))[0]['NetworkSettings']['IPAddress']
    node = inspect('node_app_1')
    image_id = command('docker', 'image', 'inspect', 'node_app:latest', '--format', '{{.Id}}', capture=True)
    if node and (node['Image'] != image_id or f'APP_DB_HOST={mysql_ip}' not in node['Config']['Env']):
        command('docker', 'rm', '-f', 'node_app_1')
        node = None
    if not node:
        command('docker', 'run', '-d', '--name', 'node_app_1', '--label', LABEL, '--restart', 'unless-stopped', '-p', '3000:3000', '-e', f'APP_DB_HOST={mysql_ip}', 'node_app:latest')
    else:
        command('docker', 'start', 'node_app_1')
    verify_website('http://127.0.0.1:3000', migrated=True)
    print('Publishing Node application image to ECR...', flush=True)
    registry = config['repository_url'].split('/')[0]
    password = command('aws', 'ecr', 'get-login-password', '--region', config['region'], capture=True)
    with tempfile.TemporaryDirectory(prefix='lab81-docker-') as docker_config:
        environment = os.environ.copy()
        environment['DOCKER_CONFIG'] = docker_config
        command('docker', 'login', '--username', 'AWS', '--password-stdin', registry, input=password + '\n', environment=environment)
        command('docker', 'tag', 'node_app:latest', config['repository_url'] + ':latest')
        command('docker', 'push', '--quiet', config['repository_url'] + ':latest', environment=environment)
    if config.get('prepare_grading'):
        prepare_grading(config['region'])
    for name in ('node_app_1', 'mysql_1'):
        container = inspect(name)
        if not container['State']['Running']:
            raise RuntimeError(f'{name} is not running.')
    print('LAB81_COMPLETE ' + json.dumps({'containers': ['node_app_1', 'mysql_1'], 'mysql_host': mysql_ip, 'supplier_crud': 'passed'}), flush=True)


def prepare_grading(region):
    state = ROOT / '.lab81'
    marker = state / 'grading-prepared'
    if marker.exists():
        print('Academy grading key is already prepared.', flush=True)
        return
    print('Preparing the grading key specified in resources/setup.sh...', flush=True)
    key = state / 'grading-key'
    if not key.exists():
        command('ssh-keygen', '-q', '-t', 'rsa', '-b', '2048', '-N', '', '-f', str(key))
    encoded = state / 'grading-key-encoded'
    encoded.write_text(base64.b64encode(key.read_bytes()).decode())
    encoded.chmod(0o600)
    result = subprocess.run(['aws', 'secretsmanager', 'create-secret', '--name', 'c9key', '--secret-string', 'file://' + str(encoded), '--region', region], text=True, capture_output=True)
    encoded.unlink(missing_ok=True)
    if result.returncode:
        raise RuntimeError('Could not create the Academy c9key secret. Existing secrets were not read or replaced. ' + result.stderr.strip())
    ssh = Path.home() / '.ssh'
    ssh.mkdir(mode=0o700, exist_ok=True)
    authorized = ssh / 'authorized_keys'
    public = key.with_suffix('.pub').read_text().strip()
    existing = authorized.read_text() if authorized.exists() else ''
    if public not in existing.splitlines():
        with authorized.open('a') as output:
            output.write(('\n' if existing and not existing.endswith('\n') else '') + public + '\n')
    authorized.chmod(0o600)
    daemon_path = Path('/etc/docker/daemon.json')
    daemon_config = json.loads(daemon_path.read_text()) if daemon_path.exists() else {}
    daemon_config['log-driver'] = 'syslog'
    subprocess.run(['sudo', 'tee', str(daemon_path)], input=json.dumps(daemon_config) + '\n', text=True, stdout=subprocess.PIPE, check=True)
    marker.write_text('c9key\n')
    command('ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new', '-o', 'UserKnownHostsFile=' + str(state / 'known_hosts'), '-i', str(key), 'ec2-user@127.0.0.1', 'docker ps --format "{{.Names}}"')
    print('Academy grading key prepared.', flush=True)


if __name__ == '__main__':
    main(json.loads((ROOT / '.lab81/config.json').read_text()))
