import os
from bson import ObjectId

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorGridFSBucket

from .models import CallEvent, Handoff, JourneySubmission, Lead, SalesDraft, ScriptDocument, VoiceArtifact
from .seed_data import SEED_LEADS


class Repository:
    async def connect(self) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    async def seed(self) -> None:
        raise NotImplementedError

    async def list_leads(self) -> list[Lead]:
        raise NotImplementedError

    async def get_lead(self, lead_id: str) -> Lead | None:
        raise NotImplementedError

    async def save_lead(self, lead: Lead) -> None:
        raise NotImplementedError

    async def add_event(self, event: CallEvent) -> None:
        raise NotImplementedError

    async def list_events(self, lead_id: str) -> list[CallEvent]:
        raise NotImplementedError

    async def add_handoff(self, handoff: Handoff) -> None:
        raise NotImplementedError

    async def list_handoffs(self, lead_id: str) -> list[Handoff]:
        raise NotImplementedError

    async def add_submission(self, submission: JourneySubmission) -> None:
        raise NotImplementedError

    async def list_submissions(self, lead_id: str) -> list[JourneySubmission]:
        raise NotImplementedError

    async def save_voice_artifact(self, artifact: VoiceArtifact, data: bytes | None = None) -> VoiceArtifact:
        raise NotImplementedError

    async def list_voice_artifacts(self, lead_id: str) -> list[VoiceArtifact]:
        raise NotImplementedError

    async def get_voice_artifact(self, artifact_id: str) -> VoiceArtifact | None:
        raise NotImplementedError

    async def read_voice_artifact(self, artifact: VoiceArtifact) -> bytes:
        raise NotImplementedError

    async def upsert_draft(self, draft: SalesDraft) -> SalesDraft:
        raise NotImplementedError

    async def get_draft(self, draft_id: str) -> SalesDraft | None:
        raise NotImplementedError

    async def list_drafts(self) -> list[SalesDraft]:
        raise NotImplementedError

    async def add_script(self, document: ScriptDocument) -> None:
        raise NotImplementedError

    async def list_scripts(self) -> list[ScriptDocument]:
        raise NotImplementedError

    async def get_script(self, script_id: str) -> ScriptDocument | None:
        raise NotImplementedError

    async def save_file(self, filename: str, data: bytes, metadata: dict | None = None) -> str:
        raise NotImplementedError


