import ipaddress
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

from runtime import ROOT, TOOLS, aws_session, find_terraform, load_environment


def configure(environment, session):
    s3 = session.client("s3")
    bucket = environment.get("WEBSITE_BUCKET") or environment.get("TF_VAR_website_bucket")
    if not bucket:
        candidates = [item["Name"] for item in s3.list_buckets()["Buckets"] if "s3bucket" in item["Name"]]
        if len(candidates) != 1:
            raise RuntimeError("Set WEBSITE_BUCKET in .env to the website bucket provided by the lab.")
        bucket = candidates[0]
    location = s3.get_bucket_location(Bucket=bucket).get("LocationConstraint")
    region = {None: "us-east-1", "EU": "eu-west-1"}.get(location, location)
    cidr = environment.get("WEBSITE_SOURCE_CIDR") or environment.get("TF_VAR_website_source_cidr")
    if not cidr:
        with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=20) as response:
            cidr = f"{ipaddress.IPv4Address(response.read().decode().strip())}/32"
    cidr = str(ipaddress.IPv4Network(cidr, strict=False))
    environment.update({
        "TF_VAR_website_bucket": bucket,
        "TF_VAR_website_bucket_region": region,
        "TF_VAR_website_source_cidr": cidr,
    })
    return bucket, session.client("s3", region_name=region)


def terraform_command(environment, *arguments, capture=False):
    return subprocess.run(
        [find_terraform(), f"-chdir={ROOT}", *arguments],
        env=environment, check=True, capture_output=capture, text=True,
    )


def state_addresses(environment):
    try:
        return set(terraform_command(environment, "state", "list", capture=True).stdout.splitlines())
    except subprocess.CalledProcessError as error:
        if "No state file was found" not in (error.stderr or ""):
            raise
        return set()


def import_existing(environment, session, s3, bucket):
    state = state_addresses(environment)
    gateway = session.client("apigateway")
    selected = environment.get("PRODUCTS_API_ID")
    apis = [item for page in gateway.get_paginator("get_rest_apis").paginate() for item in page.get("items", []) if item["name"] == "ProductsApi"]
    if selected:
        apis = [item for item in apis if item["id"] == selected]
        if not apis:
            raise RuntimeError("PRODUCTS_API_ID must identify an existing ProductsApi.")
    if len(apis) > 1:
        raise RuntimeError("More than one ProductsApi exists. Set PRODUCTS_API_ID in .env.")
    if apis:
        api = apis[0]
        if api["endpointConfiguration"]["types"] != ["REGIONAL"]:
            raise RuntimeError("Existing ProductsApi must use a REGIONAL endpoint.")
        if "aws_api_gateway_rest_api.products" not in state:
            terraform_command(environment, "import", "-input=false", "-no-color", "aws_api_gateway_rest_api.products", api["id"])
        stages = gateway.get_stages(restApiId=api["id"])["item"]
        if any(stage["stageName"] == "prod" for stage in stages) and "aws_api_gateway_stage.prod" not in state:
            terraform_command(environment, "import", "-input=false", "-no-color", "aws_api_gateway_stage.prod", f"{api['id']}/prod")
    if "aws_s3_bucket_policy.website" not in state:
        try:
            s3.get_bucket_policy(Bucket=bucket)
        except s3.exceptions.ClientError as error:
            if error.response["Error"]["Code"] != "NoSuchBucketPolicy":
                raise
        else:
            terraform_command(environment, "import", "-input=false", "-no-color", "aws_s3_bucket_policy.website", bucket)


def http_request(url, method="GET", data=None, headers=None):
    request = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.status, dict(response.headers.items()), response.read().decode()


def verify(outputs):
    base = outputs["api_base_url"]["value"]
    website = outputs["website_url"]["value"]
    origin = website.rsplit("/", 1)[0]
    endpoints = [
        ("/products", "GET", "products.json"),
        ("/products/on_offer", "GET", "on_offer.json"),
        ("/create_report", "POST", "create_report.json"),
    ]
    for path, method, filename in endpoints:
        expected = json.loads((ROOT / "data" / filename).read_text())
        for attempt in range(12):
            try:
                status, headers, body = http_request(
                    base + path, method, b"{}" if method == "POST" else None,
                    {"Content-Type": "application/json", "Origin": origin},
                )
                if status != 200 or json.loads(body) != expected:
                    raise RuntimeError(f"Unexpected mock response: {path}")
                if method == "GET" and {key.lower(): value for key, value in headers.items()}.get("access-control-allow-origin") != "*":
                    raise RuntimeError(f"Missing CORS header: {path}")
                break
            except (urllib.error.URLError, RuntimeError, json.JSONDecodeError):
                if attempt == 11:
                    raise
                time.sleep(5)
        if method == "GET":
            status, headers, _ = http_request(base + path, "OPTIONS", headers={"Origin": origin, "Access-Control-Request-Method": "GET"})
            headers = {key.lower(): value for key, value in headers.items()}
            if status != 200 or headers.get("access-control-allow-origin") != "*" or "GET" not in headers.get("access-control-allow-methods", ""):
                raise RuntimeError(f"CORS preflight failed: {path}")
    status, headers, body = http_request(website)
    if status != 200 or "text/html" not in headers.get("Content-Type", "") or "config.js" not in body:
        raise RuntimeError("The cafe website could not be loaded.")
    _, _, config = http_request(origin + "/config.js")
    settings = json.loads(config.split("=", 1)[1].strip().removesuffix(";"))
    if settings != {"API_GW_BASE_URL_STR": base, "COGNITO_LOGIN_BASE_URL_STR": None}:
        raise RuntimeError("Website config.js does not match the deployed API.")
    print("Lab 6.1 complete: three mock endpoints, CORS, prod stage and connected cafe website.", flush=True)
    print(f"Website: {website}", flush=True)
    print(f"API: {base}", flush=True)


def deploy(environment):
    session = aws_session(environment)
    bucket, s3 = configure(environment, session)
    terraform_command(environment, "init", "-input=false", "-no-color")
    terraform_command(environment, "validate", "-no-color")
    import_existing(environment, session, s3, bucket)
    TOOLS.mkdir(exist_ok=True)
    plan = str(TOOLS / "lab.tfplan")
    terraform_command(environment, "plan", "-input=false", "-no-color", f"-out={plan}")
    terraform_command(environment, "apply", "-input=false", "-no-color", plan)
    outputs = json.loads(terraform_command(environment, "output", "-json", capture=True).stdout)
    verify(outputs)


def main():
    environment = load_environment()
    command = sys.argv[1] if len(sys.argv) > 1 else "deploy"
    if command == "deploy":
        deploy(environment)
    elif command == "verify":
        outputs = json.loads(terraform_command(environment, "output", "-json", capture=True).stdout)
        verify(outputs)
    else:
        if command in {"plan", "apply", "import", "destroy"}:
            configure(environment, aws_session(environment))
        terraform_command(environment, command, *sys.argv[2:])


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        if error.stderr:
            print(error.stderr, file=sys.stderr)
        sys.exit(error.returncode)
    except Exception as error:
        sys.exit(str(error))
