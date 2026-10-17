"""Provider selection. No credentials in JSON; SDK imports are lazy."""
import json
from pathlib import Path
from .storage import Invalid

DEFAULTS = dict(provider="gcs", bucket="", project=None, region=None, endpoint=None,
                prefix="agentdb-state", host="127.0.0.1", port=8844,
                subscription=None, tokenizer=None)


def load_settings(path=None, **overrides):
    settings = dict(DEFAULTS)
    if path:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict) or set(value) - set(DEFAULTS):
            raise Invalid("Unknown config fields; credentials belong in SDK credential providers")
        settings.update(value)
    for key, value in overrides.items():
        if key not in DEFAULTS:
            raise Invalid("Unknown config override: " + key)
        if value is not None:
            settings[key] = value
    provider = settings["provider"]
    if provider not in {"gcs", "oss", "s3"}:
        raise Invalid("provider must be gcs, oss or s3")
    if not isinstance(settings["bucket"], str) or not settings["bucket"]:
        raise Invalid("An existing bucket is required; no disk/memory persistence fallback")
    if provider == "oss" and not settings["region"]:
        raise Invalid("OSS region is required, for example cn-hangzhou")
    if provider != "gcs" and (settings["subscription"] or settings["project"]):
        raise Invalid("project and subscription apply only to GCS")
    if provider == "gcs" and (settings["region"] or settings["endpoint"]):
        raise Invalid("region and endpoint apply only to OSS/S3")
    return settings


def create_store(settings):
    if settings["provider"] == "gcs":
        from .storage import GCSStore
        return GCSStore(settings["bucket"], settings.get("project"))
    if settings["provider"] == "oss":
        from .oss import OSSStore
        return OSSStore(settings["bucket"], settings["region"], settings.get("endpoint"))
    if settings["provider"] == "s3":
        from .s3 import S3Store
        return S3Store(settings["bucket"], settings.get("region"), settings.get("endpoint"))
    raise Invalid("Unsupported storage provider")
