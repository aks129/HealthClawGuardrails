"""Tests for r6/sdc/documents.py — DocumentReference persistence with
embedded PDF bytes (base64 Attachment.data) and the round-trip getter used
by the delivery route (Task 7)."""

import base64

from r6.sdc.documents import persist_intake_document, get_document_pdf_bytes

PDF_BYTES = b"%PDF-1.4 test bytes"
OTHER_TENANT = "other-tenant"


def test_persist_stores_documentreference_retrievable_by_id(app):
    with app.app_context():
        from r6.models import R6Resource

        resource = persist_intake_document(
            "test-tenant", "Patient/123", PDF_BYTES, title="My Intake")

        assert resource["resourceType"] == "DocumentReference"
        assert resource["status"] == "current"
        assert resource["subject"]["reference"] == "Patient/123"

        row = R6Resource.query.filter_by(
            resource_type="DocumentReference", id=resource["id"],
            tenant_id="test-tenant").first()
        assert row is not None


def test_attachment_content_type_and_size(app):
    with app.app_context():
        resource = persist_intake_document(
            "test-tenant", "Patient/123", PDF_BYTES)
        attachment = resource["content"][0]["attachment"]
        assert attachment["contentType"] == "application/pdf"
        assert attachment["size"] == len(PDF_BYTES)


def test_attachment_data_is_base64_and_decodes_to_exact_bytes(app):
    with app.app_context():
        resource = persist_intake_document(
            "test-tenant", "Patient/123", PDF_BYTES)
        attachment = resource["content"][0]["attachment"]
        data = attachment["data"]
        # Valid base64 — round-trips without raising, and matches the input.
        decoded = base64.b64decode(data, validate=True)
        assert decoded == PDF_BYTES


def test_title_defaults_when_not_provided(app):
    with app.app_context():
        resource = persist_intake_document(
            "test-tenant", "Patient/123", PDF_BYTES)
        assert resource["content"][0]["attachment"]["title"] == "Intake form"


def test_title_uses_provided_value(app):
    with app.app_context():
        resource = persist_intake_document(
            "test-tenant", "Patient/123", PDF_BYTES, title="Custom Title")
        assert resource["content"][0]["attachment"]["title"] == "Custom Title"


def test_get_document_pdf_bytes_round_trips_exact_bytes(app):
    with app.app_context():
        resource = persist_intake_document(
            "test-tenant", "Patient/123", PDF_BYTES)
        fetched = get_document_pdf_bytes("test-tenant", resource["id"])
        assert fetched == PDF_BYTES


def test_get_document_pdf_bytes_returns_none_for_missing_docref(app):
    with app.app_context():
        assert get_document_pdf_bytes("test-tenant", "does-not-exist") is None


def test_tenant_isolation_docref_not_visible_to_other_tenant(app):
    with app.app_context():
        resource = persist_intake_document(
            "test-tenant", "Patient/123", PDF_BYTES)
        # Same docref id, wrong tenant — must not resolve.
        assert get_document_pdf_bytes(OTHER_TENANT, resource["id"]) is None


def test_tenant_isolation_query_scoped(app):
    with app.app_context():
        from r6.models import R6Resource

        resource = persist_intake_document(
            "test-tenant", "Patient/123", PDF_BYTES)

        other_tenant_rows = R6Resource.query.filter_by(
            resource_type="DocumentReference", tenant_id=OTHER_TENANT).all()
        assert other_tenant_rows == []

        same_id_other_tenant = R6Resource.query.filter_by(
            resource_type="DocumentReference", id=resource["id"],
            tenant_id=OTHER_TENANT).first()
        assert same_id_other_tenant is None


def test_questionnaire_response_id_links_context_related(app):
    with app.app_context():
        resource = persist_intake_document(
            "test-tenant", "Patient/123", PDF_BYTES,
            questionnaire_response_id="qr-42")

        related_refs = [r["reference"]
                        for r in resource["context"]["related"]]
        assert "QuestionnaireResponse/qr-42" in related_refs


def test_questionnaire_response_id_links_relates_to(app):
    with app.app_context():
        resource = persist_intake_document(
            "test-tenant", "Patient/123", PDF_BYTES,
            questionnaire_response_id="qr-42")

        assert resource["relatesTo"][0]["target"]["reference"] == \
            "QuestionnaireResponse/qr-42"
        assert resource["relatesTo"][0]["code"] == "transforms"


def test_no_questionnaire_response_id_omits_context_and_relates_to(app):
    with app.app_context():
        resource = persist_intake_document(
            "test-tenant", "Patient/123", PDF_BYTES)
        assert "context" not in resource
        assert "relatesTo" not in resource


def test_persisted_document_visible_via_get_after_second_document(app):
    """A second DocumentReference for a different subject doesn't shadow
    the first — bytes fetched by id must match what was stored for that id."""
    with app.app_context():
        first = persist_intake_document(
            "test-tenant", "Patient/123", PDF_BYTES)
        second_bytes = b"%PDF-1.4 a different document entirely"
        second = persist_intake_document(
            "test-tenant", "Patient/456", second_bytes)

        assert get_document_pdf_bytes("test-tenant", first["id"]) == PDF_BYTES
        assert get_document_pdf_bytes("test-tenant", second["id"]) == second_bytes
