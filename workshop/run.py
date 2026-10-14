import argparse
import hashlib
from http.cookiejar import CookieJar
import json
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

from runtime import ROOT, TOOLS, aws_session, find_terraform, load_environment


SOURCE_REPOSITORY = 'https://github.com/13D4C/Workshop-IT-KMITL-Cloud-Computing-Deployment-Website-Production.git'
SOURCE_REVISION = 'cbd8bcd77e5125619015309f9fc1f642b85a60ff'


def save_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2))
    temporary.chmod(0o600)
    temporary.replace(path)


def wait_for(description, operation, timeout=2400):
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
    raise TimeoutError('Timed out waiting for ' + description + '. Run the same command to resume.')


def secrets_for(environment):
    generated = {}
    for key, value in [('DB_PASSWORD', 'Workshop-' + secrets.token_hex(16)), ('JWT_SECRET', secrets.token_hex(32))]:
        if not environment.get(key) or environment[key].startswith('GENERATED_'):
            generated[key] = value
            environment[key] = value
    if generated:
        path = ROOT / '.env'
        lines = path.read_text().splitlines() if path.exists() else []
        for key, value in generated.items():
            lines = [line for line in lines if line.partition('=')[0].strip().upper() != key]
            lines.append(key.lower() + '=' + value)
        path.write_text('\n'.join(lines) + '\n')
        path.chmod(0o600)


def source_for(environment):
    repository = environment.get('SOURCE_REPOSITORY', SOURCE_REPOSITORY)
    revision = environment.get('SOURCE_REVISION', SOURCE_REVISION)
    if not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?', repository):
        raise RuntimeError('SOURCE_REPOSITORY must be a public HTTPS GitHub repository URL.')
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise RuntimeError('SOURCE_REVISION must be a complete Git commit ID.')
    if not shutil.which('git'):
        raise RuntimeError('Install Git before running the workshop.')
    path = TOOLS / 'source'
    def git(*arguments, capture=False):
        result = subprocess.run(['git', *arguments], cwd=path if path.exists() else ROOT,
                                check=True, text=True, stdout=subprocess.PIPE if capture else None)
        return result.stdout.strip() if capture else None
    created = not path.exists()
    if created:
        subprocess.run(['git', 'clone', '--no-checkout', repository, str(path)], check=True)
    if git('remote', 'get-url', 'origin', capture=True) != repository:
        raise RuntimeError('The cached application belongs to another repository.')
    if not created and git('status', '--porcelain', capture=True):
        raise RuntimeError('The cached application has local edits. Preserve them before continuing.')
    try:
        git('cat-file', '-e', revision + '^{commit}')
    except subprocess.CalledProcessError:
        git('fetch', '--depth', '1', 'origin', revision)
    git('checkout', '--detach', revision)
    for filename in ('deploy/docker-compose.prod.yml', 'backend/cmd/main.go', 'frontend/package.json'):
        if not (path / filename).is_file():
            raise RuntimeError('The application source is incomplete: ' + filename)
    environment['TF_VAR_source_repository'] = repository
    environment['TF_VAR_source_revision'] = revision


def identity_for(session, destroy=False):
    identity = {'account_id': session.client('sts').get_caller_identity()['Account'], 'region': session.region_name}
    path = TOOLS / 'identity.json'
    if path.exists() and json.loads(path.read_text()) != identity:
        state_path = ROOT / 'terraform.tfstate'
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if any(x.get('mode') == 'managed' and x.get('instances') for x in state.get('resources', [])):
            raise RuntimeError('These credentials or this region differ from the workshop state. Restore the original .env or use another directory.')
        for filename in ('configuration.json', 'test-user.json'):
            (TOOLS / filename).unlink(missing_ok=True)
        save_json(path, identity)
    if not path.exists() and (ROOT / 'terraform.tfstate').exists():
        raise RuntimeError('The Terraform state has no matching .tools/identity.json. Restore both before continuing.')
    if destroy and not (ROOT / 'terraform.tfstate').exists():
        raise RuntimeError('There is no workshop state to destroy in this directory.')
    if not path.exists():
        save_json(path, identity)
    return identity


