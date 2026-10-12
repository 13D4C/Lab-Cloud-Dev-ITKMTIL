import argparse
import base64
import io
import ipaddress
import json
import subprocess
import sys
import tarfile
import urllib.request

from runtime import ROOT, aws_session, find_tofu, load_environment


def discover(session, environment):
    ec2 = session.client('ec2')
    instances = []
    for page in ec2.get_paginator('describe_instances').paginate(Filters=[{'Name': 'instance-state-name', 'Values': ['running']} ]):
        instances.extend(instance for reservation in page['Reservations'] for instance in reservation['Instances'])
    by_name = {}
    for instance in instances:
        tags = {item['Key']: item['Value'] for item in instance.get('Tags', [])}
        name = tags.get('Name', '')
        if name in ('Lab IDE', 'MysqlServerNode', 'AppServerNode'):
            if environment.get('LAB_STACK_NAME') and tags.get('cloudlab') != environment['LAB_STACK_NAME'] and tags.get('aws:cloudformation:stack-name') != environment['LAB_STACK_NAME']:
                continue
            if name in by_name:
                raise RuntimeError(f'More than one {name} instance found. Set LAB_STACK_NAME in .env.')
            by_name[name] = instance
    if set(by_name) != {'Lab IDE', 'MysqlServerNode', 'AppServerNode'}:
        raise RuntimeError('Start Lab 8.1 and wait for all three EC2 instances to be running.')
    ide = by_name['Lab IDE']
    tags = {item['Key']: item['Value'] for item in ide['Tags']}
    stack_name = environment.get('LAB_STACK_NAME') or tags.get('cloudlab')
    if not stack_name:
        raise RuntimeError('Set LAB_STACK_NAME to the lab CloudFormation stack name in .env.')
    stack = session.client('cloudformation').describe_stacks(StackName=stack_name)['Stacks'][0]
    outputs = {item['OutputKey']: item['OutputValue'] for item in stack.get('Outputs', [])}
    if not outputs.get('LabIDEURL') or not outputs.get('LabIDEPassword'):
        raise RuntimeError('Lab IDE outputs are not ready. Wait for Start Lab to finish.')
    return by_name, outputs


