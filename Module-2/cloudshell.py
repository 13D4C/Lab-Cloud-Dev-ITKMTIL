import base64
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import select
import shlex
import shutil
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
import uuid

from runtime import ROOT, TOOLS


def call(session, operation, **parameters):
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    request = AWSRequest(method='POST', url=f'https://cloudshell.{session.region_name}.amazonaws.com/{operation}',
                         data=json.dumps(parameters).encode(), headers={'Content-Type': 'application/json'})
    SigV4Auth(session.get_credentials().get_frozen_credentials(), 'cloudshell', session.region_name).add_auth(request)
    try:
        with urllib.request.urlopen(urllib.request.Request(request.url, data=request.body, headers=dict(request.headers), method='POST'), timeout=60) as response:
            data = response.read()
            return json.loads(data) if data else {}
    except urllib.error.HTTPError as error:
        message = error.read().decode()
        raise RuntimeError(f'CloudShell {operation} failed ({error.code}): {message}') from None


def plugin():
    binary = shutil.which('session-manager-plugin')
    if binary:
        return binary
    target = TOOLS / 'session-manager-plugin'
    if target.exists():
        return str(target)
    if platform.system() != 'Linux':
        raise RuntimeError('Install the AWS Session Manager plugin before running on this operating system.')
    architecture = {'x86_64': '64bit', 'aarch64': 'arm64'}.get(platform.machine())
    if not architecture:
        raise RuntimeError('Unsupported Session Manager plugin architecture.')
    url = f'https://session-manager-downloads.s3.amazonaws.com/plugin/latest/ubuntu_{architecture}/session-manager-plugin.deb'
    print('Downloading the AWS Session Manager plugin...', flush=True)
    with urllib.request.urlopen(url, timeout=120) as response:
        package = response.read()
    if not package.startswith(b'!<arch>\n'):
        raise RuntimeError('Invalid Session Manager plugin package.')
    offset = 8
    while offset + 60 <= len(package):
        header = package[offset:offset + 60]
        size = int(header[48:58])
        name = header[:16].decode().strip().rstrip('/')
        data = package[offset + 60:offset + 60 + size]
        if name.startswith('data.tar'):
            with tarfile.open(fileobj=io.BytesIO(data), mode='r:*') as archive:
                entry = next(x for x in archive.getmembers() if x.isfile() and x.name.endswith('/bin/session-manager-plugin'))
                target.write_bytes(archive.extractfile(entry).read())
            target.chmod(0o755)
            return str(target)
        offset += 60 + size + size % 2
    raise RuntimeError('Session Manager plugin binary was not found in the package.')


def run_terminal(connection, region, script, timeout=600):
    import pty
    import termios
    payload = base64.b64encode(script.encode())
    ready = 'LAB2_CLOUDSHELL_READY_' + uuid.uuid4().hex
    done = 'LAB2_CLOUDSHELL_DONE_' + uuid.uuid4().hex
    loader = f"import base64,subprocess,sys; print({ready!r},flush=True); data=sys.stdin.buffer.read({len(payload)}); result=subprocess.run(['/bin/bash'],input=base64.b64decode(data)); print({done!r}+':'+str(result.returncode),flush=True)"
    launcher = 'stty -echo -icanon; python3 -u -c ' + shlex.quote(loader) + '; stty sane\n'
    master, slave = pty.openpty()
    attributes = termios.tcgetattr(slave)
    attributes[3] &= ~termios.ECHO
    termios.tcsetattr(slave, termios.TCSANOW, attributes)
    process = subprocess.Popen([plugin(), json.dumps(connection), region, 'StartSession', '',
                                json.dumps({'Target': 'cloudshell'}), f'https://ssm.{region}.amazonaws.com'],
                               stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    output = ''
    sent_launcher = False
    sent_payload = False
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master], [], [], 2)
            if not readable:
                if process.poll() is not None:
                    raise RuntimeError('CloudShell terminal closed before the lab commands completed.')
                continue
            try:
                data = os.read(master, 65536).decode(errors='replace')
            except OSError:
                raise RuntimeError('CloudShell terminal connection ended unexpectedly.') from None
            output += data
            if not sent_launcher:
                print(data, end='', flush=True)
                if '$' in output or '# ' in output:
                    os.write(master, launcher.encode())
                    sent_launcher = True
                    output = ''
            elif not sent_payload:
                if ready + '\r' in output or ready + '\n' in output:
                    for offset in range(0, len(payload), 4096):
                        os.write(master, payload[offset:offset + 4096])
                    sent_payload = True
                    output = output.split(ready, 1)[1].lstrip('\r\n')
            else:
                print(data, end='', flush=True)
                if done + ':' in output:
                    code = output.split(done + ':', 1)[1].splitlines()[0].strip()
                    if code and code != '0':
                        raise RuntimeError('CloudShell lab commands failed with exit code ' + code)
                    if code == '0':
                        return output
        raise TimeoutError('CloudShell lab commands timed out. Run the same command again to resume.')
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        os.close(master)


def prepare(session, config, wait_for):
    environments = call(session, 'describeEnvironments')['Environments']
    public = [x for x in environments if not x.get('VpcConfig') and not x.get('VpcId')]
    environment = public[0] if public else call(session, 'createEnvironment')
    environment_id = environment['EnvironmentId']
    print('CloudShell environment: ' + environment_id, flush=True)
    def ready():
        status = call(session, 'getEnvironmentStatus', EnvironmentId=environment_id)['Status']
        if status in ('SUSPENDED', 'STOPPED'):
            call(session, 'startEnvironment', EnvironmentId=environment_id)
        if status in ('FAILED', 'DELETING', 'DELETED'):
            raise RuntimeError('CloudShell environment is ' + status)
        return status == 'RUNNING'
    wait_for('CloudShell to start', ready, timeout=600)
    connection = call(session, 'createSession', EnvironmentId=environment_id, SessionType='TMUX',
                      TabId=str(uuid.uuid4()), QCliDisabled=True)
    credentials = session.get_credentials().get_frozen_credentials()
    environment = dict(AWS_ACCESS_KEY_ID=credentials.access_key, AWS_SECRET_ACCESS_KEY=credentials.secret_key,
                       AWS_SESSION_TOKEN=credentials.token, AWS_REGION=session.region_name, AWS_DEFAULT_REGION=session.region_name,
                       AWS_PAGER='')
    exports = '\n'.join('export ' + key + '=' + shlex.quote(value or '') for key, value in environment.items())
    source = base64.b64encode((ROOT / 'list-buckets.py').read_bytes()).decode()
    script = "set -euo pipefail\n" + exports + "\ncd ~\naws --version\naws s3 ls\nbase64 -d <<'LAB2_PYTHON' > list-buckets.py\n" + source + "\nLAB2_PYTHON\npython3 list-buckets.py\naws s3 cp list-buckets.py " + shlex.quote('s3://' + config['sample_bucket'] + '/list-buckets.py') + " --content-type text/x-python --cache-control max-age=0\ndf -H /home\nprintf 'LAB2_CLOUDSHELL_VERIFIED\\n'\n"
    try:
        result = run_terminal(connection, session.region_name, script)
        if 'LAB2_CLOUDSHELL_VERIFIED' not in result:
            raise RuntimeError('CloudShell verification marker was not returned.')
    finally:
        call(session, 'deleteSession', EnvironmentId=environment_id, SessionId=connection['SessionId'])
    return environment_id