def configuration(session, environment, identity):
    path = TOOLS / 'configuration.json'
    config = json.loads(path.read_text()) if path.exists() else {}
    ec2 = session.client('ec2')
    profile = environment.get('INSTANCE_PROFILE', 'LabInstanceProfile')
    instance_profile = session.client('iam').get_instance_profile(InstanceProfileName=profile)['InstanceProfile']
    zones = environment.get('AVAILABILITY_ZONES', session.region_name + 'a,' + session.region_name + 'b').split(',')
    available = {x['ZoneName'] for x in ec2.describe_availability_zones(Filters=[{'Name':'state','Values':['available']}])['AvailabilityZones']}
    if len(zones) != 2 or len(set(zones)) != 2 or not set(zones).issubset(available):
        raise RuntimeError('AVAILABILITY_ZONES must contain two available AZ names, separated by a comma.')
    ami = environment.get('AMI_ID') or config.get('ami_id')
    if not ami:
        images = ec2.describe_images(Owners=['amazon'], Filters=[
            {'Name':'name','Values':['al2023-ami-2023.*-kernel-*-x86_64']},
            {'Name':'architecture','Values':['x86_64']}, {'Name':'state','Values':['available']},
            {'Name':'virtualization-type','Values':['hvm']}, {'Name':'root-device-type','Values':['ebs']}])['Images']
        if not images:
            raise RuntimeError('No Amazon Linux 2023 x86_64 AMI is available.')
        ami = max(images, key=lambda x:x['CreationDate'])['ImageId']
    version = environment.get('DB_ENGINE_VERSION') or config.get('db_engine_version')
    rds = session.client('rds')
    if not version:
        versions = [x['EngineVersion'] for p in rds.get_paginator('describe_db_engine_versions').paginate(Engine='postgres')
                    for x in p['DBEngineVersions'] if x['EngineVersion'].startswith('16.')]
        version = max(versions, key=lambda x:tuple(int(v) for v in x.split('.')))
    instance_class = environment.get('DB_INSTANCE_CLASS', 'db.t3.micro')
    options = [x for p in rds.get_paginator('describe_orderable_db_instance_options').paginate(
        Engine='postgres', EngineVersion=version, DBInstanceClass=instance_class) for x in p['OrderableDBInstanceOptions']]
    if not any(x.get('MultiAZCapable') and x.get('StorageType') == 'gp2' for x in options):
        raise RuntimeError('The selected PostgreSQL version and instance class do not support Multi-AZ with gp2 storage.')
    key = TOOLS / 'workshop-key.pem'
    if not key.exists():
        if not shutil.which('ssh-keygen'):
            raise RuntimeError('Install OpenSSH to generate workshop-key.pem.')
        subprocess.run(['ssh-keygen', '-q', '-m', 'PEM', '-t', 'rsa', '-b', '2048', '-N', '', '-f', str(key)], check=True)
    key.chmod(0o600)
    if not key.with_suffix('.pem.pub').exists():
        public = subprocess.check_output(['ssh-keygen', '-y', '-f', str(key)], text=True)
        key.with_suffix('.pem.pub').write_text(public)
    config.update(account_id=identity['account_id'], aws_region=session.region_name, availability_zones=zones,
                  ami_id=ami, db_engine_version=version, db_instance_class=instance_class,
                  instance_profile=profile, lab_role_arn=instance_profile['Roles'][0]['Arn'],
                  instance_type=environment.get('INSTANCE_TYPE', 't3.micro'),
                  assets_bucket_name=environment.get('ASSETS_BUCKET_NAME', 'itkmitl-workshop-assets-' + identity['account_id']))
    for name, value in config.items():
        environment['TF_VAR_' + name] = value if isinstance(value,str) else json.dumps(value)
    environment['TF_VAR_ssh_public_key'] = key.with_suffix('.pem.pub').read_text().strip()
    environment['TF_VAR_db_password'] = environment['DB_PASSWORD']
    environment['TF_VAR_jwt_secret'] = environment['JWT_SECRET']
    save_json(path, config)
    return config


