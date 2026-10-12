import ipaddress
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

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
    environment["TF_VAR_lambda_role_name"] = environment.get("LAMBDA_ROLE_NAME", environment.get("TF_VAR_lambda_role_name", "LambdaAccessToDynamoDB"))
    environment["TF_VAR_lambda_runtime"] = environment.get("LAMBDA_RUNTIME", environment.get("TF_VAR_lambda_runtime", "python3.12"))
    session.client("iam").get_role(RoleName=environment["TF_VAR_lambda_role_name"])
    if region != environment["TF_VAR_aws_region"]:
        raise RuntimeError("The lab website bucket and Lambda functions must be in the same AWS region.")
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


def import_api_and_bucket(environment, session, s3, bucket):
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


def package_functions():
    TOOLS.mkdir(exist_ok=True)
    for source in sorted((ROOT / "functions").glob("*_code.py")):
        entry = zipfile.ZipInfo(source.name, date_time=(2024, 1, 1, 0, 0, 0))
        entry.compress_type = zipfile.ZIP_DEFLATED
        entry.external_attr = 0o644 << 16
        with zipfile.ZipFile(TOOLS / f"{source.stem}.zip", "w") as archive:
            archive.writestr(entry, source.read_bytes())


def import_existing(environment, session, s3, bucket):
    state = state_addresses(environment)
    dynamodb = session.client("dynamodb")
    try:
        table = dynamodb.describe_table(TableName="FoodProducts")["Table"]
    except dynamodb.exceptions.ResourceNotFoundException:
        table = None
    if table:
        if table["KeySchema"] != [{"AttributeName": "product_name", "KeyType": "HASH"}]:
            raise RuntimeError("Existing FoodProducts has an incompatible primary key.")
        if "aws_dynamodb_table.food_products" not in state:
            terraform_command(environment, "import", "-input=false", "-no-color", "aws_dynamodb_table.food_products", "FoodProducts")
    import_api_and_bucket(environment, session, s3, bucket)
    functions = session.client("lambda")
    routes = {
        "/products": ("GET", "get_all_products"),
        "/products/on_offer": ("GET", "get_all_products"),
        "/create_report": ("POST", "create_report"),
    }
    for name in ("get_all_products", "create_report"):
        try:
            functions.get_function_configuration(FunctionName=name)
        except functions.exceptions.ResourceNotFoundException:
            continue
        address = f'aws_lambda_function.functions["{name}"]'
        if address not in state:
            terraform_command(environment, "import", "-input=false", "-no-color", address, name)
        try:
            policy = json.loads(functions.get_policy(FunctionName=name)["Policy"])
        except functions.exceptions.ResourceNotFoundException:
            policy = {"Statement": []}
        statements = {item["Sid"] for item in policy["Statement"]}
        for path, (method, function) in routes.items():
            statement = f"AllowGateway{method}{path.replace('/', '-').replace('_', '-')}"
            address = f'aws_lambda_permission.gateway["{path}"]'
            if function == name and statement in statements and address not in state:
                terraform_command(environment, "import", "-input=false", "-no-color", address, f"{name}/{statement}")


