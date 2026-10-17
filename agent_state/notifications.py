"""Provider notification adapters. Messages invalidate; they never publish state."""
from .storage import Invalid


def gcs_notification(runtime, attributes, bucket=None):
    if not isinstance(attributes, dict):
        raise Invalid("Notification attributes must be an object")
    if bucket and attributes.get("bucketId") != bucket:
        return False
    prefix = runtime.engine.objects.prefix + "/tasks/"
    key = attributes.get("objectId", "")
    if not isinstance(key, str) or not key.startswith(prefix) or not key.endswith("/head.json"):
        return False
    task_id = key[len(prefix):-len("/head.json")]
    if "/" in task_id:
        return False
    kind = attributes.get("eventType")
    if kind not in {"OBJECT_FINALIZE", "OBJECT_DELETE"}:
        return False
    try:
        token = int(attributes["objectGeneration"])
    except (KeyError, ValueError, TypeError) as error:
        raise Invalid("Notification requires objectGeneration") from error
    current = runtime.cached_token(task_id)
    # GCS-specific ordered hint optimization stays in its adapter. Generic
    # backends must never interpret ETags as increasing integers or strings.
    if kind != "OBJECT_DELETE" and type(current) is int and token <= current:
        return False
    return runtime.invalidate(task_id, token, kind == "OBJECT_DELETE")