def destroy_configuration(environment):
    path = TOOLS / 'configuration.json'
    if not path.exists():
        raise RuntimeError('Restore .tools/configuration.json with the Terraform state before cleanup.')
    config = json.loads(path.read_text())
    for name, value in config.items():
        environment['TF_VAR_' + name] = value if isinstance(value, str) else json.dumps(value)
    public = TOOLS / 'workshop-key.pem.pub'
    environment['TF_VAR_ssh_public_key'] = public.read_text().strip() if public.exists() else 'unused-on-destroy'
    environment['TF_VAR_db_password'] = environment.get('DB_PASSWORD', 'unused-on-destroy')
    environment['TF_VAR_jwt_secret'] = environment.get('JWT_SECRET', 'unused-on-destroy-' * 3)
    return config


def conflicts(session):
    if (ROOT / 'terraform.tfstate').exists():
        return
    ec2 = session.client('ec2')
    if ec2.describe_vpcs(Filters=[{'Name':'tag:Name','Values':['workshop-vpc']}])['Vpcs']:
        raise RuntimeError('workshop-vpc already exists without local Terraform state. Restore its original state before running here.')
    if session.client('autoscaling').describe_auto_scaling_groups(AutoScalingGroupNames=['workshop-asg'])['AutoScalingGroups']:
        raise RuntimeError('workshop-asg already exists. Restore the original Terraform state.')
    for item in session.client('rds').describe_db_instances()['DBInstances']:
        if item['DBInstanceIdentifier'] == 'workshop-db':
            raise RuntimeError('workshop-db already exists. Restore the original Terraform state.')


def http(opener, base, path, method='GET', payload=None, token=None):
    headers = {'Content-Type':'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    request = urllib.request.Request(base + path, data=json.dumps(payload).encode() if payload is not None else None,
                                     headers=headers, method=method)
    with opener.open(request, timeout=30) as response:
        data = response.read()
        return json.loads(data) if response.headers.get_content_type() == 'application/json' else data


def wait_ready(session, outputs, timeout=2400, availability_probe=None, previous_instances=None):
    scaling = session.client('autoscaling')
    elb = session.client('elbv2')
    ssm = session.client('ssm')
    commands = {}
    checks = {}
    ssm_allowed = True
    def ready():
        nonlocal ssm_allowed
        if availability_probe:
            availability_probe()
        groups = scaling.describe_auto_scaling_groups(AutoScalingGroupNames=['workshop-asg'])['AutoScalingGroups']
        if not groups:
            raise RuntimeError('workshop-asg was not created.')
        group = groups[0]
        for instance in group['Instances']:
            identifier = instance['InstanceId']
            if instance['LifecycleState'] != 'Pending:Wait' or not ssm_allowed:
                continue
            if identifier in commands:
                try:
                    result = ssm.get_command_invocation(CommandId=commands[identifier], InstanceId=identifier)
                except ssm.exceptions.InvocationDoesNotExist:
                    continue
                if result['Status'] in ('Pending','InProgress','Delayed'):
                    continue
                del commands[identifier]
                if result['Status'] == 'Success' and 'WORKSHOP_READY' in result['StandardOutputContent']:
                    try:
                        scaling.complete_lifecycle_action(AutoScalingGroupName='workshop-asg', LifecycleHookName='workshop-bootstrap',
                            LifecycleActionResult='CONTINUE', InstanceId=identifier)
                    except scaling.exceptions.ClientError as error:
                        if error.response['Error']['Code'] not in ('ValidationError','ResourceContention'):
                            raise
                    continue
            if time.monotonic() < checks.get(identifier, 0):
                continue
            checks[identifier] = time.monotonic() + 30
            try:
                online = ssm.describe_instance_information(Filters=[{'Key':'InstanceIds','Values':[identifier]}])['InstanceInformationList']
                if online and online[0]['PingStatus'] == 'Online':
                    command = ssm.send_command(InstanceIds=[identifier], DocumentName='AWS-RunShellScript',
                        Parameters={'commands':['test ! -f /var/log/user-data-done.log || echo WORKSHOP_READY']})
                    commands[identifier] = command['Command']['CommandId']
            except ssm.exceptions.ClientError as error:
                if error.response['Error']['Code'] in ('AccessDeniedException','AccessDenied'):
                    ssm_allowed = False
                elif error.response['Error']['Code'] != 'InvalidInstanceId':
                    raise
        active = {x['InstanceId'] for x in group['Instances'] if x['LifecycleState'] == 'InService'
                  and x['HealthStatus'] == 'Healthy' and x.get('LaunchTemplate',{}).get('Version') == group['LaunchTemplate']['Version']}
        if len(active) < 2:
            return False
        if previous_instances is not None and not active.difference(previous_instances):
            return False
        for arn in outputs['target_groups'].values():
            healthy = {x['Target']['Id'] for x in elb.describe_target_health(TargetGroupArn=arn)['TargetHealthDescriptions']
                       if x['TargetHealth']['State'] == 'healthy'}
            if not active.issubset(healthy):
                return False
        return sorted(active)
    return wait_for('two healthy EC2 instances in both target groups', ready, timeout)


