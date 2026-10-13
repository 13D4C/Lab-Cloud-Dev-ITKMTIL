import argparse
import base64
from datetime import datetime, timedelta, timezone
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
import urllib.request

from runtime import ROOT, TOOLS, aws_session, find_terraform, load_environment
from source import prepare_source


def wait_for(description, operation, timeout=1200):
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


def canonical_policy(value):
    if isinstance(value, dict):
        return {key: canonical_policy(item) for key, item in value.items()}
    if isinstance(value, list):
        items = [canonical_policy(item) for item in value]
        return items[0] if len(items) == 1 else sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
    return value


def discover(session, environment):
    account = session.client('sts').get_caller_identity()['Account']
    cf = session.client('cloudformation')
    candidates = [x for page in cf.get_paginator('describe_stacks').paginate() for x in page['Stacks']
                  if not x.get('ParentId') and x['StackName'].startswith(environment.get('LAB_STACK_NAME', 'c'))
                  and not x['StackStatus'].startswith('DELETE')]
    if len(candidates) != 1:
        raise RuntimeError('Expected one AWS Academy lab stack. Set LAB_STACK_NAME in .env to select the intended lab.')
    name = candidates[0]['StackName']
    def ready():
        stack = cf.describe_stacks(StackName=name)['Stacks'][0]
        status = stack['StackStatus']
        if any(word in status for word in ('FAILED', 'ROLLBACK', 'DELETE')):
            raise RuntimeError('AWS Academy stack is ' + status)
        return stack if status in ('CREATE_COMPLETE', 'UPDATE_COMPLETE') else None
    stack = wait_for('AWS Academy to finish preparing Lab 3', ready)
    resources = {x['LogicalResourceId']: x['PhysicalResourceId']
                 for page in cf.get_paginator('list_stack_resources').paginate(StackName=name)
                 for x in page['StackResourceSummaries']}
    if 'VSCodeStack' not in resources or 'lockdown' not in resources:
        raise RuntimeError('These credentials do not belong to the expected Lab 3 environment.')
    outputs = {x['OutputKey']: x['OutputValue'] for x in stack.get('Outputs', [])}
    def nested_outputs(stack_id):
        nested = cf.describe_stacks(StackName=stack_id)['Stacks'][0]
        values = {x['OutputKey']: x['OutputValue'] for x in nested.get('Outputs', [])}
        if not (values.get('LabIDEPassword') or values.get('RandomCharacters')):
            for page in cf.get_paginator('list_stack_resources').paginate(StackName=stack_id):
                for x in page['StackResourceSummaries']:
                    if x['ResourceType'] == 'AWS::CloudFormation::Stack':
                        values.update(nested_outputs(x['PhysicalResourceId']))
        return values
    if not outputs.get('LabIDEPassword'):
        nested = nested_outputs(resources['VSCodeStack'])
        outputs['LabIDEPassword'] = nested.get('LabIDEPassword', nested.get('RandomCharacters'))
        outputs.setdefault('LabIDEURL', nested.get('LabWorkspaceURL'))
    if not outputs.get('LabIDEURL') or not outputs.get('LabIDEPassword'):
        raise RuntimeError('Lab IDE URL/password are missing from the AWS Academy stack outputs.')
    prefix = environment.get('BUCKET_PREFIX') or 'cafe'
    prefix = prefix.lower()
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,19}', prefix):
        raise RuntimeError('BUCKET_PREFIX must contain 1-20 lowercase letters, numbers or hyphens and start with a letter.')
    previous = json.loads((TOOLS / 'lab.json').read_text()) if (TOOLS / 'lab.json').exists() else {}
    bucket = environment.get('SITE_BUCKET')
    if not bucket and previous.get('account_id') == account and previous.get('stack_name') == name:
        bucket = previous['site_bucket']
    if not bucket:
        date = datetime.now(timezone(timedelta(hours=7))).date().isoformat()
        bucket = f'{prefix}-{account}-{date}-s3site'
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', bucket):
        raise RuntimeError('SITE_BUCKET must be a valid S3 bucket name without dots.')
    cidr = environment.get('CLIENT_CIDR')
    if not cidr:
        with urllib.request.urlopen('https://checkip.amazonaws.com', timeout=30) as response:
            cidr = response.read().decode().strip() + '/32'
    network = ipaddress.ip_network(cidr, strict=True)
    if network.version != 4 or network.prefixlen != 32 or not network.network_address.is_global:
        raise RuntimeError('CLIENT_CIDR must be your public IPv4 address followed by /32.')
    return dict(account_id=account, aws_region=session.region_name, stack_name=name, site_bucket=bucket,
                client_cidr=cidr, lab_ide_url=outputs['LabIDEURL'],
                website_url=f'https://{bucket}.s3.{session.region_name}.amazonaws.com/index.html'), outputs


