from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib import request
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse

from greenference_persistence import RuntimeSettings
from greenference_protocol import BuildContextRecord, BuildRecord


class BuilderExecutionError(RuntimeError):
    def __init__(self, message: str, *, operation: str, failure_class: str, retryable: bool) -> None:
        super().__init__(message)
        self.operation = operation
        self.failure_class = failure_class
        self.retryable = retryable


@dataclass(slots=True)
class StagedContext:
    context: BuildContextRecord
    log_uri: str
    message: str


@dataclass(slots=True)
class PublishedImage:
    registry_repository: str
    image_tag: str
    artifact_uri: str
    artifact_digest: str
    registry_manifest_uri: str
    executor_name: str
    message: str


@dataclass(slots=True)
class BuildPreparation:
    executor_name: str
    log_uri: str
    message: str
    initial_stage: str = "staging"


@dataclass(slots=True)
class BuildStageResult:
    stage: str
    next_stage: str | None
    message: str
    stage_state: dict[str, str] | None = None
    context: BuildContextRecord | None = None
    published_image: PublishedImage | None = None


class BuildRunner:
    def prepare_job(self, build: BuildRecord, context: BuildContextRecord) -> BuildPreparation:
        raise NotImplementedError

    def run_stage(
        self,
        build: BuildRecord,
        context: BuildContextRecord,
        stage: str,
        *,
        stage_state: dict[str, str] | None = None,
    ) -> BuildStageResult:
        raise NotImplementedError

    def finalize_success(self, build: BuildRecord, published: PublishedImage) -> BuildRecord:
        raise NotImplementedError

    def finalize_failure(self, build: BuildRecord, exc: BuilderExecutionError) -> BuildRecord:
        raise NotImplementedError

    def build_log_uri(self, build_id: str) -> str:
        raise NotImplementedError


class ObjectStoreAdapter:
    def stage_context(self, build: BuildRecord, context: BuildContextRecord) -> StagedContext:
        raise NotImplementedError

    def build_log_uri(self, build_id: str) -> str:
        raise NotImplementedError

    def cleanup(self, build: BuildRecord, context: BuildContextRecord | None) -> str:
        raise NotImplementedError


class RegistryAdapter:
    def publish(self, build: BuildRecord, context: BuildContextRecord) -> PublishedImage:
        raise NotImplementedError

    def cleanup(self, build: BuildRecord) -> str:
        raise NotImplementedError


class BuildExecutorAdapter:
    def execute_build(self, build: BuildRecord, context: BuildContextRecord) -> PublishedImage:
        raise NotImplementedError


