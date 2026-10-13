import os
import boto3
from botocore.config import Config


s3 = boto3.client("s3", config=Config(signature_version="s3v4"))


def lambda_handler(event, context):
    return {
        "presigned_url_str": s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": os.environ["REPORT_BUCKET"], "Key": "report.html"},
            ExpiresIn=int(os.environ.get("REPORT_EXPIRY", "120")),
        )
    }
