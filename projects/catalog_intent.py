"""A catalog-to-project navigation hint; matching still decides applicability."""

import re

from django.core import signing
from django.core.signing import BadSignature

from catalog.models import (
    CatalogEvidenceBatch, CatalogEvidenceClaim, CatalogFamily, SupplementProduct,
)
from catalog.publication import visible_families
from catalog.supplement_publication import publicly_available_supplement
from projects.task_profiles import process_for


SHA256 = re.compile(r"[0-9a-f]{64}\Z")
INTENT_SALT = "projects.catalog-intent.v1"


def make_catalog_intent_token(object_slug, process_code, source_kind, source_ref, checksum):
    """Sign public source identifiers so the process and object cannot be edited."""
    if (source_kind not in {"organizer_v4", "manufacturer_supplement"}
            or not isinstance(source_ref, str) or not source_ref
            or len(source_ref) > 100 or not SHA256.fullmatch(checksum or "")
            or process_for(object_slug, process_code) is None):
        return None
    return signing.dumps({
        "object": object_slug, "process": process_code, "kind": source_kind,
        "ref": source_ref, "checksum": checksum,
    }, salt=INTENT_SALT)


def catalog_intent(object_slug, process_code, token, snapshot):
    """Resolve a public model and a source-backed process in a pinned snapshot."""
    process = process_for(object_slug, process_code)
    if process is None or not isinstance(token, str) or len(token) > 1000:
        return None
    try:
        payload = signing.loads(token, salt=INTENT_SALT)
    except (BadSignature, TypeError, ValueError):
        return None
    if (not isinstance(payload, dict) or payload.get("object") != object_slug
            or payload.get("process") != process.code):
        return None
    kind, ref, checksum = payload.get("kind"), payload.get("ref"), payload.get("checksum")
    if (kind not in {"organizer_v4", "manufacturer_supplement"}
            or not isinstance(ref, str) or not ref or len(ref) > 100
            or not isinstance(checksum, str) or not SHA256.fullmatch(checksum)):
        return None
    if kind == "organizer_v4":
        if (checksum != snapshot.get("evidence_checksum") or not ref.isascii()
                or not ref.isdecimal() or len(ref) > 18):
            return None
        evidence = CatalogEvidenceBatch.objects.filter(
            checksum=checksum, catalog_batch__checksum=snapshot.get("catalog_checksum"),
        ).first()
        if evidence is None:
            return None
        family = visible_families(evidence.catalog_batch, evidence).filter(pk=int(ref)).first()
        if family is None or not CatalogEvidenceClaim.objects.filter(
            evidence_batch=evidence, use="candidate_application",
            object_slug=object_slug, value=process.code,
            catalog_rows__family=family,
        ).exists():
            return None
        return {"token": token, "process": process, "name": family.name,
                "source_kind": kind, "family_id": family.pk}
    if kind == "manufacturer_supplement":
        if checksum != snapshot.get("supplement_checksum"):
            return None
        supplement = publicly_available_supplement(checksum)
        if supplement is None:
            return None
        product = SupplementProduct.objects.filter(
            batch=supplement, product_ref=ref,
            applications__object_slug=object_slug,
            applications__process_code=process.code,
        ).first()
        if product is None:
            return None
        return {"token": token, "process": process,
                "name": product.model if product.variant == product.model else f"{product.model} · {product.variant}",
                "source_kind": kind, "product_ref": product.product_ref}
    return None
