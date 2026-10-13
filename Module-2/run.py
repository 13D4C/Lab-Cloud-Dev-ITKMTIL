import argparse
import base64
import io
import json
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request

from runtime import ROOT, TOOLS, aws_session, find_terraform, load_environment


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
    stack = wait_for('AWS Academy to finish preparing Lab 2', ready)
    resources = {x['LogicalResourceId']: x['PhysicalResourceId']
                 for page in cf.get_paginator('list_stack_resources').paginate(StackName=name)
                 for x in page['StackResourceSummaries']}
    if not all(key in resources for key in ('SampleBucket', 'VSCodeStack')):
        raise RuntimeError('These credentials do not belong to the expected Lab 2 environment.')
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
    session.client('s3').head_bucket(Bucket=resources['SampleBucket'])
    return dict(account_id=account, aws_region=session.region_name, stack_name=name,
                sample_bucket=resources['SampleBucket'], lab_ide_url=outputs['LabIDEURL']), outputs


def guard_state(config):
    TOOLS.mkdir(exist_ok=True)
    identity = {key: config[key] for key in ('account_id', 'stack_name', 'sample_bucket')}
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
    for address, operation, missing in (
        ('aws_s3_bucket_ownership_controls.sample', s3.get_bucket_ownership_controls, 'OwnershipControlsNotFoundError'),
        ('aws_s3_bucket_public_access_block.sample', s3.get_public_access_block, 'NoSuchPublicAccessBlockConfiguration'),
    ):
        if address in tracked:
            continue
        try:
            operation(Bucket=config['sample_bucket'])
        except s3.exceptions.ClientError as error:
            if error.response['Error']['Code'] != missing:
                raise
        else:
            execute('import', '-input=false', '-no-color', address, config['sample_bucket'])


def apply_plan(execute):
    execute('plan', '-input=false', '-no-color', '-out=.tools/apply.tfplan')
    plan = json.loads(execute('show', '-json', '.tools/apply.tfplan', capture=True))
    changes = [x['address'] for x in plan.get('resource_changes', []) if 'delete' in x['change']['actions']]
    if changes:
        raise RuntimeError('Refusing unexpected resource replacement/deletion: ' + ', '.join(changes))
    execute('apply', '-input=false', '-no-color', '.tools/apply.tfplan')


def bundle(config):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        for filename in ('deploy.py', 'index.html', 's3-permissions.py'):
            archive.add(ROOT / filename, arcname='.lab2/' + filename)
        data = json.dumps(config).encode()
        entry = tarfile.TarInfo('.lab2/config.json')
        entry.mode = 0o600
        entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))
    payload = base64.b64encode(output.getvalue()).decode()
    return "set -euo pipefail\ncd /home/ec2-user/environment\nmkdir -p .lab2\nbase64 -d <<'LAB2_BUNDLE' | tar xz\n" + payload + "\nLAB2_BUNDLE\npython3 -u .lab2/deploy.py\n"


def prepare_ide(config, outputs, verify_only=False):
    from lab_ide import LabIDE
    remote = LabIDE(outputs['LabIDEURL'], outputs['LabIDEPassword'])
    try:
        if 'LAB2_IDE_VERIFIED' not in remote.run(bundle(dict(config, verify_only=verify_only)), timeout=900):
            raise RuntimeError('IDE preparation did not finish.')
    finally:
        remote.close()


def verify(session, config, values):
    s3 = session.client('s3')
    bucket = config['sample_bucket']
    for filename in ('list-buckets.py', 'index.html'):
        value = s3.get_object(Bucket=bucket, Key=filename)
        if value['Body'].read() != (ROOT / filename).read_bytes():
            raise RuntimeError('Unexpected S3 object content: ' + filename)
    ownership = s3.get_bucket_ownership_controls(Bucket=bucket)['OwnershipControls']['Rules'][0]['ObjectOwnership']
    if ownership != 'BucketOwnerPreferred':
        raise RuntimeError('The bucket ownership setting does not match the lab.')
    if any(s3.get_public_access_block(Bucket=bucket)['PublicAccessBlockConfiguration'].values()):
        raise RuntimeError('The bucket public-access-block settings do not match the lab.')
    grants = s3.get_object_acl(Bucket=bucket, Key='index.html')['Grants']
    if not any(x['Grantee'].get('URI') == 'http://acs.amazonaws.com/groups/global/AllUsers' and x['Permission'] == 'READ' for x in grants):
        raise RuntimeError('The Hello World page does not have the required public-read ACL.')
    with urllib.request.urlopen(values['hello_world_url'], timeout=30) as response:
        if response.read() != (ROOT / 'index.html').read_bytes() or response.headers.get_content_type() != 'text/html':
            raise RuntimeError('The public Hello World page did not load correctly.')
    script_url = values['hello_world_url'].rsplit('/', 1)[0] + '/list-buckets.py'
    try:
        urllib.request.urlopen(script_url, timeout=30)
    except urllib.error.HTTPError as error:
        if error.code != 403:
            raise
    else:
        raise RuntimeError('The Python script should remain private.')
    print('Verified: both S3 files, BucketOwnerPreferred, public-read index.html, working Hello World URL and private Python source.', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    environment = load_environment()
    session = aws_session(environment)
    config, outputs = discover(session, environment)
    print('Lab 2 account: ' + config['account_id'] + '; bucket: ' + config['sample_bucket'], flush=True)
    guard_state(config)
    for key in ('account_id', 'sample_bucket', 'lab_ide_url'):
        environment['TF_VAR_' + key] = config[key]
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
        from cloudshell import prepare
        prepare(session, config, wait_for)
    values = {k: v['value'] for k, v in json.loads(execute('output', '-json', capture=True)).items()}
    prepare_ide(config, outputs, verify_only=args.verify_only)
    if not args.verify_only:
        session.client('s3').put_object_acl(Bucket=config['sample_bucket'], Key='index.html', ACL='public-read')
    verify(session, config, values)
    print('Lab 2 completed. Submit in AWS Academy to record your grade.')
    print('Hello World: ' + values['hello_world_url'])
    print('Lab IDE: ' + values['lab_ide_url'])


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print('Lab 2 failed: ' + str(error), file=sys.stderr)
        sys.exit(1)