def guard_state(config):
    TOOLS.mkdir(exist_ok=True)
    identity = {key: config[key] for key in ('account_id', 'stack_name', 'site_bucket')}
    path = TOOLS / 'lab.json'
    if path.exists() and json.loads(path.read_text()) != identity:
        archive = TOOLS / 'previous-labs' / str(time.time_ns())
        archive.mkdir(parents=True)
        for state in ROOT.glob('terraform.tfstate*'):
            shutil.move(str(state), archive / state.name)
        print('New lab detected; previous local state archived.', flush=True)
    if not path.exists() and (ROOT / 'terraform.tfstate').exists():
        raise RuntimeError('Existing state has no lab identity. Restore .tools/lab.json before continuing.')
    path.write_text(json.dumps(identity))


def import_existing(session, config, execute):
    tracked = set(execute('state', 'list', capture=True).splitlines()) if (ROOT / 'terraform.tfstate').exists() else set()
    s3 = session.client('s3')
    bucket = config['site_bucket']
    try:
        s3.head_bucket(Bucket=bucket, ExpectedBucketOwner=config['account_id'])
    except s3.exceptions.ClientError as error:
        if error.response['Error']['Code'] in ('404', 'NoSuchBucket', 'NotFound'):
            return
        raise RuntimeError('The requested bucket exists but is not accessible in this lab account.') from None
    location = s3.get_bucket_location(Bucket=bucket).get('LocationConstraint') or 'us-east-1'
    if location != config['aws_region']:
        raise RuntimeError('The existing bucket belongs to a different AWS region.')
    if 'aws_s3_bucket.website' in tracked:
        state = json.loads(execute('show', '-json', capture=True))
        previous = next(x for x in state['values']['root_module']['resources'] if x['address'] == 'aws_s3_bucket.website')
        if previous['values'].get('bucket') != bucket:
            raise RuntimeError('The previous bucket resource belongs to a different bucket.')
        execute('state', 'rm', 'aws_s3_bucket.website')
    for address, operation, missing in (
        ('aws_s3_bucket_public_access_block.website', s3.get_public_access_block, 'NoSuchPublicAccessBlockConfiguration'),
        ('aws_s3_bucket_policy.website', s3.get_bucket_policy, 'NoSuchBucketPolicy'),
    ):
        if address in tracked:
            continue
        try:
            operation(Bucket=bucket)
        except s3.exceptions.ClientError as error:
            if error.response['Error']['Code'] != missing:
                raise
        else:
            execute('import', '-input=false', '-no-color', address, bucket)


def apply_plan(execute):
    output = execute('plan', '-input=false', '-no-color', '-out=.tools/apply.tfplan', capture=True)
    (TOOLS / 'plan.log').write_text(output)
    plan = json.loads(execute('show', '-json', '.tools/apply.tfplan', capture=True))
    changes = [x['address'] for x in plan.get('resource_changes', []) if 'delete' in x['change']['actions'] and x['address'] != 'terraform_data.website']
    if changes:
        raise RuntimeError('Refusing unexpected resource replacement/deletion: ' + ', '.join(changes))
    counts = {action: sum(action in x['change']['actions'] for x in plan.get('resource_changes', [])) for action in ('create', 'update', 'delete')}
    print('Terraform plan: ' + json.dumps(counts) + '; details: ' + str(TOOLS / 'plan.log'), flush=True)
    execute('apply', '-input=false', '-no-color', '.tools/apply.tfplan')


def bundle(config, verify_only):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        for filename in ('deploy.py', 'source.py', 'mime-types.json'):
            archive.add(ROOT / filename, arcname='.lab3/' + filename)
        data = json.dumps(dict(config, verify_only=verify_only)).encode()
        entry = tarfile.TarInfo('.lab3/config.json')
        entry.mode = 0o600
        entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))
    payload = base64.b64encode(output.getvalue()).decode()
    return "set -euo pipefail\ncd /home/ec2-user/environment\nmkdir -p .lab3\nbase64 -d <<'LAB3_BUNDLE' | tar xz\n" + payload + "\nLAB3_BUNDLE\npython3 -u .lab3/deploy.py\n"


def prepare_ide(config, outputs, verify_only):
    from lab_ide import LabIDE
    remote = LabIDE(outputs['LabIDEURL'], outputs['LabIDEPassword'])
    try:
        if 'LAB3_IDE_VERIFIED' not in remote.run(bundle(config, verify_only), timeout=1200):
            raise RuntimeError('IDE preparation did not finish.')
    finally:
        remote.close()