class AdapterBackedBuildRunner(BuildRunner):
    def __init__(
        self,
        object_store: ObjectStoreAdapter,
        registry: RegistryAdapter,
        executor: BuildExecutorAdapter | None = None,
    ) -> None:
        self.object_store = object_store
        self.registry = registry
        self.executor = executor

    def prepare_job(self, build: BuildRecord, context: BuildContextRecord) -> BuildPreparation:
        return BuildPreparation(
            executor_name=type(self.executor).__name__ if self.executor is not None else "adapter-runner",
            log_uri=self.object_store.build_log_uri(build.build_id),
            message="prepared build job for staged execution",
        )

    def run_stage(
        self,
        build: BuildRecord,
        context: BuildContextRecord,
        stage: str,
        *,
        stage_state: dict[str, str] | None = None,
    ) -> BuildStageResult:
        if stage == "staging":
            staged = self.object_store.stage_context(build, context)
            return BuildStageResult(
                stage="staging",
                next_stage="building",
                message=staged.message,
                stage_state={
                    "staged_context_uri": staged.context.staged_context_uri or "",
                    "context_manifest_uri": staged.context.context_manifest_uri or "",
                    "log_uri": staged.log_uri,
                },
                context=staged.context,
            )
        if stage == "building":
            if self.executor is not None:
                published = self.executor.execute_build(build, context)
                return BuildStageResult(
                    stage="building",
                    next_stage="publishing",
                    message=published.message,
                    stage_state={
                        "remote_build": "true",
                        "artifact_uri": published.artifact_uri,
                        "artifact_digest": published.artifact_digest,
                        "registry_manifest_uri": published.registry_manifest_uri,
                        "registry_repository": published.registry_repository,
                        "image_tag": published.image_tag,
                        "executor_name": published.executor_name,
                    },
                    context=context,
                )
            return BuildStageResult(
                stage="building",
                next_stage="publishing",
                message="prepared staged context for registry publish",
                stage_state={
                    "staged_context_uri": context.staged_context_uri or "",
                    "context_manifest_uri": context.context_manifest_uri or "",
                    "prepared": "true",
                },
                context=context,
            )
        if stage == "publishing":
            if stage_state and stage_state.get("remote_build") == "true":
                published = PublishedImage(
                    registry_repository=stage_state["registry_repository"],
                    image_tag=stage_state["image_tag"],
                    artifact_uri=stage_state["artifact_uri"],
                    artifact_digest=stage_state["artifact_digest"],
                    registry_manifest_uri=stage_state["registry_manifest_uri"],
                    executor_name=stage_state.get("executor_name", "remote-build-executor"),
                    message=stage_state.get("last_stage_message", "recorded remote build publish result"),
                )
                return BuildStageResult(
                    stage="publishing",
                    next_stage=None,
                    message=f"recorded published image {published.registry_manifest_uri}",
                    stage_state={
                        "artifact_uri": published.artifact_uri,
                        "artifact_digest": published.artifact_digest,
                        "registry_manifest_uri": published.registry_manifest_uri,
                        "registry_repository": published.registry_repository,
                        "image_tag": published.image_tag,
                    },
                    context=context,
                    published_image=published,
                )
            published = self.registry.publish(build, context)
            return BuildStageResult(
                stage="publishing",
                next_stage=None,
                message=published.message,
                stage_state={
                    "artifact_uri": published.artifact_uri,
                    "artifact_digest": published.artifact_digest,
                    "registry_manifest_uri": published.registry_manifest_uri,
                    "registry_repository": published.registry_repository,
                    "image_tag": published.image_tag,
                },
                context=context,
                published_image=published,
            )
        raise ValueError(f"unsupported build stage: {stage}")

    def finalize_success(self, build: BuildRecord, published: PublishedImage) -> BuildRecord:
        build.status = "published"
        build.registry_repository = published.registry_repository
        build.image_tag = published.image_tag
        build.artifact_digest = published.artifact_digest
        build.artifact_uri = published.artifact_uri
        build.registry_manifest_uri = published.registry_manifest_uri
        build.executor_name = published.executor_name
        build.failure_class = None
        build.failure_reason = None
        build.cleanup_status = None
        build.last_operation = "published"
        build.updated_at = datetime.now(UTC)
        return build

    def finalize_failure(self, build: BuildRecord, exc: BuilderExecutionError) -> BuildRecord:
        build.status = "failed"
        build.failure_reason = str(exc)
        build.failure_class = exc.failure_class
        build.last_operation = exc.operation
        build.updated_at = datetime.now(UTC)
        return build

    def build_log_uri(self, build_id: str) -> str:
        return self.object_store.build_log_uri(build_id)


class SimulatedObjectStoreAdapter(ObjectStoreAdapter):
    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings

    def stage_context(self, build: BuildRecord, context: BuildContextRecord) -> StagedContext:
        _maybe_inject_transient_failure(build, "fail-once-object-store", "stage_context", "object_store_failure")
        bucket = self.settings.object_store_bucket
        staged_context_uri = f"s3://{bucket}/contexts/{build.build_id}/context.tar.gz"
        context_manifest_uri = f"s3://{bucket}/manifests/{build.build_id}.json"
        updated_context = context.model_copy(
            update={
                "staged_context_uri": staged_context_uri,
                "context_manifest_uri": context_manifest_uri,
            }
        )
        return StagedContext(
            context=updated_context,
            log_uri=self.build_log_uri(build.build_id),
            message=f"staged build context at {staged_context_uri}",
        )

    def build_log_uri(self, build_id: str) -> str:
        return f"s3://{self.settings.object_store_bucket}/build-logs/{build_id}.log"

    def cleanup(self, build: BuildRecord, context: BuildContextRecord | None) -> str:
        return "simulated object store cleanup completed"