class MongoRepository(Repository):
    def __init__(self, uri: str, db_name: str = "cimenergy") -> None:
        self.uri = uri
        self.db_name = db_name
        self.client: AsyncIOMotorClient | None = None
        self.fs: AsyncIOMotorGridFSBucket | None = None

    async def connect(self) -> None:
        self.client = AsyncIOMotorClient(
            self.uri,
            appname=os.getenv("MONGODB_APP_NAME", "CIMEnergy"),
            serverSelectionTimeoutMS=10000,
        )
        await self.client.admin.command("ping")
        self.fs = AsyncIOMotorGridFSBucket(self.db, bucket_name="voice_artifacts_fs")
        await self._ensure_indexes()

    async def close(self) -> None:
        if self.client:
            self.client.close()

    @property
    def db(self):
        if not self.client:
            raise RuntimeError("MongoRepository is not connected")
        return self.client[self.db_name]

    async def _ensure_indexes(self) -> None:
        await self.db.leads.create_index("lead_id", unique=True)
        await self.db.call_events.create_index([("lead_id", 1), ("created_at", 1)])
        await self.db.call_events.create_index("call_id")
        await self.db.handoffs.create_index([("lead_id", 1), ("created_at", 1)])
        await self.db.journey_submissions.create_index([("lead_id", 1), ("created_at", 1)])
        await self.db.voice_artifacts.create_index([("lead_id", 1), ("created_at", 1)])
        await self.db.voice_artifacts.create_index("call_id")
        await self.db.sales_drafts.create_index("draft_id", unique=True)
        await self.db.sales_drafts.create_index([("lead_id", 1), ("updated_at", -1)])
        await self.db.script_documents.create_index("script_id", unique=True)

    async def seed(self) -> None:
        for item in SEED_LEADS:
            await self.db.leads.update_one(
                {"lead_id": item["lead_id"]},
                {"$setOnInsert": dict(item)},
                upsert=True,
            )

    async def list_leads(self) -> list[Lead]:
        docs = await self.db.leads.find({}, {"_id": False}).sort("lead_id", 1).to_list(length=100)
        return [Lead.model_validate(item) for item in docs]

    async def get_lead(self, lead_id: str) -> Lead | None:
        doc = await self.db.leads.find_one({"lead_id": lead_id}, {"_id": False})
        return Lead.model_validate(doc) if doc else None

    async def save_lead(self, lead: Lead) -> None:
        await self.db.leads.replace_one(
            {"lead_id": lead.lead_id},
            lead.model_dump(mode="json"),
            upsert=True,
        )

    async def add_event(self, event: CallEvent) -> None:
        await self.db.call_events.insert_one(event.model_dump(mode="json"))

    async def list_events(self, lead_id: str) -> list[CallEvent]:
        docs = await self.db.call_events.find({"lead_id": lead_id}, {"_id": False}).sort("created_at", 1).to_list(500)
        return [CallEvent.model_validate(item) for item in docs]

    async def add_handoff(self, handoff: Handoff) -> None:
        await self.db.handoffs.insert_one(handoff.model_dump(mode="json"))

    async def list_handoffs(self, lead_id: str) -> list[Handoff]:
        docs = await self.db.handoffs.find({"lead_id": lead_id}, {"_id": False}).sort("created_at", 1).to_list(100)
        return [Handoff.model_validate(item) for item in docs]

    async def add_submission(self, submission: JourneySubmission) -> None:
        await self.db.journey_submissions.insert_one(submission.model_dump(mode="json"))

    async def list_submissions(self, lead_id: str) -> list[JourneySubmission]:
        docs = await self.db.journey_submissions.find({"lead_id": lead_id}, {"_id": False}).sort("created_at", 1).to_list(50)
        return [JourneySubmission.model_validate(item) for item in docs]

    async def save_voice_artifact(self, artifact: VoiceArtifact, data: bytes | None = None) -> VoiceArtifact:
        artifact_to_store = artifact
        if data is not None:
            if not self.fs:
                raise RuntimeError("GridFS is not initialized")
            gridfs_id = await self.fs.upload_from_stream(
                artifact.filename,
                data,
                metadata={
                    "artifact_id": artifact.artifact_id,
                    "lead_id": artifact.lead_id,
                    "call_id": artifact.call_id,
                    "source": artifact.source,
                    "artifact_type": artifact.artifact_type,
                    **artifact.metadata,
                },
            )
            artifact_to_store = artifact.model_copy(
                update={
                    "storage": "gridfs",
                    "gridfs_id": str(gridfs_id),
                    "size_bytes": len(data),
                }
            )

        await self.db.voice_artifacts.insert_one(artifact_to_store.model_dump(mode="json"))
        return artifact_to_store

    async def list_voice_artifacts(self, lead_id: str) -> list[VoiceArtifact]:
        docs = await self.db.voice_artifacts.find({"lead_id": lead_id}, {"_id": False}).sort("created_at", 1).to_list(200)
        return [VoiceArtifact.model_validate(item) for item in docs]

    async def get_voice_artifact(self, artifact_id: str) -> VoiceArtifact | None:
        doc = await self.db.voice_artifacts.find_one({"artifact_id": artifact_id}, {"_id": False})
        return VoiceArtifact.model_validate(doc) if doc else None

    async def read_voice_artifact(self, artifact: VoiceArtifact) -> bytes:
        if artifact.storage != "gridfs" or not artifact.gridfs_id:
            raise FileNotFoundError("Artifact is not stored in GridFS")
        if not self.fs:
            raise RuntimeError("GridFS is not initialized")
        stream = await self.fs.open_download_stream(ObjectId(artifact.gridfs_id))
        return await stream.read()

    async def upsert_draft(self, draft: SalesDraft) -> SalesDraft:
        await self.db.sales_drafts.replace_one(
            {"draft_id": draft.draft_id},
            draft.model_dump(mode="json"),
            upsert=True,
        )
        return draft

    async def get_draft(self, draft_id: str) -> SalesDraft | None:
        doc = await self.db.sales_drafts.find_one({"draft_id": draft_id}, {"_id": False})
        return SalesDraft.model_validate(doc) if doc else None

    async def list_drafts(self) -> list[SalesDraft]:
        docs = await self.db.sales_drafts.find({}, {"_id": False}).sort("updated_at", -1).to_list(200)
        return [SalesDraft.model_validate(item) for item in docs]

    async def add_script(self, document: ScriptDocument) -> None:
        await self.db.script_documents.insert_one(document.model_dump(mode="json"))

    async def list_scripts(self) -> list[ScriptDocument]:
        docs = await self.db.script_documents.find({}, {"_id": False}).sort("created_at", -1).to_list(100)
        return [ScriptDocument.model_validate(item) for item in docs]

    async def get_script(self, script_id: str) -> ScriptDocument | None:
        doc = await self.db.script_documents.find_one({"script_id": script_id}, {"_id": False})
        return ScriptDocument.model_validate(doc) if doc else None

    async def save_file(self, filename: str, data: bytes, metadata: dict | None = None) -> str:
        if not self.fs:
            raise RuntimeError("GridFS is not initialized")
        gridfs_id = await self.fs.upload_from_stream(filename, data, metadata=metadata or {})
        return str(gridfs_id)


def build_repository() -> Repository:
    mongo_uri = os.getenv("MONGODB_URI") or os.getenv("MONGO_URI")
    if not mongo_uri:
        raise RuntimeError("MongoDB is required. Set MONGO_URI or MONGODB_URI in .env.")
    return MongoRepository(mongo_uri, os.getenv("MONGODB_DATABASE", "cimenergy"))
