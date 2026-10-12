import os

import boto3
from boto3.dynamodb.conditions import Attr


def lambda_handler(event, context):
    table = boto3.resource("dynamodb").Table(os.environ.get("TABLE_NAME", "FoodProducts"))
    options = {}
    if (event or {}).get("path") is not None:
        print("running scan on index")
        options = {
            "IndexName": os.environ.get("INDEX_NAME", "special_GSI"),
            "FilterExpression": ~Attr("tags").contains("out of stock"),
        }
    else:
        print("running scan on table")
        options["ConsistentRead"] = True
    products = []
    while True:
        response = table.scan(**options)
        for item in response.get("Items", []):
            product = {
                "product_name_str": item["product_name"],
                "product_id_str": item.get("product_id", item.get("product_id_str")),
                "price_in_cents_int": int(item["price_in_cents"]),
                "description_str": item["description"],
                "tag_str_arr": item["tags"],
            }
            if "special" in item:
                product["special_int"] = int(item["special"])
            products.append(product)
        if "LastEvaluatedKey" not in response:
            break
        options["ExclusiveStartKey"] = response["LastEvaluatedKey"]
    return {"product_item_arr": products}
