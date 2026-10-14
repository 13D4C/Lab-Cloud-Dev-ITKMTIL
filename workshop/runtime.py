import hashlib
import importlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parent
TOOLS = ROOT / ".tools"
TERRAFORM_VERSION = "1.16.5"


def load_environment():
    environment = os.environ.copy()
    allowed = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_REGION", "AWS_DEFAULT_REGION", "DB_PASSWORD", "JWT_SECRET", "ASSETS_BUCKET_NAME", "INSTANCE_PROFILE", "INSTANCE_TYPE", "DB_INSTANCE_CLASS", "SOURCE_REPOSITORY", "SOURCE_REVISION", "AMI_ID", "DB_ENGINE_VERSION", "AVAILABILITY_ZONES"}
    source = ROOT / ".env"
    if source.exists():
        for line in source.read_text().splitlines():
            key, separator, value = line.partition("=")
            key = key.strip().upper()
            if separator and key in allowed:
                environment[key] = value.strip().strip("\"'")
    environment["TF_VAR_aws_region"] = environment.get("AWS_REGION", environment.get("AWS_DEFAULT_REGION", "us-east-1"))
    return environment


def find_terraform():
    binary = "terraform.exe" if platform.system() == "Windows" else "terraform"
    executable = TOOLS / binary
    if executable.exists():
        return str(executable)
    operating_system = {"Linux": "linux", "Darwin": "darwin", "Windows": "windows"}.get(platform.system())
    architecture = {"x86_64": "amd64", "AMD64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine())
    if not operating_system or not architecture:
        raise RuntimeError("Unsupported platform. Install Terraform 1.16.5 and update find_terraform().")
    filename = f"terraform_{TERRAFORM_VERSION}_{operating_system}_{architecture}.zip"
    base = f"https://releases.hashicorp.com/terraform/{TERRAFORM_VERSION}"
    TOOLS.mkdir(exist_ok=True)
    print(f"Downloading Terraform {TERRAFORM_VERSION}...", flush=True)
    with tempfile.TemporaryDirectory(dir=TOOLS) as directory:
        target = Path(directory) / filename
        with urllib.request.urlopen(f"{base}/{filename}", timeout=120) as response, target.open("wb") as output:
            shutil.copyfileobj(response, output)
        with urllib.request.urlopen(f"{base}/terraform_{TERRAFORM_VERSION}_SHA256SUMS", timeout=60) as response:
            checksums = response.read().decode()
        expected = next(line.split()[0] for line in checksums.splitlines() if line.split()[-1] == filename)
        if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise RuntimeError("Terraform download checksum does not match.")
        with zipfile.ZipFile(target) as archive:
            archive.extract(binary, TOOLS)
    executable.chmod(0o755)
    return str(executable)


def dependencies():
    target = TOOLS / "python"
    sys.path.insert(0, str(target))
    try:
        import boto3
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--target", str(target), "-r", str(ROOT / "requirements.txt")], check=True)
        importlib.invalidate_caches()


def aws_session(environment):
    dependencies()
    import boto3
    return boto3.Session(
        aws_access_key_id=environment.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=environment.get("AWS_SECRET_ACCESS_KEY"),
        aws_session_token=environment.get("AWS_SESSION_TOKEN"),
        region_name=environment["TF_VAR_aws_region"],
    )
