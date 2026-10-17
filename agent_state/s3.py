"""S3 conditional writes. Compatibility labels do not replace live validation."""
from urllib.parse import urlparse
from .storage import Object, Metrics, Conflict, Corrupt, Invalid, Missing, TooLarge


class S3Store:
    provider = "s3"

    def __init__(self, bucket, region=None, endpoint=None, client=None):
        if not bucket or "/" in bucket:
            raise Invalid("Supply an S3 bucket name without a URL")
        if endpoint and (urlparse(endpoint).scheme != "https" or not urlparse(endpoint).netloc):
            raise Invalid("S3 endpoint must be an HTTPS URL")
        from botocore.exceptions import ClientError
        self.error_type = ClientError
        self.bucket, self.region = bucket, region
        self.metrics = Metrics()
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client("s3", region_name=region, endpoint_url=endpoint,
                                  config=Config(signature_version="s3v4", connect_timeout=10,
                                                read_timeout=30, retries={"total_max_attempts": 1}))
        self.client = client

    def get(self, key, max_bytes=1048576):
        self.metrics.add(reads=1, sdk_calls=1)
        try:
            result = self.client.get_object(Bucket=self.bucket, Key=key)
        except self.error_type as error:
            if error.response["Error"]["Code"] == "NoSuchKey":
                raise Missing(key) from error
            raise
        body = result["Body"]
        try:
            size = result.get("ContentLength")
            if type(size) is not int or size < 0 or size > max_bytes:
                raise TooLarge("S3 response length missing or exceeds limit")
            data = body.read(max_bytes + 1)
            if len(data) != size or not result.get("ETag"):
                raise Corrupt("Invalid S3 response length or ETag")
        finally:
            body.close()
        self.metrics.add(downloaded_bytes=len(data))
        return Object(data, result["ETag"])

    def put(self, key, data, expected_generation):
        if expected_generation == 0:
            condition = dict(IfNoneMatch="*")
        elif isinstance(expected_generation, str) and expected_generation:
            condition = dict(IfMatch=expected_generation)
        else:
            raise Invalid("S3 update requires an opaque ETag token")
        self.metrics.add(writes=1, sdk_calls=1)
        try:
            result = self.client.put_object(Bucket=self.bucket, Key=key, Body=data,
                                            ContentLength=len(data), ContentType="application/octet-stream",
                                            **condition)
        except self.error_type as error:
            code = error.response["Error"]["Code"]
            if code in {"PreconditionFailed", "ConditionalRequestConflict", "OperationAborted"} or (
                    code == "NoSuchKey" and expected_generation != 0):
                self.metrics.add(cas_conflicts=1)
                raise Conflict("S3 conditional publication failed") from error
            raise
        if not result.get("ETag"):
            raise Corrupt("Missing S3 write ETag; outcome requires verification")
        self.metrics.add(uploaded_bytes=len(data))
        return result["ETag"]

    def check(self):
        self.metrics.add(sdk_calls=1)
        self.client.head_bucket(Bucket=self.bucket)
        return dict(provider="s3", bucket=self.bucket, region=self.region,
                    note="Bucket access checked; conditional-write support requires live smoke test")
