import os

from runtime import aws_session, load_environment


def main():
    session = aws_session(load_environment())
    account = os.environ['LAB_ACCOUNT_ID']
    bucket = os.environ['LAB_SITE_BUCKET']
    if session.client('sts').get_caller_identity()['Account'] != account:
        raise RuntimeError('The bucket creation credentials belong to a different AWS account.')
    s3 = session.client('s3')
    try:
        s3.head_bucket(Bucket=bucket, ExpectedBucketOwner=account)
    except s3.exceptions.ClientError as error:
        if error.response['Error']['Code'] not in ('404', 'NoSuchBucket', 'NotFound'):
            raise
        parameters = {'Bucket': bucket}
        if session.region_name != 'us-east-1':
            parameters['CreateBucketConfiguration'] = {'LocationConstraint': session.region_name}
        s3.create_bucket(**parameters)
        s3.get_waiter('bucket_exists').wait(Bucket=bucket, ExpectedBucketOwner=account)
        print('Created the lab website bucket: ' + bucket, flush=True)
    else:
        print('Reusing the existing lab website bucket: ' + bucket, flush=True)


if __name__ == '__main__':
    main()
