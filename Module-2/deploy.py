import importlib
import json
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path('/home/ec2-user/environment')
WORK = ROOT / '.lab2'


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
    version = command('aws', '--version')
    if 'aws-cli/2.' not in version:
        raise RuntimeError('AWS CLI version 2 is required on the provided IDE.')
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
    bucket = config['sample_bucket']
    if bucket not in command('aws', 's3', 'ls'):
        raise RuntimeError('The sample bucket was not returned by the AWS CLI.')
    if not verify_only:
        command('aws', 's3', 'cp', f's3://{bucket}/list-buckets.py', str(ROOT / 'list-buckets.py'))
    if bucket not in command(sys.executable, str(ROOT / 'list-buckets.py')):
        raise RuntimeError('The SDK example did not return the sample bucket.')
    if not verify_only:
        shutil.copyfile(WORK / 'index.html', ROOT / 'index.html')
        permissions = (WORK / 's3-permissions.py').read_text().replace('YOUR_BUCKET_NAME', bucket)
        (ROOT / 's3-permissions.py').write_text(permissions)
        command('aws', 's3', 'cp', str(ROOT / 'index.html'), f's3://{bucket}/index.html',
                '--content-type', 'text/html', '--cache-control', 'max-age=0')
    if (ROOT / 'index.html').read_bytes() != (WORK / 'index.html').read_bytes():
        raise RuntimeError('The IDE Hello World file does not match the lab.')
    if bucket not in (ROOT / 's3-permissions.py').read_text():
        raise RuntimeError('The IDE permissions script does not contain the sample bucket name.')
    print('Boto3 version: ' + boto3.__version__, flush=True)
    print('LAB2_IDE_VERIFIED', flush=True)


if __name__ == '__main__':
    main()
