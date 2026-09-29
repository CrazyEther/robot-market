"""Stable identity for new sources with a read-only legacy v4 adapter."""

import hashlib
import json
import re


SHA256 = re.compile(r"[0-9a-f]{64}\Z")
PRODUCT_REF = re.compile(r"[a-z0-9][a-z0-9_.-]{0,99}\Z")


def selection_ref(selection, snapshot):
    """Return a validated typed ref, deriving one for old v4 snapshots."""
    if not isinstance(selection, dict) or not isinstance(snapshot, dict):
        return None
    ref = selection.get("selection_ref")
    if ref is None:
        record_index = selection.get("record_index")
        if (type(record_index) is not int or record_index <= 0
                or selection.get("catalog_checksum") != snapshot.get("catalog_checksum")
                or selection.get("evidence_checksum") != snapshot.get("evidence_checksum")):
            return None
        catalog_sha, evidence_sha = selection.get("catalog_checksum"), selection.get("evidence_checksum")
        if not all(isinstance(value, str) and value
                   for value in (catalog_sha, evidence_sha)):
            # Old snapshots are read as stored; newly written typed refs below
            # require real SHA-256 values from the source importer.
            return None
        return {"catalog_source_kind": "organizer_v4", "catalog_source_checksum": catalog_sha,
                "evidence_checksum": evidence_sha, "record_index": record_index}
    if not isinstance(ref, dict):
        return None
    kind = ref.get("catalog_source_kind")
    if kind == "organizer_v4":
        expected = {"catalog_source_kind": kind,
                    "catalog_source_checksum": snapshot.get("catalog_checksum"),
                    "evidence_checksum": snapshot.get("evidence_checksum"),
                    "record_index": selection.get("record_index")}
        if (set(ref) != set(expected) or ref != expected
                or type(ref["record_index"]) is not int or ref["record_index"] <= 0
                or not all(isinstance(ref[key], str) and SHA256.fullmatch(ref[key])
                           for key in ("catalog_source_checksum", "evidence_checksum"))):
            return None
        return ref
    if kind == "manufacturer_supplement":
        expected = {"catalog_source_kind": kind,
                    "catalog_source_checksum": snapshot.get("supplement_checksum"),
                    "evidence_checksum": snapshot.get("supplement_checksum"),
                    "product_ref": selection.get("product_ref"),
                    "offer_ref": selection.get("offer_ref")}
        if (set(ref) != set(expected) or ref != expected
                or not isinstance(ref["catalog_source_checksum"], str)
                or not SHA256.fullmatch(ref["catalog_source_checksum"])
                or ref["evidence_checksum"] != ref["catalog_source_checksum"]
                or not isinstance(ref["product_ref"], str)
                or not PRODUCT_REF.fullmatch(ref["product_ref"])
                or (ref["offer_ref"] is not None and
                    (not isinstance(ref["offer_ref"], str)
                     or not PRODUCT_REF.fullmatch(ref["offer_ref"])))):
            return None
        return ref
    return None


def selection_key(ref):
    if not isinstance(ref, dict):
        return None
    return hashlib.sha256(json.dumps(ref, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def workload_matches_selection(profile, selection, snapshot):
    if not isinstance(profile, dict):
        return False
    ref = selection_ref(selection, snapshot)
    if ref is None:
        return False
    if "selection_ref" in profile:
        return profile.get("selection_ref") == ref
    return (ref["catalog_source_kind"] == "organizer_v4"
            and profile.get("robot_record_index") == ref["record_index"])


def availability_matches_selection(plan_ref, plan, selection, snapshot):
    """Bind a persisted calendar to one exact selection, including old v4 calendars."""
    if not isinstance(plan_ref, dict) or plan is None:
        return False
    ref = selection_ref(selection, snapshot)
    if ref is None:
        return False
    key = selection_key(ref)
    record_index = ref.get("record_index")
    if "selection_ref" in plan_ref or "selection_key" in plan_ref:
        return (plan_ref.get("selection_ref") == ref
                and plan_ref.get("selection_key") == key
                and plan.selection_key == key
                and plan.robot_record_index == record_index
                and plan_ref.get("robot_record_index") == record_index)
    return (ref["catalog_source_kind"] == "organizer_v4"
            and plan.selection_key is None
            and plan.robot_record_index == record_index
            and plan_ref.get("robot_record_index") == record_index)
