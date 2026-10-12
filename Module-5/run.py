import json
import hashlib
import importlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parent
TOOLS = ROOT / ".tools"


def load_environment():
    environment = os.environ.copy()
    source = ROOT / ".env"
    allowed = {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
    }
    if source.exists():
        for line in source.read_text().splitlines():
            key, separator, value = line.partition("=")
            key = key.strip().upper()
            if separator and key in allowed:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                environment[key] = value
    environment.setdefault(
        "TF_VAR_aws_region",
        environment.get("AWS_REGION", environment.get("AWS_DEFAULT_REGION", "us-east-1")),
    )
    return environment


def find_terraform():
    existing = shutil.which("terraform")
    if existing:
        return existing
    binary = "terraform.exe" if platform.system() == "Windows" else "terraform"
    executable = TOOLS / binary
    if executable.exists():
        return str(executable)
    operating_system = {"Linux": "linux", "Darwin": "darwin", "Windows": "windows"}.get(platform.system())
    architecture = {"x86_64": "amd64", "AMD64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine())
    if not operating_system or not architecture:
        raise RuntimeError("Install Terraform >= 1.7 and add it to PATH.")
    version = "1.13.5"
    filename = f"terraform_{version}_{operating_system}_{architecture}.zip"
    base_url = f"https://releases.hashicorp.com/terraform/{version}"
    TOOLS.mkdir(exist_ok=True)
    print("Downloading Terraform...", flush=True)
    with tempfile.TemporaryDirectory(dir=TOOLS) as directory:
        archive_path = Path(directory) / filename
        with urllib.request.urlopen(f"{base_url}/{filename}", timeout=30) as response:
            with archive_path.open("wb") as output:
                shutil.copyfileobj(response, output)
        with urllib.request.urlopen(f"{base_url}/terraform_{version}_SHA256SUMS", timeout=30) as response:
            checksums = response.read().decode()
        expected = next(line.split()[0] for line in checksums.splitlines() if line.split()[-1] == filename)
        if hashlib.sha256(archive_path.read_bytes()).hexdigest() != expected:
            raise RuntimeError("Terraform download checksum does not match.")
        with zipfile.ZipFile(archive_path) as archive:
            archive.extract(binary, TOOLS)
    executable.chmod(0o755)
    return str(executable)


def aws_session(environment):
    dependencies = TOOLS / "python"
    sys.path.insert(0, str(dependencies))
    try:
        import boto3
    except ImportError:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--target", str(dependencies), "-r", str(ROOT / "requirements.txt")],
            check=True,
        )
        importlib.invalidate_caches()
        import boto3
    return boto3.Session(
        aws_access_key_id=environment.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=environment.get("AWS_SECRET_ACCESS_KEY"),
        aws_session_token=environment.get("AWS_SESSION_TOKEN"),
        region_name=environment["TF_VAR_aws_region"],
    )


def scan_table(table, **request):
    result = []
    while True:
        page = table.scan(**request)
        result.extend(page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            return result
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def read_items(command, arguments, environment):
    session = aws_session(environment)
    from boto3.dynamodb.conditions import Attr

    table = session.resource("dynamodb").Table("FoodProducts")
    if command == "get":
        product = arguments[0] if arguments else "chocolate cake"
        result = table.get_item(Key={"product_name": product}, ConsistentRead=True).get("Item")
    else:
        request = {"ConsistentRead": True}
        if command == "specials":
            request = {
                "IndexName": "special_GSI",
                "FilterExpression": ~Attr("tags").contains("out of stock"),
            }
        result = scan_table(table, **request)
        result.sort(key=lambda item: item["product_name"])
    print(json.dumps(result, indent=2, ensure_ascii=False, default=int))


def deploy(environment):
    session = aws_session(environment)
    client = session.client("dynamodb")
    try:
        existing = client.describe_table(TableName="FoodProducts")["Table"]
    except client.exceptions.ResourceNotFoundException:
        existing = None
    if existing:
        definitions = {attribute["AttributeName"]: attribute["AttributeType"] for attribute in existing["AttributeDefinitions"]}
        if existing["KeySchema"] != [{"AttributeName": "product_name", "KeyType": "HASH"}] or definitions.get("product_name") != "S":
            raise RuntimeError("Existing FoodProducts has a different primary key. No changes were made.")

    terraform = find_terraform()
    prefix = [terraform, f"-chdir={ROOT}"]

    def execute(*arguments, capture=False):
        return subprocess.run(
            [*prefix, *arguments], env=environment, check=True,
            capture_output=capture, text=True,
        )

    execute("init", "-input=false", "-no-color")
    execute("validate", "-no-color")
    if existing:
        try:
            state = execute("state", "list", capture=True).stdout.splitlines()
        except subprocess.CalledProcessError as error:
            if "No state file was found" not in (error.stderr or ""):
                raise
            state = []
        if "aws_dynamodb_table.food_products" not in state:
            execute("import", "-input=false", "-no-color", "aws_dynamodb_table.food_products", "FoodProducts")
    TOOLS.mkdir(exist_ok=True)
    plan = str(TOOLS / "lab.tfplan")
    execute("plan", "-input=false", "-no-color", "-parallelism=1", f"-out={plan}")
    execute("apply", "-input=false", "-no-color", "-parallelism=1", plan)
    verify(session)


def verify(session):
    from boto3.dynamodb.conditions import Attr

    table = session.resource("dynamodb").Table("FoodProducts")
    actual = {item["product_name"]: item for item in scan_table(table, ConsistentRead=True)}
    products = json.loads((ROOT / "data/all_products.json").read_text())["product_item_arr"]
    specials = set()
    for product in products:
        expected = {
            "product_name": product["product_name_str"],
            "product_id_str": product["product_id_str"],
            "price_in_cents": product["price_in_cents_int"],
            "description": product["description_str"],
            "tags": product["tag_str_arr"],
        }
        if "special_int" in product:
            expected["special"] = product["special_int"]
            if "out of stock" not in product["tag_str_arr"]:
                specials.add(product["product_name_str"])
        if actual.get(expected["product_name"]) != expected:
            raise RuntimeError(f"Product verification failed: {expected['product_name']}")
    if table.get_item(Key={"product_name": "chocolate cake"}, ConsistentRead=True).get("Item") != actual["chocolate cake"]:
        raise RuntimeError("Single-product lookup verification failed.")
    for attempt in range(12):
        indexed = scan_table(table, IndexName="special_GSI", FilterExpression=~Attr("tags").contains("out of stock"))
        if {item["product_name"] for item in indexed} == specials:
            print(f"Lab 5.1 complete: FoodProducts, {len(products)} products, special_GSI, {len(specials)} available specials.", flush=True)
            return
        if attempt < 11:
            time.sleep(5)
    raise RuntimeError("Products were created but special_GSI has not caught up. Run the command again.")


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "deploy"
    arguments = sys.argv[2:]
    environment = load_environment()
    if command == "deploy":
        deploy(environment)
        return 0
    if command in {"scan", "get", "specials"}:
        read_items(command, arguments, environment)
        return 0
    terraform = find_terraform()
    return subprocess.run(
        [terraform, f"-chdir={ROOT}", command, *arguments], env=environment
    ).returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as error:
        if error.stderr:
            print(error.stderr, file=sys.stderr)
        sys.exit(error.returncode)
    except Exception as error:
        sys.exit(str(error))
