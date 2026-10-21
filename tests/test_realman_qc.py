"""Observable outcomes of the local preflight, including damaged real-world boundaries."""

import json
from pathlib import Path

import cv2
import numpy as np
import pytest
from mcap.writer import CompressionType
from mcap_ros2.writer import Writer
from pydantic import ValidationError

from hflow.realman_qc import QCConfig, inspect_file, render_html, run_batch, sha256

CAMERA = "/camera"
STATE = "/state"
IMAGE_SCHEMA = """std_msgs/Header header
string format
uint8[] data
================================================================================
MSG: std_msgs/Header
builtin_interfaces/Time stamp
string frame_id
================================================================================
MSG: builtin_interfaces/Time
int32 sec
uint32 nanosec
"""
STATE_SCHEMA = """sensor_msgs/JointState joint_state
================================================================================
MSG: sensor_msgs/JointState
float64[] position
"""


def profile(**overrides: object) -> QCConfig:
    return QCConfig.model_validate(
        {
            "required_cameras": [CAMERA],
            "states": {STATE: {"expected_size": 2}},
            "calibrated": True,
            **overrides,
        }
    )


def recording(
    path: Path,
    *,
    stamps: tuple[int, ...] = (1_000_000_000, 1_100_000_000, 1_200_000_000),
    bad_state: bool = False,
    bad_image: bool = False,
    missing_state: bool = False,
    black: bool = False,
    jpeg_padding: bool = False,
    truncated_jpeg: bool = False,
    state_offset_ns: int = 0,
) -> Path:
    with path.open("wb") as stream:
        writer = Writer(stream, compression=CompressionType.NONE)
        image_schema = writer.register_msgdef("sensor_msgs/msg/CompressedImage", IMAGE_SCHEMA)
        state_schema = writer.register_msgdef("test_msgs/msg/Arm", STATE_SCHEMA)
        for index, stamp in enumerate(stamps):
            pixels = (
                np.zeros((90, 160), dtype=np.uint8)
                if black
                else np.random.default_rng(index).integers(40, 255, (90, 160), dtype=np.uint8)
            )
            ok, encoded = cv2.imencode(".jpg", pixels)
            assert ok
            payload = encoded.tobytes()
            if jpeg_padding:
                payload += b"\x00" * 7
            if truncated_jpeg:
                payload = payload[:-2]
            image = {
                "header": {
                    "stamp": {"sec": stamp // 10**9, "nanosec": stamp % 10**9},
                    "frame_id": "camera",
                },
                "format": "jpeg",
                "data": b"broken" if bad_image else payload,
            }
            writer.write_message(CAMERA, image_schema, image, log_time=stamp)
            if not missing_state:
                writer.write_message(
                    STATE,
                    state_schema,
                    {
                        "joint_state": {
                            "position": [float("nan") if bad_state else index * 0.1, 1.0]
                        }
                    },
                    log_time=stamp + state_offset_ns,
                )
        writer.finish()
    return path


def codes(result: dict) -> set[str]:
    return {item["code"] for item in result["findings"]}


def test_intact_realman_shape_runs_hflow_and_preserves_input(tmp_path: Path) -> None:
    path = recording(tmp_path / "good.mcap")
    digest = sha256(path)
    result = inspect_file(path, profile())
    assert result["quality_status"] == "PASS"
    assert result["integrity"]["coverage"] == "full"
    assert result["topics"][CAMERA]["decoded_count"] == 3
    assert result["topics"][STATE]["finite_state_count"] == 3
    assert result["hflow_checks"]["status"] == "completed"
    assert result["source_unchanged"] and sha256(path) == digest


def test_truncated_footer_retains_evidence_but_never_passes(tmp_path: Path) -> None:
    path = recording(tmp_path / "truncated.mcap")
    path.write_bytes(path.read_bytes()[:-40])
    result = inspect_file(path, profile())
    assert result["quality_status"] == "FAIL"
    assert "incomplete_mcap" in codes(result)
    assert result["integrity"]["message_count"] == 6
    assert result["integrity"]["coverage"] == "readable_prefix_only"
    assert result["hflow_checks"]["status"] == "skipped"


def test_chunk_crc_damage_blocks_acceptance(tmp_path: Path) -> None:
    path = recording(tmp_path / "crc-damage.mcap")
    raw = bytearray(path.read_bytes())
    image_offset = raw.index(b"\xff\xd8\xff")
    raw[image_offset + 100] ^= 1
    path.write_bytes(raw)
    result = inspect_file(path, profile())
    assert result["quality_status"] == "FAIL"
    assert "CRCValidationError" in result["integrity"]["scan_error"]
    assert result["hflow_checks"]["status"] == "skipped"


@pytest.mark.parametrize("defect", ["bad_state", "bad_image", "missing_state"])
def test_unusable_required_stream_blocks_acceptance(tmp_path: Path, defect: str) -> None:
    path = recording(
        tmp_path / "bad.mcap",
        bad_state=defect == "bad_state",
        bad_image=defect == "bad_image",
        missing_state=defect == "missing_state",
    )
    result = inspect_file(path, profile())
    assert result["quality_status"] == "FAIL"
    assert codes(result) & {"payload_invalid", "missing_required_topic"}


def test_raw_order_gaps_and_sync_are_not_hidden_by_index_sorting(tmp_path: Path) -> None:
    path = recording(
        tmp_path / "timing.mcap",
        stamps=(1_000_000_000, 1_100_000_000, 1_100_000_000, 1_050_000_000, 2_000_000_000),
    )
    result = inspect_file(path, profile(expected_hz={CAMERA: 10, STATE: 10}))
    assert result["quality_status"] == "REVIEW"
    clock = result["topics"][CAMERA]["log_timing"]
    assert clock["duplicate_adjacent_count"] == 1
    assert clock["out_of_order_count"] == 1
    assert clock["gap_count"] == 1
    assert clock["gap_examples"][0]["start_ns"] == 1_050_000_000


def test_static_black_images_are_review_not_automatic_rejection(tmp_path: Path) -> None:
    path = recording(
        tmp_path / "black.mcap",
        stamps=tuple(1_000_000_000 + i * 100_000_000 for i in range(25)),
        black=True,
    )
    result = inspect_file(path, profile())
    assert result["quality_status"] == "REVIEW"
    assert {"black_frames", "near_static"} <= codes(result)
    assert result["topics"][CAMERA]["near_static_intervals"][0]["duration_s"] == 2.4


def test_invalid_input_batch_isolated_and_report_is_safe_html(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    recording(inputs / "good.mcap")
    (inputs / "invalid.mcap").write_bytes(b"not an mcap")
    config = tmp_path / "config.json"
    config.write_text(profile().model_dump_json(), encoding="utf-8")
    output, code = run_batch(inputs, config, tmp_path / "runtime", tmp_path / "reports")
    batch = json.loads((output / "report.json").read_text())
    assert code == 2
    assert [result["quality_status"] for result in batch["results"]] == ["PASS", "FAIL"]
    assert all(result["original_unchanged"] for result in batch["results"])
    batch["results"][0]["file"] = "<script>alert(1)</script>"
    assert "<script>" not in render_html(batch)
    assert "&lt;script&gt;" in render_html(batch)
    assert (output / "report.html").is_file()


def test_uncalibrated_profile_cannot_automatically_pass(tmp_path: Path) -> None:
    result = inspect_file(recording(tmp_path / "good.mcap"), profile(calibrated=False))
    assert result["quality_status"] == "REVIEW"
    assert "uncalibrated_profile" in codes(result)


def test_padded_camera_buffers_are_valid_but_truncated_jpeg_is_not(tmp_path: Path) -> None:
    padded = inspect_file(recording(tmp_path / "padded.mcap", jpeg_padding=True), profile())
    truncated = inspect_file(recording(tmp_path / "cut.mcap", truncated_jpeg=True), profile())
    assert padded["quality_status"] == "PASS"
    assert truncated["quality_status"] == "FAIL"
    assert truncated["topics"][CAMERA]["error_counts"]["image_missing_jpeg_end"] == 3


def test_camera_state_clock_offset_has_locatable_evidence(tmp_path: Path) -> None:
    path = recording(tmp_path / "offset.mcap", state_offset_ns=500_000_000)
    result = inspect_file(path, profile())
    assert "camera_state_sync" in codes(result)
    sync = result["camera_state_sync"][0]
    assert sync["outside_tolerance_count"] == 3
    assert sync["examples"][0]["log_time_ns"] == 1_000_000_000
    assert sync["max_ms"] == 500


@pytest.mark.parametrize("rate", [0, -1, float("nan"), float("inf")])
def test_invalid_rate_rejected_before_inspection(rate: float) -> None:
    with pytest.raises(ValidationError):
        profile(expected_hz={CAMERA: rate})
