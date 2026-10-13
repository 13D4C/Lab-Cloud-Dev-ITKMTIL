import boto3

BUCKET_NAME = 'YOUR_BUCKET_NAME'
FILE_NAME = 'index.html'
s3_client = boto3.client('s3')


def put_bucket_ownership_controls():
    return s3_client.put_bucket_ownership_controls(
        Bucket=BUCKET_NAME,
        OwnershipControls={'Rules': [{'ObjectOwnership': 'BucketOwnerPreferred'}]},
    )


def set_public_access_block():
    return s3_client.put_public_access_block(
        Bucket=BUCKET_NAME,
        PublicAccessBlockConfiguration={
            'BlockPublicAcls': False,
            'IgnorePublicAcls': False,
            'BlockPublicPolicy': False,
            'RestrictPublicBuckets': False,
        },
    )


def allow_public_access_to_file():
    return s3_client.put_object_acl(Bucket=BUCKET_NAME, Key=FILE_NAME, ACL='public-read')


if __name__ == '__main__':
    if BUCKET_NAME.startswith('YOUR_'):
        raise RuntimeError('Set BUCKET_NAME to the sample bucket before running.')
    put_bucket_ownership_controls()
    set_public_access_block()
    allow_public_access_to_file()