class S3CompatibleObjectStoreAdapter(ObjectStoreAdapter):
    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings
        self._bucket_ready = False

    def stage_context(self, build: BuildRecord, context: BuildContextRecord) -> StagedContext:
        _maybe_inject_transient_failure(build, "fail-once-object-store", "stage_context", "object_store_failure")
        self._ensure_bucket()
        context_key = f"contexts/{build.build_id}/context.json"
        manifest_key = f"manifests/{build.build_id}.json"
        log_key = f"build-logs/{build.build_id}.log"
        staged_context_uri = self._object_uri(context_key)
        context_manifest_uri = self._object_uri(manifest_key)
        log_uri = self._object_uri(log_key)

        context_payload = json.dumps(
            {
                "build_id": build.build_id,
                "image": build.image,
                "source_uri": context.source_uri,
                "normalized_context_uri": context.normalized_context_uri,
                "dockerfile_path": context.dockerfile_path,
                "dockerfile_object_uri": context.dockerfile_object_uri,
                "context_digest": context.context_digest,
                "staged_at": _utcnow_isoformat(),
            },
            sort_keys=True,
        ).encode()
        manifest_payload = json.dumps(
            {
                "build_id": build.build_id,
                "staged_context_uri": staged_context_uri,
                "source_uri": context.source_uri,
                "context_digest": context.context_digest,
                "dockerfile_path": context.dockerfile_path,
            },
            sort_keys=True,
        ).encode()
        log_payload = (
            f"[{_utcnow_isoformat()}] staging build context\n"
            f"source={context.source_uri}\n"
            f"staged={staged_context_uri}\n"
        ).encode()

        self._put_object(context_key, context_payload, "application/json")
        self._put_object(manifest_key, manifest_payload, "application/json")
        self._put_object(log_key, log_payload, "text/plain; charset=utf-8")

        updated_context = context.model_copy(
            update={
                "staged_context_uri": staged_context_uri,
                "context_manifest_uri": context_manifest_uri,
            }
        )
        return StagedContext(
            context=updated_context,
            log_uri=log_uri,
            message=f"uploaded build context manifest to {staged_context_uri}",
        )

    def build_log_uri(self, build_id: str) -> str:
        return self._object_uri(f"build-logs/{build_id}.log")

    def cleanup(self, build: BuildRecord, context: BuildContextRecord | None) -> str:
        self._ensure_bucket()
        keys = [
            f"contexts/{build.build_id}/context.json",
            f"manifests/{build.build_id}.json",
            f"build-logs/{build.build_id}.log",
        ]
        for key in keys:
            self._delete_object(key)
        return f"removed {len(keys)} object-store artifacts"

    def _object_uri(self, key: str) -> str:
        return f"s3://{self.settings.object_store_bucket}/{key}"

    def _ensure_bucket(self) -> None:
        if self._bucket_ready:
            return
        try:
            self._request("HEAD", "")
        except BuilderExecutionError as exc:
            if "status=404" not in str(exc):
                raise
            self._request("PUT", "", body=b"", content_type="application/octet-stream")
        self._bucket_ready = True

    def _put_object(self, key: str, body: bytes, content_type: str) -> None:
        self._request("PUT", key, body=body, content_type=content_type)

    def _request(
        self,
        method: str,
        key: str,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> request.addinfourl:
        payload = body or b""
        parsed = urlparse(self.settings.object_store_endpoint)
        host = parsed.netloc
        scheme = parsed.scheme or "http"
        bucket = self.settings.object_store_bucket
        encoded_key = quote(key, safe="/-_.~")
        canonical_uri = f"/{bucket}"
        if encoded_key:
            canonical_uri = f"{canonical_uri}/{encoded_key}"
        target = f"{scheme}://{host}{canonical_uri}"
        amz_date, date_scope = _aws_dates()
        payload_hash = hashlib.sha256(payload).hexdigest()
        headers = {
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        if content_type is not None:
            headers["content-type"] = content_type
        signed_headers = ";".join(sorted(headers))
        canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
        canonical_request = "\n".join(
            [
                method,
                canonical_uri,
                "",
                canonical_headers,
                signed_headers,
                payload_hash,
            ]
        )
        credential_scope = f"{date_scope}/us-east-1/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                credential_scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            ]
        )
        signature = _aws_signature(
            self.settings.object_store_secret_key,
            date_scope,
            "us-east-1",
            "s3",
            string_to_sign,
        )
        auth_header = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self.settings.object_store_access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        req = request.Request(url=target, data=payload if method != "HEAD" else None, method=method)
        for key_name, value in headers.items():
            req.add_header(key_name, value)
        req.add_header("Authorization", auth_header)
        try:
            return request.urlopen(req)  # noqa: S310
        except HTTPError as exc:
            raise BuilderExecutionError(
                f"object store request failed status={exc.code} target={target}",
                operation=f"object_store:{method.lower()}",
                failure_class="object_store_failure",
                retryable=exc.code >= 500,
            ) from exc
        except URLError as exc:
            raise BuilderExecutionError(
                f"object store request failed target={target}: {exc.reason}",
                operation=f"object_store:{method.lower()}",
                failure_class="object_store_failure",
                retryable=True,
            ) from exc

    def _delete_object(self, key: str) -> None:
        try:
            self._request("DELETE", key)
        except BuilderExecutionError as exc:
            if "status=404" in str(exc):
                return
            raise


class SimulatedRegistryAdapter(RegistryAdapter):
    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings

    def publish(self, build: BuildRecord, context: BuildContextRecord) -> PublishedImage:
        _maybe_inject_transient_failure(build, "fail-once-registry", "publish_registry", "registry_failure")
        registry_ref = _registry_ref(self.settings.registry_url)
        repository, image_tag = split_image_ref(build.image)
        digest = hashlib.sha256(
            (
                f"{build.build_id}:{build.image}:{context.context_digest}:"
                f"{context.staged_context_uri}:{context.dockerfile_path}"
            ).encode()
        ).hexdigest()
        artifact_uri = f"oci://{registry_ref.rstrip('/')}/{build.image}"
        manifest_uri = f"{artifact_uri}@sha256:{digest}"
        return PublishedImage(
            registry_repository=repository,
            image_tag=image_tag,
            artifact_uri=artifact_uri,
            artifact_digest=f"sha256:{digest}",
            registry_manifest_uri=manifest_uri,
            executor_name="simulated-buildkit",
            message=f"published registry manifest {manifest_uri}",
        )

    def cleanup(self, build: BuildRecord) -> str:
        return "simulated registry cleanup completed"


class OCIRegistryAdapter(RegistryAdapter):
    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings
        self.base_url = settings.registry_url.rstrip("/")

    def publish(self, build: BuildRecord, context: BuildContextRecord) -> PublishedImage:
        _maybe_inject_transient_failure(build, "fail-once-registry", "publish_registry", "registry_failure")
        repository, image_tag = split_image_ref(build.image)
        config_bytes = json.dumps(
            {
                "created": _utcnow_isoformat(),
                "architecture": "amd64",
                "os": "linux",
                "config": {"Labels": {"greenference.build_id": build.build_id}},
                "rootfs": {"type": "layers", "diff_ids": []},
                "history": [{"created": _utcnow_isoformat(), "created_by": "greenference-builder"}],
            },
            sort_keys=True,
        ).encode()
        layer_bytes = json.dumps(
            {
                "build_id": build.build_id,
                "context_digest": context.context_digest,
                "staged_context_uri": context.staged_context_uri,
                "context_manifest_uri": context.context_manifest_uri,
                "dockerfile_path": context.dockerfile_path,
            },
            sort_keys=True,
        ).encode()
        config_digest = self._push_blob(repository, config_bytes)
        layer_digest = self._push_blob(repository, layer_bytes)
        manifest_bytes = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {
                    "mediaType": "application/vnd.oci.image.config.v1+json",
                    "digest": config_digest,
                    "size": len(config_bytes),
                },
                "layers": [
                    {
                        "mediaType": "application/vnd.oci.image.layer.v1.tar+json",
                        "digest": layer_digest,
                        "size": len(layer_bytes),
                    }
                ],
                "annotations": {
                    "org.opencontainers.image.ref.name": image_tag,
                    "greenference.build_id": build.build_id,
                },
            },
            sort_keys=True,
        ).encode()
        manifest_digest = self._put_manifest(repository, image_tag, manifest_bytes)
        registry_ref = _registry_ref(self.settings.registry_url)
        artifact_uri = f"oci://{registry_ref.rstrip('/')}/{build.image}"
        manifest_uri = f"{artifact_uri}@{manifest_digest}"
        return PublishedImage(
            registry_repository=repository,
            image_tag=image_tag,
            artifact_uri=artifact_uri,
            artifact_digest=manifest_digest,
            registry_manifest_uri=manifest_uri,
            executor_name="oci-registry-http",
            message=f"pushed OCI manifest {manifest_uri}",
        )

    def cleanup(self, build: BuildRecord) -> str:
        if not build.registry_repository or not build.artifact_digest:
            return "registry cleanup skipped"
        url = f"{self.base_url}/v2/{build.registry_repository}/manifests/{quote(build.artifact_digest, safe=':')}"
        try:
            self._request("DELETE", url)
        except BuilderExecutionError as exc:
            if "status=404" not in str(exc):
                raise
        return "registry manifest cleanup completed"

    def _push_blob(self, repository: str, body: bytes) -> str:
        digest = f"sha256:{hashlib.sha256(body).hexdigest()}"
        start_url = f"{self.base_url}/v2/{repository}/blobs/uploads/"
        start_response = self._request("POST", start_url)
        location = start_response.headers.get("Location")
        if not location:
            raise ValueError(f"registry upload location missing for {repository}")
        upload_url = urljoin(f"{self.base_url}/", location)
        separator = "&" if "?" in upload_url else "?"
        final_url = f"{upload_url}{separator}digest={quote(digest, safe=':')}"
        self._request("PUT", final_url, body=body, content_type="application/octet-stream")
        return digest

    def _put_manifest(self, repository: str, image_tag: str, body: bytes) -> str:
        url = f"{self.base_url}/v2/{repository}/manifests/{image_tag}"
        response = self._request(
            "PUT",
            url,
            body=body,
            content_type="application/vnd.oci.image.manifest.v1+json",
        )
        return response.headers.get("Docker-Content-Digest", f"sha256:{hashlib.sha256(body).hexdigest()}")

    @staticmethod
    def _request(
        method: str,
        url: str,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> request.addinfourl:
        req = request.Request(url=url, data=body, method=method)
        if content_type is not None:
            req.add_header("Content-Type", content_type)
        try:
            return request.urlopen(req)  # noqa: S310
        except HTTPError as exc:
            raise BuilderExecutionError(
                f"registry request failed status={exc.code} target={url}",
                operation=f"registry:{method.lower()}",
                failure_class="registry_failure",
                retryable=exc.code >= 500,
            ) from exc
        except URLError as exc:
            raise BuilderExecutionError(
                f"registry request failed target={url}: {exc.reason}",
                operation=f"registry:{method.lower()}",
                failure_class="registry_failure",
                retryable=True,
            ) from exc


class RemoteBuildExecutorAdapter(BuildExecutorAdapter):
    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings
        self.base_url = settings.build_executor_endpoint.rstrip("/")

    def execute_build(self, build: BuildRecord, context: BuildContextRecord) -> PublishedImage:
        repository, image_tag = split_image_ref(build.image)
        payload = json.dumps(
            {
                "build_id": build.build_id,
                "image": build.image,
                "registry_repository": repository,
                "image_tag": image_tag,
                "context": {
                    "source_uri": context.source_uri,
                    "normalized_context_uri": context.normalized_context_uri,
                    "staged_context_uri": context.staged_context_uri,
                    "context_manifest_uri": context.context_manifest_uri,
                    "dockerfile_path": context.dockerfile_path,
                    "dockerfile_object_uri": context.dockerfile_object_uri,
                    "context_digest": context.context_digest,
                },
                "object_store": {
                    "endpoint": self.settings.object_store_endpoint,
                    "bucket": self.settings.object_store_bucket,
                },
                "registry": {
                    "url": self.settings.registry_url,
                },
            },
            sort_keys=True,
        ).encode()
        response = self._request(
            "POST",
            f"{self.base_url}/builds",
            body=payload,
            content_type="application/json",
        )
        data = json.loads(response.read().decode() or "{}")
        try:
            return PublishedImage(
                registry_repository=str(data["registry_repository"]),
                image_tag=str(data["image_tag"]),
                artifact_uri=str(data["artifact_uri"]),
                artifact_digest=str(data["artifact_digest"]),
                registry_manifest_uri=str(data["registry_manifest_uri"]),
                executor_name=str(data.get("executor_name") or "remote-build-executor"),
                message=str(data.get("message") or f"executed remote build for {build.image}"),
            )
        except KeyError as exc:
            raise BuilderExecutionError(
                f"build executor response missing field: {exc.args[0]}",
                operation="build_executor:execute",
                failure_class="build_executor_failure",
                retryable=False,
            ) from exc

    def _request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> request.addinfourl:
        req = request.Request(url=url, data=body, method=method)
        if content_type is not None:
            req.add_header("Content-Type", content_type)
        req.add_header("Authorization", f"Bearer {self.settings.build_executor_auth_token}")
        try:
            return request.urlopen(req)  # noqa: S310
        except HTTPError as exc:
            raise BuilderExecutionError(
                f"build executor request failed status={exc.code} target={url}",
                operation=f"build_executor:{method.lower()}",
                failure_class="build_executor_failure",
                retryable=exc.code >= 500,
            ) from exc
        except URLError as exc:
            raise BuilderExecutionError(
                f"build executor request failed target={url}: {exc.reason}",
                operation=f"build_executor:{method.lower()}",
                failure_class="build_executor_failure",
                retryable=True,
            ) from exc


def create_execution_adapters(
    settings: RuntimeSettings,
) -> tuple[ObjectStoreAdapter, RegistryAdapter, BuildExecutorAdapter | None]:
    if settings.build_execution_mode == "live":
        executor: BuildExecutorAdapter | None = None
        if settings.build_executor_endpoint:
            executor = RemoteBuildExecutorAdapter(settings)
        return (
            S3CompatibleObjectStoreAdapter(settings),
            OCIRegistryAdapter(settings),
            executor,
        )
    return SimulatedObjectStoreAdapter(settings), SimulatedRegistryAdapter(settings), None


def split_image_ref(image: str) -> tuple[str, str]:
    last_slash = image.rfind("/")
    last_colon = image.rfind(":")
    if last_colon > last_slash:
        return image[:last_colon], image[last_colon + 1 :]
    return image, "latest"


def _aws_dates() -> tuple[str, str]:
    now = datetime.now(UTC)
    return now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")


def _aws_signature(secret: str, date_scope: str, region: str, service: str, string_to_sign: str) -> str:
    key_date = hmac.new(f"AWS4{secret}".encode(), date_scope.encode(), hashlib.sha256).digest()
    key_region = hmac.new(key_date, region.encode(), hashlib.sha256).digest()
    key_service = hmac.new(key_region, service.encode(), hashlib.sha256).digest()
    key_signing = hmac.new(key_service, b"aws4_request", hashlib.sha256).digest()
    return hmac.new(key_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()


def _registry_ref(registry_url: str) -> str:
    parsed = urlparse(registry_url)
    return parsed.netloc or registry_url.replace("http://", "").replace("https://", "")


def _utcnow_isoformat() -> str:
    return datetime.now(UTC).isoformat()


def _maybe_inject_transient_failure(
    build: BuildRecord,
    token: str,
    operation: str,
    failure_class: str,
) -> None:
    if token not in build.context_uri:
        return
    if build.retry_count > 0:
        return
    raise BuilderExecutionError(
        f"injected transient failure for {operation}",
        operation=operation,
        failure_class=failure_class,
        retryable=True,
    )
