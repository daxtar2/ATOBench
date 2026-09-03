from __future__ import annotations

from atobench_vr.predicate_rebuild import _event_id, _value


def test_event_id_matches_alignment_contract() -> None:
    event = {
        "integrity": {
            "raw_file_sha256": "1234567890abcdef9999",
            "raw_line_number": 7,
        }
    }
    assert _event_id(event) == "http:1234567890abcdef:line:7"


def test_machine_status_value_mapping_is_conservative() -> None:
    assert _value("positive") is True
    assert _value("negative") is False
    assert _value("unknown") is None
    assert _value("unavailable") is None