def expected_products(session):
    client = session.client("dynamodb")
    products = []
    options = {"TableName": "FoodProducts", "ConsistentRead": True}
    while True:
        response = client.scan(**options)
        for item in response.get("Items", []):
            product = {
                "product_name_str": item["product_name"]["S"],
                "product_id_str": item.get("product_id", item.get("product_id_str"))["S"],
                "price_in_cents_int": int(item["price_in_cents"]["N"]),
                "description_str": item["description"]["S"],
                "tag_str_arr": [tag["S"] for tag in item["tags"]["L"]],
            }
            if "special" in item:
                product["special_int"] = int(item["special"]["N"])
            products.append(product)
        if "LastEvaluatedKey" not in response:
            return products
        options["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def normalized(products):
    return sorted(products, key=lambda item: item["product_name_str"])


def check_response(actual, expected):
    if "product_item_arr" in expected:
        if normalized(actual.get("product_item_arr", [])) != normalized(expected["product_item_arr"]):
            raise RuntimeError("Products returned by Lambda do not match DynamoDB.")
    elif actual != expected:
        raise RuntimeError("The report response does not match the lab.")


def verify_website(base, website):
    origin = website.rsplit("/", 1)[0]
    for attempt in range(24):
        try:
            status, headers, body = http_request(website)
            if status != 200 or "text/html" not in headers.get("Content-Type", "") or "config.js" not in body:
                raise RuntimeError("The cafe website could not be loaded.")
            _, _, config = http_request(origin + "/config.js")
            settings = json.loads(config.split("=", 1)[1].strip().removesuffix(";"))
            if settings != {"API_GW_BASE_URL_STR": base, "COGNITO_LOGIN_BASE_URL_STR": None}:
                raise RuntimeError("Website config.js does not match the deployed API.")
            return
        except (urllib.error.URLError, RuntimeError, json.JSONDecodeError):
            if attempt == 23:
                raise
            if attempt == 0:
                print("Waiting for website configuration to become available...", flush=True)
            time.sleep(5)


def verify(outputs, session):
    base = outputs["api_base_url"]["value"]
    website = outputs["website_url"]["value"]
    origin = website.rsplit("/", 1)[0]
    products = expected_products(session)
    if len(products) != 26:
        raise RuntimeError(f"Expected 26 seeded products, found {len(products)}.")
    specials = [item for item in products if "special_int" in item and "out of stock" not in item["tag_str_arr"]]
    routes = [
        ("/products", "GET", "get_all_products", {}, {"product_item_arr": products}),
        ("/products/on_offer", "GET", "get_all_products", {"path": "on_offer"}, {"product_item_arr": specials}),
        ("/create_report", "POST", "create_report", {}, {"msg_str": "Report processing, check your phone shortly"}),
    ]
    functions = session.client("lambda")
    gateway = session.client("apigateway")
    resources = {item["path"]: item["id"] for page in gateway.get_paginator("get_resources").paginate(restApiId=outputs["api_id"]["value"]) for item in page.get("items", [])}
    for path, method, function, event, expected in routes:
        for attempt in range(24):
            try:
                invocation = functions.invoke(FunctionName=function, Payload=json.dumps(event).encode())
                payload = json.loads(invocation["Payload"].read())
                if invocation.get("FunctionError"):
                    raise RuntimeError(f"Lambda {function}: {payload}")
                check_response(payload, expected)
                status, headers, body = http_request(base + path, method, b"{}" if method == "POST" else None, {"Content-Type": "application/json", "Origin": origin})
                if status != 200:
                    raise RuntimeError(f"Unexpected HTTP status: {path}")
                check_response(json.loads(body), expected)
                if method == "GET" and {key.lower(): value for key, value in headers.items()}.get("access-control-allow-origin") != "*":
                    raise RuntimeError(f"Missing CORS header: {path}")
                break
            except (urllib.error.URLError, RuntimeError, json.JSONDecodeError):
                if attempt == 23:
                    raise
                if attempt == 0:
                    print(f"Waiting for the API deployment: {path}...", flush=True)
                time.sleep(5)
        integration = gateway.get_integration(restApiId=outputs["api_id"]["value"], resourceId=resources[path], httpMethod=method)
        if integration["type"] != "AWS" or f":function:{function}/invocations" not in integration.get("uri", ""):
            raise RuntimeError(f"The route is not connected to the correct Lambda function: {path}")
        if path.endswith("/on_offer") and json.loads(integration["requestTemplates"]["application/json"]) != {"path": "on_offer"}:
            raise RuntimeError("The on_offer route must send its path to Lambda.")
        if method == "GET":
            status, headers, _ = http_request(base + path, "OPTIONS", headers={"Origin": origin, "Access-Control-Request-Method": "GET"})
            headers = {key.lower(): value for key, value in headers.items()}
            if status != 200 or headers.get("access-control-allow-origin") != "*" or "GET" not in headers.get("access-control-allow-methods", ""):
                raise RuntimeError(f"CORS preflight failed: {path}")
    for response_type in ("DEFAULT_4XX", "DEFAULT_5XX"):
        response = gateway.get_gateway_response(restApiId=outputs["api_id"]["value"], responseType=response_type)
        if response.get("responseParameters", {}).get("gatewayresponse.header.Access-Control-Allow-Origin") != "'*'":
            raise RuntimeError(f"Missing CORS on {response_type}.")
    verify_website(base, website)
    print(f"Verified: two Lambda functions, three API routes, {len(products)} products, {len(specials)} in-stock specials, CORS and cafe website.", flush=True)
    print(f"Website: {website}", flush=True)
    print(f"API: {base}", flush=True)


def verify_price_change(outputs, session):
    dynamodb = session.client("dynamodb")
    key = {"product_name": {"S": "apple pie slice"}}
    item = dynamodb.get_item(TableName="FoodProducts", Key=key, ConsistentRead=True)["Item"]
    original = item["price_in_cents"]
    updated = {"N": str(int(original["N"]) + 100)}
    dynamodb.update_item(TableName="FoodProducts", Key=key, UpdateExpression="SET price_in_cents = :new", ConditionExpression="price_in_cents = :old", ExpressionAttributeValues={":old": original, ":new": updated})
    try:
        for path in ("/products", "/products/on_offer"):
            for attempt in range(12):
                _, _, body = http_request(outputs["api_base_url"]["value"] + path)
                products = json.loads(body).get("product_item_arr", [])
                selected = next((product for product in products if product["product_name_str"] == "apple pie slice"), {})
                if selected.get("price_in_cents_int") == int(updated["N"]):
                    break
                if attempt == 11:
                    raise RuntimeError(f"A DynamoDB price update was not reflected by {path}.")
                time.sleep(3)
    finally:
        dynamodb.update_item(TableName="FoodProducts", Key=key, UpdateExpression="SET price_in_cents = :old", ConditionExpression="price_in_cents = :new", ExpressionAttributeValues={":old": original, ":new": updated})
    print("Verified: DynamoDB price changes reach both API routes; original price restored.", flush=True)


def review_plan(environment, plan):
    document = json.loads(terraform_command(environment, "show", "-json", plan, capture=True).stdout)
    destructive = [item["address"] for item in document.get("resource_changes", []) if "delete" in item["change"]["actions"]]
    if destructive:
        raise RuntimeError(f"The lab runner will not delete or replace existing resources: {', '.join(destructive)}")


def deploy(environment):
    session = aws_session(environment)
    bucket, s3 = configure(environment, session)
    package_functions()
    terraform_command(environment, "init", "-input=false", "-no-color")
    terraform_command(environment, "validate", "-no-color")
    import_existing(environment, session, s3, bucket)
    plan = str(TOOLS / "lab.tfplan")
    terraform_command(environment, "plan", "-input=false", "-no-color", "-parallelism=1", f"-out={plan}")
    review_plan(environment, plan)
    terraform_command(environment, "apply", "-input=false", "-no-color", "-parallelism=1", plan)
    outputs = json.loads(terraform_command(environment, "output", "-json", capture=True).stdout)
    verify(outputs, session)
    verify_price_change(outputs, session)
    print("Lab 7.1 complete.", flush=True)


def main():
    environment = load_environment()
    command = sys.argv[1] if len(sys.argv) > 1 else "deploy"
    if command == "deploy":
        deploy(environment)
    elif command in {"verify", "price-test"}:
        outputs = json.loads(terraform_command(environment, "output", "-json", capture=True).stdout)
        session = aws_session(environment)
        if command == "verify":
            verify(outputs, session)
        else:
            verify_price_change(outputs, session)
    else:
        package_functions()
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
    except urllib.error.HTTPError as error:
        sys.exit(f"HTTP {error.code} for {error.url}: {error.read().decode()[:500]}")
    except Exception as error:
        sys.exit(str(error))