def verify(session, config, website):
    s3 = session.client('s3')
    bucket = config['site_bucket']
    expected = {x.relative_to(website).as_posix(): x for x in website.rglob('*') if x.is_file()}
    objects = {x['Key']: x for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket) for x in page.get('Contents', [])}
    for key, path in expected.items():
        if key not in objects or objects[key]['ETag'].strip('"') != hashlib.md5(path.read_bytes()).hexdigest():
            raise RuntimeError('Website upload is incomplete or differs from the official source: ' + key)
    flags = s3.get_public_access_block(Bucket=bucket)['PublicAccessBlockConfiguration']
    if flags != dict(BlockPublicAcls=True, IgnorePublicAcls=True, BlockPublicPolicy=False, RestrictPublicBuckets=True):
        raise RuntimeError('S3 public-access-block settings do not match the lab.')
    if canonical_policy(json.loads(s3.get_bucket_policy(Bucket=bucket)['Policy'])) != canonical_policy(config['policy']):
        raise RuntimeError('The bucket policy does not match the required IP restriction and signed report protection.')
    if s3.get_bucket_policy_status(Bucket=bucket)['PolicyStatus']['IsPublic']:
        raise RuntimeError('S3 considers the bucket policy public instead of limited to the client IP.')
    try:
        s3.get_bucket_website(Bucket=bucket)
    except s3.exceptions.ClientError as error:
        if error.response['Error']['Code'] != 'NoSuchWebsiteConfiguration':
            raise
    else:
        raise RuntimeError('This lab uses S3 object URLs and expects no bucket website-hosting configuration.')
    types = json.loads((ROOT / 'mime-types.json').read_text())
    base = config['website_url'].rsplit('/', 1)[0]
    for key in ('index.html', 'config.js', 'scripts/main.js', 'styles/main.css', 'all_products.json', 'images/logo.png'):
        try:
            with urllib.request.urlopen(base + '/' + key, timeout=30) as response:
                if response.read() != expected[key].read_bytes():
                    raise RuntimeError('The website returned unexpected content for ' + key)
                if response.headers.get_content_type() != types[expected[key].suffix[1:]]:
                    raise RuntimeError('Unexpected website content type for ' + key)
                if response.headers.get('Cache-Control') != 'max-age=0':
                    raise RuntimeError('Website development caching is enabled for ' + key)
        except urllib.error.HTTPError as error:
            if error.code == 403:
                raise RuntimeError('Website access was denied from this computer. Set CLIENT_CIDR to its public IPv4 address and run again.') from None
            raise
    configuration = (website / 'config.js').read_text()
    if not re.search(r'API_GW_BASE_URL_STR\s*:\s*null', configuration) or not re.search(r'COGNITO_LOGIN_BASE_URL_STR\s*:\s*null', configuration):
        raise RuntimeError('The starter website API and Cognito settings should remain null for this lab.')
    if 'No API to call' not in (website / 'scripts/main.js').read_text():
        raise RuntimeError('The starter website login behavior does not match the lab.')
    print(f'Verified: {len(expected)} official website files, IP-only object access, HTTP 403 from the IDE, signed-report policy and starter login configuration.', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    environment = load_environment()
    session = aws_session(environment)
    config, outputs = discover(session, environment)
    print('Lab 3 account: ' + config['account_id'] + '; bucket: ' + config['site_bucket'] + '; allowed IP: ' + config['client_cidr'], flush=True)
    guard_state(config)
    website = prepare_source(TOOLS, TOOLS / 'source')
    for key in ('account_id', 'stack_name', 'site_bucket', 'client_cidr', 'lab_ide_url'):
        environment['TF_VAR_' + key] = config[key]
    environment['TF_VAR_python_executable'] = sys.executable
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
        import_existing(session, config, execute)
        apply_plan(execute)
    state = json.loads(execute('show', '-json', capture=True))
    resource = next(x for x in state['values']['root_module']['resources'] if x['address'] == 'aws_s3_bucket_policy.website')
    config['policy'] = json.loads(resource['values']['policy'])
    prepare_ide(config, outputs, args.verify_only)
    verify(session, config, website)
    print('Lab 3 completed. Submit in AWS Academy to record your grade.')
    print('Website: ' + config['website_url'])
    print('Allowed client IP: ' + config['client_cidr'])
    print('Lab IDE: ' + config['lab_ide_url'])


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        if isinstance(error, subprocess.CalledProcessError) and error.stdout:
            print(error.stdout, file=sys.stderr)
        print('Lab 3 failed: ' + str(error), file=sys.stderr)
        sys.exit(1)
