import argparse
from pathlib import Path

from runtime import aws_session, load_environment


def publish_file(session, topic_arn, filename):
    client = session.client('sns')
    responses = []
    for line in Path(filename).read_text().splitlines():
        message = line.strip()
        if not message:
            continue
        arguments = dict(TopicArn=topic_arn, Message=message, Subject='New bean delivery', MessageGroupId='bean_message_group')
        if int(message.split(':')[-1]) <= 0:
            arguments.update(MessageAttributes={
                'inventory_alert': {'DataType': 'String', 'StringValue': 'out_of_stock'}
            })
        response = client.publish(**arguments)
        responses.append(response)
        print(f"Published {message}: {response['MessageId']} / {response['SequenceNumber']}", flush=True)
    return responses


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('filename', type=Path)
    args = parser.parse_args()
    environment = load_environment()
    session = aws_session(environment)
    account = session.client('sts').get_caller_identity()['Account']
    publish_file(session, f"arn:aws:sns:{environment['TF_VAR_aws_region']}:{account}:updated_beans_sns.fifo", args.filename)


if __name__ == '__main__':
    main()