def bundle(config):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        for path in sorted((ROOT / 'containers').rglob('*')):
            if path.is_file() and not set(path.parts).intersection({'node_modules', '.env', '__pycache__'}):
                archive.add(path, arcname=str(path.relative_to(ROOT)))
        archive.add(ROOT / 'deploy.py', arcname='.lab81/deploy.py')
        data = json.dumps(config).encode()
        entry = tarfile.TarInfo('.lab81/config.json')
        entry.mode = 0o600
        entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))
    return base64.b64encode(output.getvalue()).decode()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepare-grading', action='store_true', help='Create the Academy grading key and keep SSH port 22 open as specified in resources/setup.sh.')
    options = parser.parse_args()
    environment = load_environment()
    session = aws_session(environment)
    account = session.client('sts').get_caller_identity()['Account']
    instances, outputs = discover(session, environment)
    ide = instances['Lab IDE']
    print(f"Lab 8.1: account {account}, Lab IDE {ide['InstanceId']}", flush=True)
    if environment.get('CLIENT_CIDR'):
        cidr = environment['CLIENT_CIDR']
    else:
        with urllib.request.urlopen('https://checkip.amazonaws.com', timeout=30) as response:
            cidr = response.read().decode().strip() + '/32'
    network = ipaddress.ip_network(cidr, strict=True)
    if network.version != 4 or network.prefixlen != 32:
        raise RuntimeError('CLIENT_CIDR must be your public IPv4 address followed by /32.')
    environment.update(
        TF_VAR_ide_instance_id=ide['InstanceId'],
        TF_VAR_mysql_instance_id=instances['MysqlServerNode']['InstanceId'],
        TF_VAR_app_instance_id=instances['AppServerNode']['InstanceId'],
        TF_VAR_client_cidr=cidr,
        TF_VAR_prepare_grading=str(options.prepare_grading).lower(),
        TF_IN_AUTOMATION='1', TF_INPUT='0',
    )
    tofu = find_tofu()
    def run(*args, capture=False):
        result = subprocess.run([tofu, *args], cwd=ROOT, env=environment, check=True, text=True, stdout=subprocess.PIPE if capture else None)
        return result.stdout if capture else None
    run('init', '-no-color')
    run('validate', '-no-color')
    resources = set(run('state', 'list', capture=True).splitlines()) if (ROOT / 'terraform.tfstate').exists() else set()
    ecr = session.client('ecr')
    if 'aws_ecr_repository.node_app' not in resources:
        try:
            ecr.describe_repositories(repositoryNames=['node-app'])
        except ecr.exceptions.RepositoryNotFoundException:
            pass
        else:
            run('import', '-no-color', 'aws_ecr_repository.node_app', 'node-app')
    if 'aws_vpc_security_group_ingress_rule.application' not in resources:
        groups = ide['SecurityGroups']
        if len(groups) != 1:
            raise RuntimeError('Lab IDE must have one security group for this lab.')
        rules = session.client('ec2').describe_security_group_rules(Filters=[{'Name': 'group-id', 'Values': [groups[0]['GroupId']]}])['SecurityGroupRules']
        for rule in rules:
            if not rule['IsEgress'] and rule.get('IpProtocol') == 'tcp' and rule.get('FromPort') == 3000 and rule.get('ToPort') == 3000 and rule.get('CidrIpv4') == cidr:
                run('import', '-no-color', 'aws_vpc_security_group_ingress_rule.application', rule['SecurityGroupRuleId'])
                break
    if options.prepare_grading and 'aws_vpc_security_group_ingress_rule.grading[0]' not in resources:
        rules = session.client('ec2').describe_security_group_rules(Filters=[{'Name': 'group-id', 'Values': [ide['SecurityGroups'][0]['GroupId']]}])['SecurityGroupRules']
        for rule in rules:
            if not rule['IsEgress'] and rule.get('IpProtocol') == 'tcp' and rule.get('FromPort') == 22 and rule.get('ToPort') == 22 and rule.get('CidrIpv4') == '0.0.0.0/0':
                run('import', '-no-color', 'aws_vpc_security_group_ingress_rule.grading[0]', rule['SecurityGroupRuleId'])
                break
    run('apply', '-auto-approve', '-no-color')
    values = json.loads(run('output', '-json', capture=True))
    config = {
        'mysql_host': instances['MysqlServerNode']['PrivateIpAddress'],
        'app_host': instances['AppServerNode']['PrivateIpAddress'],
        'repository_url': values['repository_url']['value'],
        'region': environment['TF_VAR_aws_region'],
        'prepare_grading': options.prepare_grading,
    }
    payload = bundle(config)
    from lab_ide import LabIDE
    print('Connecting to the authenticated Lab IDE terminal...', flush=True)
    remote = LabIDE(outputs['LabIDEURL'], outputs['LabIDEPassword'])
    try:
        script = "set -euo pipefail\ncd /home/ec2-user/environment\nmkdir -p .lab81\nbase64 -d <<'LAB81_BUNDLE' | tar xz\n" + payload + "\nLAB81_BUNDLE\npython3 .lab81/deploy.py"
        result = remote.run(script, timeout=2400)
        if 'LAB81_COMPLETE ' not in result:
            raise RuntimeError('Remote deployment did not report completion.')
    finally:
        remote.close()
    image = ecr.describe_images(repositoryName='node-app', imageIds=[{'imageTag': 'latest'}])['imageDetails'][0]
    website = values['website_url']['value']
    with urllib.request.urlopen(website + '/suppliers', timeout=30) as response:
        page = response.read().decode()
    if 'Nikki Wolf' not in page or '100 Container Street' not in page:
        raise RuntimeError('Public website did not return the migrated supplier.')
    print(f"\nLab 8.1 completed. Website: {website}")
    print(f"ECR: {config['repository_url']}:latest")
    print(f"Image digest: {image['imageDigest']}")
    if options.prepare_grading:
        print('Leave both Docker containers running, then submit the lab in AWS Academy. Grading SSH access is enabled until you close it or end the lab.')
    else:
        print('Containers and ECR are ready. Academy grading SSH setup requires --prepare-grading.')


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print(f'Lab 8.1 failed: {error}', file=sys.stderr)
        sys.exit(1)