def verify(session, config, outputs):
    identifiers = wait_ready(session, outputs)
    instances = [x for r in session.client('ec2').describe_instances(InstanceIds=identifiers)['Reservations'] for x in r['Instances']]
    if len({x['Placement']['AvailabilityZone'] for x in instances}) != 2 or any(x.get('PublicIpAddress') for x in instances):
        raise RuntimeError('EC2 instances must span two AZs and have no public IPv4 addresses.')
    subnets = session.client('ec2').describe_subnets(SubnetIds=list(outputs['subnet_ids'].values()))['Subnets']
    if len(subnets) != 6:
        raise RuntimeError('The workshop requires six subnets.')
    database = session.client('rds').describe_db_instances(DBInstanceIdentifier='workshop-db')['DBInstances'][0]
    if not database['MultiAZ'] or database['PubliclyAccessible'] or database['DBName'] != 'itkmitl' or not database['EngineVersion'].startswith('16.'):
        raise RuntimeError('PostgreSQL must be private, Multi-AZ and version 16.')
    scaling = session.client('autoscaling')
    group = scaling.describe_auto_scaling_groups(AutoScalingGroupNames=['workshop-asg'])['AutoScalingGroups'][0]
    if group['MinSize'] != 2 or group['MaxSize'] != 4 or group['HealthCheckType'] != 'ELB':
        raise RuntimeError('The Auto Scaling group settings differ from the workshop.')
    policies = scaling.describe_policies(AutoScalingGroupName='workshop-asg')['ScalingPolicies']
    if not any(x.get('TargetTrackingConfiguration',{}).get('TargetValue') == 70 for x in policies):
        raise RuntimeError('The CPU target tracking policy must use 70 percent.')
    attributes = {x['Key']:x['Value'] for x in session.client('elbv2').describe_target_group_attributes(TargetGroupArn=outputs['target_groups']['frontend'])['Attributes']}
    if attributes.get('stickiness.enabled') != 'true' or attributes.get('stickiness.lb_cookie.duration_seconds') != '86400':
        raise RuntimeError('Frontend stickiness must be enabled for one day.')
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    url = outputs['website_url']
    page = http(opener, url, '/')
    if b'<html' not in page.lower():
        raise RuntimeError('The frontend did not return an HTML page.')
    activities = http(opener, url, '/api/activities')['activities']
    if len(activities) != 6 or len({x['id'] for x in activities}) != 6:
        raise RuntimeError('The application must contain the six seeded activities without duplicates.')
    path = TOOLS / 'test-user.json'
    if path.exists():
        user = json.loads(path.read_text())
    else:
        user = {'student_id':str(secrets.randbelow(1000000) + 99000000), 'email':'terraform-' + uuid.uuid4().hex[:12] + '@example.invalid',
                'password':secrets.token_urlsafe(24), 'first_name':'Terraform', 'last_name':'Workshop Test'}
        save_json(path, user)
    try:
        login = http(opener, url, '/api/auth/login', 'POST', {'email':user['email'],'password':user['password']})
    except urllib.error.HTTPError as error:
        if error.code != 401:
            raise
        http(opener, url, '/api/auth/register', 'POST', user)
        login = http(opener, url, '/api/auth/login', 'POST', {'email':user['email'],'password':user['password']})
    token = login['token']
    for _ in range(6):
        direct = urllib.request.build_opener()
        account = http(direct, url, '/api/auth/me', token=token)
        if account['user']['email'] != user['email']:
            raise RuntimeError('Authentication did not survive backend load balancing.')
    http(opener, url, '/api/auth/logout', 'POST', {})
    with urllib.request.urlopen(outputs['assets_url'] + '/logo.png', timeout=30) as response:
        remote_hash = hashlib.sha256(response.read()).hexdigest()
    if remote_hash != hashlib.sha256((TOOLS / 'source/frontend/static/logo.png').read_bytes()).hexdigest():
        raise RuntimeError('The public S3 assets do not match the application source.')
    print('Verified: 6 subnets, 2 private EC2 instances across 2 AZs, private Multi-AZ PostgreSQL 16, CPU target 70%, sticky frontend, S3 assets, registration, login and 6 activities.', flush=True)
    return identifiers


