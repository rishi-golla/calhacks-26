from agents.documents.card import DESCRIPTIONS
from agents.documents.tools import check_packet
from common.agent_support import evidence, finish, identifier, mutate
from common.envelope import DelegationMeta, TaskEnvelope
from common.runtime import FreightAgent
from common.server import serve
from common.source_repository import SourceRepository, SourceUnavailable


class DocumentsAgent(FreightAgent):
    name = "documents"

    async def handle(self, env: TaskEnvelope, meta: DelegationMeta, task_id: str, updater):
        if env.task_type not in {"collect_documents", "check_document_packet"}:
            raise ValueError("unsupported POD & Document Chase skill")
        repository = SourceRepository()
        source = await repository.load(env)
        # Requesting extra evidence is allowed; received documents come only from the source adapter.
        required = env.subject.get("required_documents", source.get("required_documents"))
        packet = check_packet(source, required)
        refs = evidence(source, env)
        for document in packet["documents"]:
            if document.get("kind") not in (required or ["rate_confirmation", "pod"]):
                continue
            await mutate(self, env, task_id, "record_doc_read", {
                "id": identifier(task_id, "read:" + document["ref"]), "doc_ref": document["ref"],
                "page": document.get("page", 1), "span": document.get("span", ""),
                "extracted_json": {"kind": document["kind"], "load_id": source.get("load_id")},
            })
        if packet["complete"] or env.task_type == "check_document_packet":
            return await finish(self, env, task_id, outcome="verified" if packet["complete"] else "unverified",
                                findings=packet, evidence_refs=list(dict.fromkeys(
                                    refs + [document["ref"] for document in packet["documents"]])))
        company_id = source.get("carrier_id")
        try:
            await repository.get_contact(company_id or "")
        except SourceUnavailable:
            await self.park(task_id, "awaiting_external_verification", "documents", f"documents:{env.case_id}",
                            details={"missing": packet["missing"], "reason": "no verified contact"})
        call = TaskEnvelope(task_type="place_call", case_id=env.case_id, priority=env.priority,
                            evidence_refs=refs, subject={"purpose": "verification", "counterparty_id": company_id,
                            "questions": ["Please provide these documents: " + ", ".join(packet["missing"])],
                            "facts": {"load_id": source.get("load_id"), "invoice_id": source.get("invoice_id")}})
        await self.delegate("place_call", call, meta, task_id, invocation_key="request_missing_documents")
        await self.park(task_id, "awaiting_external_verification", "documents", f"documents:{env.case_id}",
                        details={"missing": packet["missing"], "reason": "awaiting source document ingestion"})


if __name__ == "__main__":
    serve(DocumentsAgent(), DESCRIPTIONS)
