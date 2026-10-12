from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
for dependencies in ROOT.parent.glob("Module-*/.tools/python"):
    sys.path.insert(0, str(dependencies))


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "functions" / f"{name}_code.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FunctionTests(unittest.TestCase):
    def setUp(self):
        self.module = load("get_all_products")
        self.item = {
            "product_name": "apple pie slice",
            "product_id": "a444",
            "price_in_cents": Decimal("595"),
            "special": Decimal("1"),
            "description": "A slice of apple pie.",
            "tags": ["pie slice", "on offer"],
        }

    def test_all_products_paginates_and_serializes_numbers(self):
        table = Mock()
        table.scan.side_effect = [
            {"Items": [self.item], "LastEvaluatedKey": {"product_name": "apple pie slice"}},
            {"Items": [{**self.item, "product_name": "apple pie"}]},
        ]
        with patch.object(self.module.boto3, "resource") as resource:
            resource.return_value.Table.return_value = table
            result = self.module.lambda_handler({}, None)
        self.assertEqual(len(result["product_item_arr"]), 2)
        self.assertEqual(result["product_item_arr"][0]["price_in_cents_int"], 595)
        self.assertIsInstance(result["product_item_arr"][0]["special_int"], int)
        self.assertEqual(table.scan.call_args_list[1].kwargs["ExclusiveStartKey"], {"product_name": "apple pie slice"})
        self.assertTrue(table.scan.call_args_list[1].kwargs["ConsistentRead"])
        json.dumps(result)

    def test_specials_keeps_filter_on_empty_paginated_results(self):
        table = Mock()
        table.scan.side_effect = [
            {"Items": [], "LastEvaluatedKey": {"product_name": "out of stock"}},
            {"Items": [self.item]},
        ]
        with patch.object(self.module.boto3, "resource") as resource:
            resource.return_value.Table.return_value = table
            result = self.module.lambda_handler({"path": "on_offer"}, None)
        self.assertEqual(len(result["product_item_arr"]), 1)
        for call in table.scan.call_args_list:
            self.assertEqual(call.kwargs["IndexName"], "special_GSI")
            self.assertNotIn("ConsistentRead", call.kwargs)
            condition = call.kwargs["FilterExpression"].get_expression()
            self.assertEqual(condition["operator"], "NOT")
            contains = condition["values"][0].get_expression()
            self.assertEqual(contains["operator"], "contains")
            self.assertEqual(contains["values"][1], "out of stock")

    def test_legacy_product_id_and_missing_special(self):
        item = dict(self.item)
        item["product_id_str"] = item.pop("product_id")
        del item["special"]
        with patch.object(self.module.boto3, "resource") as resource:
            resource.return_value.Table.return_value.scan.return_value = {"Items": [item]}
            product = self.module.lambda_handler(None, None)["product_item_arr"][0]
        self.assertEqual(product["product_id_str"], "a444")
        self.assertNotIn("special_int", product)

    def test_report_matches_lab_response(self):
        report = load("create_report")
        self.assertEqual(report.lambda_handler({}, None), {"msg_str": "Report processing, check your phone shortly"})


if __name__ == "__main__":
    unittest.main()
