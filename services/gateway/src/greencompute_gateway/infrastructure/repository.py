from __future__ import annotations

from collections import deque
from typing import Any

from sqlalchemy import select

from greencompute_persistence import create_db_engine, create_session_factory, init_database, session_scope
from greencompute_persistence.db import needs_bootstrap
from greencompute_persistence.orm import (
    APIKeyORM,
    BareMetalInquiryORM,
    CommercialInquiryORM,
    GpuCapacityOverrideORM,
    ProviderServerORM,
    UserORM,
    UserSecretORM,
    WorkloadShareORM,
)
from greencompute_protocol import (
    APIKeyRecord,
    BareMetalInquiryRecord,
    CommercialInquiryRecord,
    GpuCapacityOverride,
    ProviderServerRecord,
    UserRecord,
    UserSecretRecord,
    WorkloadShareRecord,
)


class GatewayRepository:
    def __init__(self, database_url: str | None = None, bootstrap: bool | None = None) -> None:
        self.engine = create_db_engine(database_url)
        self.session_factory = create_session_factory(self.engine)
        self.routing_decisions: deque[dict[str, Any]] = deque(maxlen=200)
        if needs_bootstrap(str(self.engine.url), bootstrap):
            init_database(self.engine)

    def save_user(self, user: UserRecord) -> UserRecord:
        with session_scope(self.session_factory) as session:
            row = session.get(UserORM, user.user_id) or UserORM(user_id=user.user_id)
            row.username = user.username
            row.email = user.email
            row.display_name = user.display_name
            row.bio = user.bio
            row.website = user.website
            row.profile_metadata = user.metadata
            row.balance_credits = getattr(user, "balance_credits", 0)
            row.created_at = user.created_at
            session.add(row)
        return user

    def get_user(self, user_id: str) -> UserRecord | None:
        with session_scope(self.session_factory) as session:
            row = session.get(UserORM, user_id)
            return self._to_user(row) if row else None

    def get_user_by_email(self, email: str) -> UserRecord | None:
        if not email:
            return None
        with session_scope(self.session_factory) as session:
            row = session.scalars(
                select(UserORM).where(UserORM.email == email).limit(1)
            ).first()
            return self._to_user(row) if row else None

    def get_user_by_username(self, username: str) -> UserRecord | None:
        # `username` carries a UNIQUE index (ix_users_username). register_user
        # uses this to find a free username before INSERT so a duplicate display
        # name never 500s the signup (which left users with a null greenfApiKey).
        if not username:
            return None
        with session_scope(self.session_factory) as session:
            row = session.scalars(
                select(UserORM).where(UserORM.username == username).limit(1)
            ).first()
            return self._to_user(row) if row else None

    def list_users(self) -> list[UserRecord]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(select(UserORM)).all()
            return [self._to_user(row) for row in rows]

    def save_api_key(self, api_key: APIKeyRecord) -> APIKeyRecord:
        with session_scope(self.session_factory) as session:
            row = session.get(APIKeyORM, api_key.key_id) or APIKeyORM(key_id=api_key.key_id)
            row.user_id = api_key.user_id
            row.name = api_key.name
            row.admin = api_key.admin
            row.scopes = api_key.scopes
            row.secret = api_key.secret
            row.created_at = api_key.created_at
            session.add(row)
        return api_key

    def list_api_keys(self, user_id: str | None = None) -> list[APIKeyRecord]:
        with session_scope(self.session_factory) as session:
            stmt = select(APIKeyORM)
            if user_id:
                stmt = stmt.where(APIKeyORM.user_id == user_id)
            rows = session.scalars(stmt).all()
            return [self._to_api_key(row) for row in rows]

    def get_api_key(self, key_id: str) -> APIKeyRecord | None:
        with session_scope(self.session_factory) as session:
            row = session.get(APIKeyORM, key_id)
            return self._to_api_key(row) if row else None

    def delete_api_key(self, key_id: str) -> APIKeyRecord | None:
        with session_scope(self.session_factory) as session:
            row = session.get(APIKeyORM, key_id)
            if row is None:
                return None
            record = self._to_api_key(row)
            session.delete(row)
            return record

    def save_secret(self, secret: UserSecretRecord) -> UserSecretRecord:
        with session_scope(self.session_factory) as session:
            row = session.get(UserSecretORM, secret.secret_id) or UserSecretORM(secret_id=secret.secret_id)
            row.user_id = secret.user_id
            row.name = secret.name
            row.value = secret.value
            row.created_at = secret.created_at
            row.updated_at = secret.updated_at
            session.add(row)
        return secret

    def list_secrets(self, user_id: str) -> list[UserSecretRecord]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(select(UserSecretORM).where(UserSecretORM.user_id == user_id)).all()
            return [self._to_secret(row) for row in rows]

    def get_secret(self, secret_id: str) -> UserSecretRecord | None:
        with session_scope(self.session_factory) as session:
            row = session.get(UserSecretORM, secret_id)
            return self._to_secret(row) if row else None

    def delete_secret(self, secret_id: str) -> UserSecretRecord | None:
        with session_scope(self.session_factory) as session:
            row = session.get(UserSecretORM, secret_id)
            if row is None:
                return None
            record = self._to_secret(row)
            session.delete(row)
            return record

    def save_workload_share(self, share: WorkloadShareRecord) -> WorkloadShareRecord:
        with session_scope(self.session_factory) as session:
            row = session.get(WorkloadShareORM, share.share_id) or WorkloadShareORM(share_id=share.share_id)
            row.workload_id = share.workload_id
            row.owner_user_id = share.owner_user_id
            row.shared_with_user_id = share.shared_with_user_id
            row.permission = share.permission
            row.created_at = share.created_at
            session.add(row)
        return share

    def list_workload_shares(self, workload_id: str) -> list[WorkloadShareRecord]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(select(WorkloadShareORM).where(WorkloadShareORM.workload_id == workload_id)).all()
            return [self._to_workload_share(row) for row in rows]

    def list_shared_workloads_for_user(self, user_id: str) -> list[WorkloadShareRecord]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(
                select(WorkloadShareORM).where(WorkloadShareORM.shared_with_user_id == user_id)
            ).all()
            return [self._to_workload_share(row) for row in rows]

    def record_routing_decision(self, decision: dict[str, Any]) -> None:
        self.routing_decisions.appendleft(decision)

    def list_routing_decisions(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self.routing_decisions)[:limit]

    # --- Commercial inquiries (public /contact-sales) -------------------

    def save_commercial_inquiry(self, inquiry: CommercialInquiryRecord) -> CommercialInquiryRecord:
        with session_scope(self.session_factory) as session:
            row = (
                session.get(CommercialInquiryORM, inquiry.inquiry_id)
                or CommercialInquiryORM(inquiry_id=inquiry.inquiry_id)
            )
            row.name = inquiry.name
            row.email = inquiry.email
            row.company = inquiry.company
            row.gpu_count = inquiry.gpu_count
            row.duration = inquiry.duration
            row.deployment_date = inquiry.deployment_date
            row.budget = inquiry.budget
            row.use_case = inquiry.use_case
            row.discord = inquiry.discord
            row.phone = inquiry.phone
            row.source_ip = inquiry.source_ip
            row.user_agent = inquiry.user_agent
            row.status = inquiry.status
            row.notes = inquiry.notes
            row.submitted_at = inquiry.submitted_at
            row.reviewed_at = inquiry.reviewed_at
            session.add(row)
        return inquiry

    def list_commercial_inquiries(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[CommercialInquiryRecord]:
        with session_scope(self.session_factory) as session:
            stmt = select(CommercialInquiryORM).order_by(
                CommercialInquiryORM.submitted_at.desc()
            )
            if status:
                stmt = stmt.where(CommercialInquiryORM.status == status)
            rows = session.scalars(stmt.offset(offset).limit(limit)).all()
            return [self._to_inquiry(row) for row in rows]

    def count_commercial_inquiries_from_ip_since(
        self, source_ip: str, since_seconds: int
    ) -> int:
        """Rate-limit helper — how many inquiries this IP submitted recently."""
        from datetime import timedelta
        from greencompute_persistence.orm import utcnow

        if not source_ip:
            return 0
        cutoff = utcnow() - timedelta(seconds=since_seconds)
        with session_scope(self.session_factory) as session:
            rows = session.scalars(
                select(CommercialInquiryORM).where(
                    CommercialInquiryORM.source_ip == source_ip,
                    CommercialInquiryORM.submitted_at >= cutoff,
                )
            ).all()
            return len(rows)

    def update_commercial_inquiry_status(
        self, inquiry_id: str, *, status: str | None = None, notes: str | None = None
    ) -> CommercialInquiryRecord | None:
        from greencompute_persistence.orm import utcnow

        with session_scope(self.session_factory) as session:
            row = session.get(CommercialInquiryORM, inquiry_id)
            if row is None:
                return None
            if status is not None:
                row.status = status
            if notes is not None:
                row.notes = notes
            row.reviewed_at = utcnow()
            session.add(row)
            return self._to_inquiry(row)

    # --- Bare-metal inquiries (dedicated /rental sales form) ------------

    def save_bare_metal_inquiry(self, inquiry: BareMetalInquiryRecord) -> BareMetalInquiryRecord:
        with session_scope(self.session_factory) as session:
            row = (
                session.get(BareMetalInquiryORM, inquiry.inquiry_id)
                or BareMetalInquiryORM(inquiry_id=inquiry.inquiry_id)
            )
            row.name = inquiry.name
            row.email = inquiry.email
            row.company = inquiry.company
            row.card_type = inquiry.card_type
            row.node_count = inquiry.node_count
            row.required_vram_gb = inquiry.required_vram_gb
            row.storage_gb_per_node = inquiry.storage_gb_per_node
            row.work_type = inquiry.work_type
            row.deployment_date = inquiry.deployment_date
            row.duration = inquiry.duration
            row.notes = inquiry.notes
            row.discord = inquiry.discord
            row.phone = inquiry.phone
            row.source_ip = inquiry.source_ip
            row.user_agent = inquiry.user_agent
            row.status = inquiry.status
            row.review_notes = inquiry.review_notes
            row.submitted_at = inquiry.submitted_at
            row.reviewed_at = inquiry.reviewed_at
            session.add(row)
        return inquiry

    def list_bare_metal_inquiries(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[BareMetalInquiryRecord]:
        with session_scope(self.session_factory) as session:
            stmt = select(BareMetalInquiryORM).order_by(
                BareMetalInquiryORM.submitted_at.desc()
            )
            if status:
                stmt = stmt.where(BareMetalInquiryORM.status == status)
            rows = session.scalars(stmt.offset(offset).limit(limit)).all()
            return [self._to_bare_metal_inquiry(row) for row in rows]

    def count_bare_metal_inquiries_from_ip_since(
        self, source_ip: str, since_seconds: int
    ) -> int:
        from datetime import timedelta
        from greencompute_persistence.orm import utcnow

        if not source_ip:
            return 0
        cutoff = utcnow() - timedelta(seconds=since_seconds)
        with session_scope(self.session_factory) as session:
            rows = session.scalars(
                select(BareMetalInquiryORM).where(
                    BareMetalInquiryORM.source_ip == source_ip,
                    BareMetalInquiryORM.submitted_at >= cutoff,
                )
            ).all()
            return len(rows)

    def update_bare_metal_inquiry_status(
        self, inquiry_id: str, *, status: str | None = None, review_notes: str | None = None
    ) -> BareMetalInquiryRecord | None:
        from greencompute_persistence.orm import utcnow

        with session_scope(self.session_factory) as session:
            row = session.get(BareMetalInquiryORM, inquiry_id)
            if row is None:
                return None
            if status is not None:
                row.status = status
            if review_notes is not None:
                row.review_notes = review_notes
            row.reviewed_at = utcnow()
            session.add(row)
            return self._to_bare_metal_inquiry(row)

    @staticmethod
    def _to_bare_metal_inquiry(row: BareMetalInquiryORM) -> BareMetalInquiryRecord:
        return BareMetalInquiryRecord(
            inquiry_id=row.inquiry_id,
            name=row.name or "",
            email=row.email,
            company=row.company or "",
            card_type=row.card_type or "",
            node_count=row.node_count,
            required_vram_gb=row.required_vram_gb,
            storage_gb_per_node=row.storage_gb_per_node,
            work_type=row.work_type or "",
            deployment_date=row.deployment_date or "",
            duration=row.duration or "",
            notes=row.notes or "",
            discord=getattr(row, "discord", "") or "",
            phone=getattr(row, "phone", "") or "",
            source_ip=row.source_ip,
            user_agent=row.user_agent,
            status=row.status or "new",
            review_notes=row.review_notes or "",
            submitted_at=row.submitted_at,
            reviewed_at=row.reviewed_at,
        )

    # --- GPU capacity overrides (admin-controlled cluster size) ---------

    def list_gpu_capacity_overrides(self) -> list[GpuCapacityOverride]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(select(GpuCapacityOverrideORM)).all()
            return [self._to_capacity_override(r) for r in rows]

    def get_gpu_capacity_override(self, gpu_model: str) -> GpuCapacityOverride | None:
        with session_scope(self.session_factory) as session:
            row = session.get(GpuCapacityOverrideORM, gpu_model.lower())
            return self._to_capacity_override(row) if row else None

    def upsert_gpu_capacity_override(
        self, override: GpuCapacityOverride
    ) -> GpuCapacityOverride:
        from greencompute_persistence.orm import utcnow

        key = override.gpu_model.lower()
        with session_scope(self.session_factory) as session:
            row = session.get(GpuCapacityOverrideORM, key) or GpuCapacityOverrideORM(
                gpu_model=key
            )
            row.total_gpus = override.total_gpus
            row.available_gpus = override.available_gpus
            row.note = override.note
            row.updated_by = override.updated_by
            row.updated_at = utcnow()
            session.add(row)
        return override

    def delete_gpu_capacity_override(self, gpu_model: str) -> bool:
        with session_scope(self.session_factory) as session:
            row = session.get(GpuCapacityOverrideORM, gpu_model.lower())
            if row is None:
                return False
            session.delete(row)
            return True

    @staticmethod
    def _to_capacity_override(row: GpuCapacityOverrideORM) -> GpuCapacityOverride:
        return GpuCapacityOverride(
            gpu_model=row.gpu_model,
            total_gpus=row.total_gpus or 0,
            available_gpus=row.available_gpus or 0,
            note=row.note or "",
            updated_by=row.updated_by or "",
            updated_at=row.updated_at,
        )

    @staticmethod
    def _to_inquiry(row: CommercialInquiryORM) -> CommercialInquiryRecord:
        return CommercialInquiryRecord(
            inquiry_id=row.inquiry_id,
            name=row.name or "",
            email=row.email,
            company=row.company or "",
            gpu_count=row.gpu_count,
            duration=row.duration or "",
            deployment_date=getattr(row, "deployment_date", "") or "",
            budget=row.budget or "",
            use_case=row.use_case or "",
            discord=getattr(row, "discord", "") or "",
            phone=getattr(row, "phone", "") or "",
            source_ip=row.source_ip,
            user_agent=row.user_agent,
            status=row.status or "new",
            notes=row.notes or "",
            submitted_at=row.submitted_at,
            reviewed_at=row.reviewed_at,
        )

    @staticmethod
    def _to_user(row: UserORM) -> UserRecord:
        return UserRecord(
            user_id=row.user_id,
            username=row.username,
            email=row.email,
            display_name=row.display_name,
            bio=row.bio,
            website=row.website,
            metadata=row.profile_metadata or {},
            balance_credits=getattr(row, "balance_credits", 0),
            created_at=row.created_at,
        )

    @staticmethod
    def _to_api_key(row: APIKeyORM) -> APIKeyRecord:
        return APIKeyRecord(
            key_id=row.key_id,
            user_id=row.user_id,
            name=row.name,
            admin=row.admin,
            scopes=row.scopes,
            secret=row.secret,
            created_at=row.created_at,
        )

    @staticmethod
    def _to_secret(row: UserSecretORM) -> UserSecretRecord:
        return UserSecretRecord(
            secret_id=row.secret_id,
            user_id=row.user_id,
            name=row.name,
            value=row.value,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _to_workload_share(row: WorkloadShareORM) -> WorkloadShareRecord:
        return WorkloadShareRecord(
            share_id=row.share_id,
            workload_id=row.workload_id,
            owner_user_id=row.owner_user_id,
            shared_with_user_id=row.shared_with_user_id,
            permission=row.permission,
            created_at=row.created_at,
        )

    # --- Provider servers (self-service onboarding) --------------------

    def save_provider_server(self, server: ProviderServerRecord) -> ProviderServerRecord:
        with session_scope(self.session_factory) as session:
            row = session.get(ProviderServerORM, server.server_id) or ProviderServerORM(
                server_id=server.server_id
            )
            row.owner_user_id = server.owner_user_id
            row.hotkey = server.hotkey
            row.payout_address = server.payout_address
            row.label = server.label
            row.ssh_host = server.ssh_host
            row.ssh_port = server.ssh_port
            row.ssh_user = server.ssh_user
            row.node_id = server.node_id
            row.status = server.status
            row.gpu_model = server.gpu_model
            row.gpu_count = server.gpu_count
            row.vram_gb_per_gpu = server.vram_gb_per_gpu
            row.cpu_cores = server.cpu_cores
            row.memory_gb = server.memory_gb
            row.public_ip = server.public_ip
            row.last_error = server.last_error
            row.provision_log = server.provision_log
            row.created_at = server.created_at
            row.updated_at = server.updated_at
            session.add(row)
        return server

    def get_provider_server(self, server_id: str) -> ProviderServerRecord | None:
        with session_scope(self.session_factory) as session:
            row = session.get(ProviderServerORM, server_id)
            return self._to_provider_server(row) if row else None

    def list_provider_servers(self, owner_user_id: str | None, *, admin: bool = False) -> list[ProviderServerRecord]:
        with session_scope(self.session_factory) as session:
            stmt = select(ProviderServerORM).order_by(ProviderServerORM.created_at.desc())
            if not admin:
                stmt = stmt.where(ProviderServerORM.owner_user_id == owner_user_id)
            rows = session.scalars(stmt).all()
            return [self._to_provider_server(r) for r in rows]

    def update_provider_server_status(
        self,
        server_id: str,
        *,
        status: str | None = None,
        append_log: str | None = None,
        last_error: str | None = None,
        hardware: dict[str, Any] | None = None,
        node_id: str | None = None,
        public_ip: str | None = None,
    ) -> ProviderServerRecord | None:
        """Atomic status/log update used by the provisioning job. `append_log`
        is appended to the existing provision_log so the UI can stream it."""
        from greencompute_persistence.orm import utcnow

        with session_scope(self.session_factory) as session:
            row = session.get(ProviderServerORM, server_id)
            if row is None:
                return None
            if status is not None:
                row.status = status
            if last_error is not None:
                row.last_error = last_error
            if node_id is not None:
                row.node_id = node_id
            if public_ip is not None:
                row.public_ip = public_ip
            if append_log:
                prev = row.provision_log or ""
                # Cap the stored log so a pathological run can't bloat the row.
                row.provision_log = (prev + append_log)[-20000:]
            if hardware:
                for k in ("gpu_model", "gpu_count", "vram_gb_per_gpu", "cpu_cores", "memory_gb"):
                    if k in hardware and hardware[k] is not None:
                        setattr(row, k, hardware[k])
            row.updated_at = utcnow()
            session.add(row)
            return self._to_provider_server(row)

    def delete_provider_server(self, server_id: str) -> ProviderServerRecord | None:
        with session_scope(self.session_factory) as session:
            row = session.get(ProviderServerORM, server_id)
            if row is None:
                return None
            rec = self._to_provider_server(row)
            session.delete(row)
            return rec

    @staticmethod
    def _to_provider_server(row: ProviderServerORM) -> ProviderServerRecord:
        return ProviderServerRecord(
            server_id=row.server_id,
            owner_user_id=row.owner_user_id,
            hotkey=row.hotkey or "",
            payout_address=row.payout_address or "",
            label=row.label or "",
            ssh_host=row.ssh_host or "",
            ssh_port=row.ssh_port or 22,
            ssh_user=row.ssh_user or "root",
            node_id=row.node_id or "",
            status=row.status or "pending",
            gpu_model=row.gpu_model or "",
            gpu_count=row.gpu_count or 0,
            vram_gb_per_gpu=row.vram_gb_per_gpu or 0,
            cpu_cores=row.cpu_cores or 0,
            memory_gb=row.memory_gb or 0,
            public_ip=row.public_ip or "",
            last_error=row.last_error or "",
            provision_log=row.provision_log or "",
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
