import importlib
import json
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request

from source import prepare_source


ROOT = Path('/home/ec2-user/environment')
WORK = ROOT / '.lab3'


def command(*arguments):
    result = subprocess.run(arguments, text=True, capture_output=True)
    print(result.stdout, end='', flush=True)
    if result.stderr:
        print(result.stderr, end='', flush=True)
    result.check_returncode()
    return result.stdout


def main():
    config = json.loads((WORK / 'config.json').read_text())
    verify_only = config.get('verify_only', False)
    if 'aws-cli/2.' not in command('aws', '--version'):
        raise RuntimeError('The provided IDE must have AWS CLI version 2.')
    try:
        import boto3
    except ImportError:
        if verify_only:
            raise RuntimeError('Boto3 is missing from the IDE. Run without --verify-only to install it.')
        if subprocess.run([sys.executable, '-m', 'pip', '--version'], capture_output=True).returncode:
            command('sudo', 'dnf', 'install', '-y', 'python3-pip')
        command('sudo', sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', 'boto3>=1.34,<2')
        importlib.invalidate_caches()
        import boto3
    session = boto3.Session(region_name=config['aws_region'])
    if session.client('sts').get_caller_identity()['Account'] != config['account_id']:
        raise RuntimeError('The IDE belongs to a different AWS account.')
    website = ROOT / 'resources/website'
    policy_path = ROOT / 'website_security_policy.json'
    if not verify_only:
        website = prepare_source(WORK, ROOT)
        policy_path.write_text(json.dumps(config['policy'], indent=2) + '\n')
        permissions = ROOT / 'python_3/permissions.py'
        permissions.write_text(permissions.read_text().replace('<bucket-name>', config['site_bucket']))
        command(sys.executable, str(permissions))
        content_types = json.loads((WORK / 'mime-types.json').read_text())
        files = sorted(x for x in website.rglob('*') if x.is_file())
        s3 = session.client('s3')
        for path in files:
            s3.upload_file(str(path), config['site_bucket'], path.relative_to(website).as_posix(), ExtraArgs={
                'ContentType': content_types.get(path.suffix[1:].lower(), 'application/octet-stream'),
                'CacheControl': 'max-age=0',
            })
        print(f'Uploaded {len(files)} website files with Boto3.', flush=True)
    if json.loads(policy_path.read_text()) != config['policy']:
        raise RuntimeError('The IDE policy file does not match the Terraform configuration.')
    if not (website / 'index.html').exists() or not (ROOT / 'python_3/permissions.py').exists():
        raise RuntimeError('Required lab files are missing from the IDE.')
    with urllib.request.urlopen('https://checkip.amazonaws.com', timeout=30) as response:
        ide_ip = response.read().decode().strip()
    if ide_ip == config['client_cidr'].split('/')[0]:
        raise RuntimeError('The allowed client IP equals the IDE IP; use your own computer public IPv4 address.')
    try:
        urllib.request.urlopen(config['website_url'], timeout=30)
    except urllib.error.HTTPError as error:
        if error.code != 403 or b'AccessDenied' not in error.read():
            raise
    else:
        raise RuntimeError('Anonymous access from the IDE network should be denied.')
    print('Verified: anonymous website access from the IDE network is denied with HTTP 403.', flush=True)
    print('Boto3 version: ' + boto3.__version__, flush=True)
    print('LAB3_IDE_VERIFIED', flush=True)


if __name__ == '__main__':
    main()