def test_ha(session, config, outputs, identifiers, instance_id=None):
    original = set(identifiers)
    victim = instance_id or sorted(original)[0]
    if victim not in original:
        raise RuntimeError('The selected HA test instance is not a healthy member of this workshop ASG.')
    print('Testing recovery by replacing workshop instance ' + victim + '.', flush=True)
    session.client('autoscaling').terminate_instance_in_auto_scaling_group(InstanceId=victim, ShouldDecrementDesiredCapacity=False)
    opener = urllib.request.build_opener()
    def probe():
        http(opener, outputs['website_url'], '/')
        http(opener, outputs['website_url'], '/api/activities')
    current = wait_ready(session, outputs, availability_probe=probe, previous_instances=original)
    if victim in current:
        raise RuntimeError('The terminated instance is still registered as healthy.')
    verify(session, config, outputs)
    print('Verified: website probes succeeded during replacement and Auto Scaling restored two healthy instances.', flush=True)


def main():
    parser = argparse.ArgumentParser()
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--plan-only', action='store_true')
    actions.add_argument('--verify-only', action='store_true')
    actions.add_argument('--destroy', action='store_true')
    parser.add_argument('--test-ha', action='store_true')
    parser.add_argument('--ha-instance-id')
    args = parser.parse_args()
    if args.test_ha and (args.destroy or args.plan_only):
        parser.error('--test-ha requires a deployed workshop.')
    if args.ha_instance_id and not args.test_ha:
        parser.error('--ha-instance-id requires --test-ha.')
    TOOLS.mkdir(exist_ok=True)
    environment = load_environment()
    session = aws_session(environment)
    identity = identity_for(session, args.destroy)
    print('Workshop account: ' + identity['account_id'] + '; region: ' + identity['region'], flush=True)
    if args.destroy:
        config = destroy_configuration(environment)
    else:
        secrets_for(environment)
        source_for(environment)
        config = configuration(session, environment, identity)
    environment['TF_IN_AUTOMATION'] = '1'
    environment['AWS_PAGER'] = ''
    terraform = find_terraform()
    def execute(*arguments, capture=False):
        result = subprocess.run([terraform,*arguments], cwd=ROOT, env=environment, check=True, text=True,
                                stdout=subprocess.PIPE if capture else None)
        return result.stdout if capture else None
    execute('init','-input=false','-no-color')
    execute('fmt','-check','-no-color')
    execute('validate','-no-color')
    if args.destroy:
        execute('destroy','-input=false','-auto-approve','-no-color')
        print('Workshop resources destroyed. The provided LabRole and default VPC were preserved.')
        return
    if not args.verify_only:
        conflicts(session)
        execute('plan','-input=false','-no-color','-out=.tools/workshop.tfplan')
        plan = json.loads(execute('show','-json','.tools/workshop.tfplan',capture=True))
        deletes = [x['address'] for x in plan.get('resource_changes',[]) if 'delete' in x['change']['actions']]
        if deletes:
            raise RuntimeError('Refusing resource deletion or replacement: ' + ', '.join(deletes))
        if args.plan_only:
            print('Plan validated. Run without --plan-only to deploy the workshop.')
            return
        execute('apply','-input=false','-no-color','.tools/workshop.tfplan')
    outputs = {key:value['value'] for key,value in json.loads(execute('output','-json',capture=True)).items()}
    identifiers = verify(session, config, outputs)
    if args.test_ha:
        test_ha(session, config, outputs, identifiers, args.ha_instance_id)
    print('Workshop ready: ' + outputs['website_url'])
    print('Private key: ' + str(TOOLS / 'workshop-key.pem'))
    print('Cleanup: python3 ' + str(ROOT / 'run.py') + ' --destroy')


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print('Workshop failed: ' + str(error), file=sys.stderr)
        sys.exit(1)
