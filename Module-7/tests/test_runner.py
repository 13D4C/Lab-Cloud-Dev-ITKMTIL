import json
from pathlib import Path
import sys
import unittest
import urllib.error
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run


class RunnerTests(unittest.TestCase):
    def test_website_recovers_from_temporary_access_denied(self):
        base = "https://example.execute-api.us-east-1.amazonaws.com/prod"
        website = "https://example.s3.us-east-1.amazonaws.com/index.html"
        config = "window.COFFEE_CONFIG = " + json.dumps({"API_GW_BASE_URL_STR": base, "COGNITO_LOGIN_BASE_URL_STR": None}) + ";"
        responses = [
            urllib.error.HTTPError(website, 403, "Forbidden", {}, None),
            (200, {"Content-Type": "text/html"}, '<script src="config.js"></script>'),
            (200, {}, config),
        ]
        with patch.object(run, "http_request", side_effect=responses), patch.object(run.time, "sleep") as sleep:
            run.verify_website(base, website)
        sleep.assert_called_once_with(5)

    def test_price_is_restored_when_api_verification_fails(self):
        session = Mock()
        dynamodb = session.client.return_value
        dynamodb.get_item.return_value = {"Item": {"price_in_cents": {"N": "595"}}}
        outputs = {"api_base_url": {"value": "https://example/prod"}}
        with patch.object(run, "http_request", side_effect=RuntimeError("API unavailable")):
            with self.assertRaisesRegex(RuntimeError, "API unavailable"):
                run.verify_price_change(outputs, session)
        self.assertEqual(dynamodb.update_item.call_count, 2)
        restoration = dynamodb.update_item.call_args.kwargs
        self.assertEqual(restoration["ExpressionAttributeValues"], {":old": {"N": "595"}, ":new": {"N": "695"}})
        self.assertEqual(restoration["UpdateExpression"], "SET price_in_cents = :old")
        self.assertEqual(restoration["ConditionExpression"], "price_in_cents = :new")


if __name__ == "__main__":
    unittest.main()
